"""Authority decisions and watch lifecycle without any real cron or device calls."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import imaplib
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
from socketserver import StreamRequestHandler, TCPServer
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch

import cli
import jobs
import mail_idle  # pyright: ignore[reportMissingImports] (cli adds runtime/ to sys.path)
from tests.test_setup import FakeCron


ADD = ["watch", "add", "--name", "Card drop", "--schedule", "*/30 * * * *",
       "--check", "the shop page for the new card set", "--when", "preorders open"]


class AuthorityTests(unittest.TestCase):
    def test_defaults_and_unknown_actions(self):
        with patch.object(cli.phconf, "CFG", cli.base_config("custom")):
            self.assertEqual(cli.phconf.allowed("calendar_blocks"), "ask")
            self.assertEqual(cli.phconf.allowed("send_email"), "ask")
            self.assertEqual(cli.phconf.allowed("unknown"), "ask")
            self.assertEqual(cli.phconf.allowed("payments"), "never")

    def test_never_wins_and_invalid_auto_keys_are_not_authorized(self):
        cfg = cli.base_config("custom")
        cfg["authority"]["auto"] = ["calendar_blocks", "reminders", "send_email", "unknown"]
        cfg["authority"]["never"].append("reminders")
        with patch.object(cli.phconf, "CFG", cfg):
            self.assertEqual(cli.phconf.allowed("calendar_blocks"), "auto")
            self.assertEqual(cli.phconf.allowed("reminders"), "never")
            self.assertEqual(cli.phconf.allowed("send_email"), "ask")
            self.assertEqual(cli.phconf.allowed("unknown"), "ask")


class WatchFixture(unittest.TestCase):
    def setUp(self):
        home = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"HERMES_HOME": home}, clear=True))
        self.home = Path(home)
        self.cron = FakeCron()
        self.enterContext(patch.object(jobs, "_plugin_ensure", self.cron.ensure))
        self.enterContext(patch.object(jobs, "api_problem", return_value=None))
        self.config = cli.phconf.config_path()
        self.registry = self.config.with_name("jobs.json")
        self.parser = argparse.ArgumentParser()
        cli.setup_parser(self.parser)

    def command(self, argv):
        args = self.parser.parse_args(argv)
        out, err = StringIO(), StringIO()
        apply = jobs.apply

        def apply_fake(cfg, **kwargs):
            kwargs.setdefault("run", self.cron)
            return apply(cfg, **kwargs)

        with patch.object(jobs, "apply", side_effect=apply_fake), redirect_stdout(out), redirect_stderr(err):
            result = cli.handle(args)
        return result, out.getvalue() + err.getvalue()


class SetupAnswersWatchTests(WatchFixture):
    watch = {"name": "Card drop", "schedule": "*/30 * * * *", "check": "shop page",
             "when": "preorders open", "then": "tell"}

    def setup_answers(self, **answers):
        path = self.home / "answers.json"
        path.write_text(json.dumps({"decisions.enabled": "n", **answers}))
        return self.command(["setup", "--preset", "custom", "--answers", str(path)])

    def assert_setup_ok(self, **answers):
        rc, output = self.setup_answers(**answers)
        self.assertEqual(rc, 0, output)

    def assert_watches(self, names):
        self.assertEqual([watch["name"] for watch in cli.phconf.load()["watches"]], names)
        self.assertEqual({key for key in jobs._load_registry() if key.startswith("watch:")},
                         {f"watch:{name}" for name in names})

    def test_explicit_list_installs_watch_on_fresh_desk(self):
        self.assert_setup_ok(watches=[self.watch])
        self.assert_watches(["Card drop"])
        self.assertEqual(cli.phconf.load()["watches"], [self.watch])
        self.assertIn(self.cron.job("Proactive · watch: Card drop"), self.cron.live)

    def test_explicit_empty_list_removes_installed_watch(self):
        self.assert_setup_ok()
        self.assertEqual(self.command(ADD + ["--then", "tell"])[0], 0)
        jid = self.cron.job("Proactive · watch: Card drop")
        self.assert_setup_ok(watches=[])
        self.assert_watches([])
        self.assertNotIn(jid, self.cron.live)

    def test_omission_preserves_watch_changes_after_preview(self):
        self.assert_setup_ok()
        self.assertEqual(self.command(ADD + ["--then", "tell"])[0], 0)

        def change_watches(_):
            self.assertEqual(self.command(["watch", "remove", "--name", "Card drop"])[0], 0)
            self.assertEqual(self.command(ADD[:3] + ["Another drop"] + ADD[4:] + ["--then", "tell"])[0], 0)
            return ""

        with patch.object(cli, "decisions_note", side_effect=change_watches):
            self.assert_setup_ok()
        self.assert_watches(["Another drop"])

    def test_conflicting_watch_change_refuses_without_applying(self):
        self.assert_setup_ok()
        before = None

        def change_watches(_):
            nonlocal before
            self.assertEqual(self.command(ADD + ["--then", "tell"])[0], 0)
            before = self.config.read_bytes(), self.registry.read_bytes(), self.cron.live.copy()
            self.cron.calls.clear()
            return ""

        with patch.object(cli, "decisions_note", side_effect=change_watches):
            rc, output = self.setup_answers(watches=[])
        self.assertNotEqual(rc, 0, output)
        self.assertIn("watches changed during setup", output)
        self.assertEqual((self.config.read_bytes(), self.registry.read_bytes(), self.cron.live), before)
        self.assertEqual(self.cron.calls, [])


class UninstalledWatchTests(WatchFixture):
    def test_staged_watch_does_not_start_mail_push_until_setup(self):
        commands = []
        first_idle = None
        idling = threading.Event()

        class ImapStub(StreamRequestHandler):
            def handle(self):
                nonlocal first_idle
                connection_commands = []
                self.request.settimeout(6)
                self.wfile.write(b"* OK fixture IMAP ready\r\n")
                for line in self.rfile:
                    tag, command, *_ = line.split()
                    commands.append(command)
                    connection_commands.append(command)
                    if command == b"IDLE":
                        if first_idle is None:
                            first_idle = tuple(connection_commands)
                        self.wfile.write(b"+ idling\r\n")
                        idling.set()
                        return
                    if command == b"CAPABILITY":
                        self.wfile.write(b"* CAPABILITY IMAP4rev1 IDLE\r\n")
                    elif command == b"EXAMINE":
                        self.wfile.write(b"* 1 EXISTS\r\n")
                    self.wfile.write(tag + b" OK fixture response\r\n")

        server = self.enterContext(TCPServer(("127.0.0.1", 0), ImapStub))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        credentials = Mock(return_value="fixture-only")
        ctx = Mock()
        ctx.get_config.side_effect = {"mail_1_address": "fixture@example.test", "mail_1_host": "127.0.0.1",
                                      "mail_1_port": port}.get
        self.enterContext(patch.object(cli.phconf, "CFG", cli.phconf.load()))
        self.assertEqual(self.command(ADD + ["--then", "tell"])[0], 0)
        self.assertFalse(self.registry.exists())

        def loopback(host, remote_port, **_):
            self.assertEqual((host, remote_port), server.server_address)
            return imaplib.IMAP4(host, remote_port, timeout=5)

        # The transport is plaintext only in this loopback fixture; exercise real IMAP commands.
        with patch.object(imaplib, "IMAP4_SSL", side_effect=loopback) as connect, \
                patch.object(mail_idle, "_setup_file_log"):
            try:
                outcome = mail_idle.start_in_gateway(ctx, gateway=True, secret=credentials)
                if outcome == "started":
                    idling.wait(5)  # capture the wire trace even on the broken implementation
                self.assertEqual((outcome, commands), ("not set up", []))
                credentials.assert_not_called()
                ctx.get_config.assert_not_called()
                ctx.on_unload.assert_not_called()
                connect.assert_not_called()
                self.assertIsNone(mail_idle._SERVICE)

                answers = self.home / "answers.json"
                answers.write_text(json.dumps({"features": {"mail_radar": True, "mail_push": True},
                                               "decisions.enabled": "n"}))
                rc, output = self.command(["setup", "--preset", "custom", "--answers", str(answers)])
                self.assertEqual(rc, 0, output)
                self.assertIn("watch:Card drop", json.loads(self.registry.read_text()))
                self.assertEqual(mail_idle.start_in_gateway(ctx, gateway=True, secret=credentials), "started")
                self.assertTrue(idling.wait(5), commands)
                # Some versions of imaplib issue their own CAPABILITY right after LOGIN.
                self.assertIn(first_idle, ((b"CAPABILITY", b"LOGIN", b"CAPABILITY", b"EXAMINE", b"IDLE"),
                                           (b"CAPABILITY", b"LOGIN", b"CAPABILITY", b"CAPABILITY", b"EXAMINE", b"IDLE")))
                credentials.assert_called_once_with("PROACTIVE_MAIL_1_PASSWORD")
            finally:
                mail_idle.stop_service()

    def test_staged_watch_does_not_hide_first_chat_setup_offer(self):
        spec = importlib.util.spec_from_file_location("ph_offer_test", jobs.HERE / "__init__.py")
        assert spec is not None and spec.loader is not None
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        self.assertEqual(self.command(ADD + ["--then", "tell"])[0], 0)
        flag = self.config.with_name("setup_offered")
        jobs._save_registry({"replacing": {"say": {"old": "abcdef012345"}}})
        self.assertIsNone(plugin._offer_setup(platform="cron"))
        self.assertEqual(plugin._offer_setup(platform="cli"), {"context": plugin.OFFER})
        self.assertIsNone(plugin._offer_setup(platform="cli"))

        flag.unlink()
        self.config.unlink()
        jobs._save_registry({"say": "abcdef012345"})  # Installed without a mail radar.
        self.assertIsNone(plugin._offer_setup(platform="cli"))
        self.assertFalse(flag.exists())

    def test_fresh_remove_missing_watch_touches_nothing(self):
        self.assertEqual(self.command(["watch", "remove", "--name", "Card drop"]), (0, "No watch named Card drop.\n"))
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(self.cron.calls, [])
        self.assertEqual(self.cron.ensured, [])

    def test_fresh_add_saves_config_without_installing_jobs_or_runtime(self):
        with patch.object(jobs, "preflight") as preflight:
            output = self.command(ADD + ["--then", "tell"])
        self.assertEqual(self.cron.calls, [])
        self.assertEqual(self.cron.ensured, [])
        preflight.assert_not_called()
        self.assertEqual(output, (0, "Saved. Run `hermes proactive setup` to start the desk.\n"))
        self.assertEqual(json.loads(self.config.read_text())["watches"], [
            {"name": "Card drop", "schedule": "*/30 * * * *", "check": "the shop page for the new card set",
             "when": "preorders open", "then": "tell", "urgent": False}])
        self.assertFalse(self.registry.exists())
        self.assertFalse((self.home / "scripts").exists())

    def test_remove_retained_watch_after_uninstall_only_updates_config(self):
        cfg = cli.base_config("custom")
        cfg["watches"] = [{"name": "Card drop", "schedule": "*/30 * * * *", "check": "stock",
                           "when": "available", "then": "tell", "urgent": False}]
        cli.write_config(cfg)
        jobs.apply(cfg, run=self.cron)
        remove_all = jobs.remove_all
        with patch.object(jobs, "remove_all", side_effect=lambda: remove_all(run=self.cron)):
            self.assertEqual(self.command(["remove"])[0], 0)
        self.assertEqual(json.loads(self.registry.read_text()), {})
        self.assertEqual(self.cron.live, {})
        registry_bytes = self.registry.read_bytes()
        runtime = cli.phconf.scripts_dir().readlink()
        self.cron.calls.clear()
        self.cron.ensured.clear()

        output = self.command(["watch", "remove", "--name", "Card drop"])
        self.assertEqual(self.cron.calls, [])
        self.assertEqual(self.cron.ensured, [])
        self.assertEqual(self.cron.live, {})
        self.assertEqual(output, (0, "Saved. Run `hermes proactive setup` to start the desk.\n"))
        self.assertEqual(json.loads(self.config.read_text()), {**cfg, "watches": []})
        self.assertEqual(self.registry.read_bytes(), registry_bytes)
        self.assertEqual(cli.phconf.scripts_dir().readlink(), runtime)


class WatchTests(WatchFixture):
    def setUp(self):
        super().setUp()
        self.run = self.cron
        cfg = cli.base_config("custom")
        cfg["features"] = dict.fromkeys(cfg["features"], False)
        cli.write_config(cfg)
        # An installed desk with a recorded say ID missing from cron. The next
        # add must repair it as well as install the first watch.
        jobs._save_registry({"say": "123456abcdef"})

    def test_add_list_reapply_and_remove_only_the_watch(self):
        self.assertEqual(self.command(ADD + ["--then", "alarm", "--urgent"])[0], 0)
        # Both jobs are staged first and armed only after their ID is recorded.
        self.assertEqual([a[2] for a in self.run.calls if a[2] != "list"],
                         ["create", "edit", "resume", "pause", "edit", "resume"])
        jid = self.run.job("Proactive · watch: Card drop")
        argv = self.run.live[jid]
        self.assertEqual(argv[argv.index("--name") + 1], "Proactive · watch: Card drop")
        self.assertEqual(self.run.schedule_of(jid), "*/30 * * * *")
        self.assertEqual(self.run.toolsets[jid], ["terminal", "file", "skills", "web", "browser"])
        self.assertEqual(json.loads(self.registry.read_text())["watch:Card drop"]["id"], jid)
        self.assertTrue(json.loads(self.config.read_text())["watches"][0]["urgent"])
        self.assertIn("Card drop", self.command(["watch", "list"])[1])
        self.assertIn("preorders open", self.command(["watch", "list"])[1])
        self.run.calls.clear()
        result = jobs.apply(cli.phconf.load(), run=self.run)
        self.assertEqual([row["action"] for row in result if row["job"].startswith("Proactive · watch:")], ["keep"])
        edits = [a for a in self.run.calls if a[2] != "list"]
        self.assertCountEqual([a[2:4] for a in edits], [["edit", owned] for owned in self.run.live])
        self.assertTrue(all("--schedule" not in a and "--timezone" not in a for a in edits))
        self.run.live["abcdef123456"] = ["unrelated job"]
        self.assertEqual(self.command(["watch", "remove", "--name", "Card drop"])[0], 0)
        self.assertEqual(set(self.run.live), {"abcdef123456"})   # no watch and nothing else: no say job either
        self.assertEqual(json.loads(self.registry.read_text()), {})
        self.assertEqual(json.loads(self.config.read_text())["watches"], [])

    def test_legacy_one_shot_is_reported_but_does_not_block_removal(self):
        cfg = cli.phconf.load()
        cfg["features"]["health"] = True
        cli.write_config(cfg)
        self.assertEqual(self.command(ADD + ["--then", "tell"])[0], 0)
        watch = self.run.job("Proactive · watch: Card drop")
        health = self.run.job("Proactive · health")
        cfg = cli.phconf.load()
        cfg["schedule"]["health"] = "in 2h"
        cli.write_config(cfg)
        self.run.manual_edit(health, schedule="once in 2h")
        self.run.repeats[health] = "0/1"
        fields, _ = jobs._fields_by_key(cfg)["health"]
        registry = json.loads(self.registry.read_text())
        registry["health"] = {"id": health, "spec": jobs._fingerprint(fields)}
        self.registry.write_text(json.dumps(registry))
        before = self.config.read_bytes(), self.registry.read_bytes(), self.run.live.copy()
        self.run.calls.clear()

        rc, output = self.command(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("schedule.health must be recurring, not 'in 2h'", output)
        self.assertIn("next reconciliation will reset it to the default '7 * * * *'", output)
        self.assertIn("Proactive · watch: Card drop: installed", output)
        self.assertTrue(all(a[2] == "list" for a in self.run.calls), self.run.calls)
        self.assertEqual((self.config.read_bytes(), self.registry.read_bytes(), self.run.live), before)

        with patch.object(cli, "write_config", side_effect=OSError("fixture publish failure")):
            rc, output = self.command(["watch", "remove", "--name", "Card drop"])
        self.assertEqual(rc, 1, output)
        self.assertEqual(self.config.read_bytes(), before[0])
        self.assertEqual(self.run.states[health], "paused")
        self.assertNotIn(["resume", health], [a[2:4] for a in self.run.calls])

        self.run.calls.clear()
        rc, output = self.command(["watch", "remove", "--name", "Card drop"])
        self.assertEqual(rc, 0, output)
        self.assertIn("health schedule 'in 2h' was a one-time schedule; reset to the default '7 * * * *'", output)
        self.assertIn("removed 1, deferred 0", output)
        calls = [a[2:4] for a in self.run.calls]
        self.assertLess(calls.index(["pause", watch]), calls.index(["remove", watch]))
        self.assertLess(calls.index(["edit", health]), calls.index(["resume", health]))
        self.assertNotIn(health, self.run.repeats)
        health_spec = json.loads(self.registry.read_text())["health"]
        self.assertEqual(health_spec["id"], health)
        self.assertNotEqual(health_spec["spec"], registry["health"]["spec"])
        self.assertEqual(self.run.states[health], "active")
        self.assertEqual(self.run.schedule_of(health), "7 * * * *")
        self.assertNotIn(watch, self.run.live)
        self.assertNotIn("watch:Card drop", json.loads(self.registry.read_text()))
        self.assertEqual(cli.phconf.load()["watches"], [])
        self.assertEqual(cli.phconf.load()["schedule"]["health"], "7 * * * *")
        remove_all = jobs.remove_all
        with patch.object(jobs, "remove_all", side_effect=lambda: remove_all(run=self.run)):
            self.assertEqual(self.command(["remove"])[0], 0)
        self.assertEqual(self.run.live, {})

    def test_publication_is_between_fencing_and_arming(self):
        self.assertEqual(self.command(ADD + ["--then", "tell"])[0], 0)
        old = self.run.job("Proactive · watch: Card drop")
        # A single setup removes the old watch and arms its replacement.
        cfg = cli.phconf.load()
        cfg["watches"][0]["name"] = "Spare parts"
        events = []

        def publish():
            self.assertEqual(self.run.states[old], "paused")
            self.assertEqual(cli.phconf.load()["watches"][0]["name"], "Card drop")
            cli.write_config(cfg)
            events.append("published")

        def host(argv, **kwargs):
            if argv[2] in ("edit", "resume"):
                self.assertEqual(cli.phconf.load()["watches"][0]["name"], "Spare parts")
                self.assertEqual(self.run.states[old], "paused")
                events.append(argv[2])
            return self.run(argv, **kwargs)

        jobs.apply(cfg, run=host, publish=publish)
        self.assertEqual(events[0], "published")
        self.assertIn("resume", events)
        self.assertEqual(self.run.states[self.run.job("Proactive · watch: Spare parts")], "active")
        self.assertNotIn(old, self.run.live)

    def pending_watch(self):
        self.assertEqual(self.command(ADD + ["--then", "tell"])[0], 0)
        old = self.run.job("Proactive · watch: Card drop")
        self.run.executions[old] = [("a" * 32, "running")]
        cfg = cli.phconf.load()
        cfg["slack"]["enabled"] = True
        cli.write_config(cfg)
        self.assertEqual(self.command(ADD + ["--then", "tell"])[0], 0)
        new = json.loads(self.registry.read_text())["watch:Card drop"]["id"]
        self.assertNotEqual(old, new)
        return old, new

    def test_watch_remove_fences_replacement_before_busy_history_poll(self):
        with patch.object(jobs, "IDLE_WAIT_S", 0):
            old, new = self.pending_watch()
            self.run.calls.clear()
            self.assertEqual(self.command(["watch", "remove", "--name", "Card drop"])[0], 0)
        steps = [tuple(a[2:4]) for a in self.run.calls]
        self.assertLess(next(i for i, step in enumerate(steps) if step in {("pause", new), ("remove", new)}),
                        steps.index(("runs", old)))
        self.assertIn("watch:Card drop", json.loads(self.registry.read_text())["replacing"])
        self.run.executions[old] = [("a" * 32, "completed")]
        jobs.apply(cli.phconf.load(), run=self.run)
        jobs.remove_all(run=self.run)
        self.assertEqual(self.run.live, {})
        self.assertNotIn(("resume", old), [tuple(a[2:4]) for a in self.run.calls])

    def test_watch_remove_stays_revoked_when_archive_fails(self):
        with patch.object(jobs, "IDLE_WAIT_S", 0):
            old, new = self.pending_watch()
        self.run.executions[old] = [("a" * 32, "completed")]
        with patch.object(jobs, "_keep_history", side_effect=OSError("fixture archive failure")):
            rc, output = self.command(["watch", "remove", "--name", "Card drop"])
        self.assertEqual(rc, 1)
        self.assertIn("fixture archive failure", output)
        self.assertEqual(cli.phconf.load()["watches"], [])
        self.assertNotEqual(jobs._job_states(self.run).get(new), "active")
        self.assertEqual(self.run.states[old], "paused")

    def test_watch_removal_fences_the_watch_before_recovering_radar_history(self):
        self.assertEqual(self.command(ADD + ["--then", "tell"])[0], 0)
        cfg = cli.phconf.load()
        cfg["features"]["mail_radar"] = True
        cli.write_config(cfg)
        jobs.apply(cfg, run=self.run)
        old = self.run.job("Proactive · mail radar")
        cfg["slack"]["enabled"] = True
        cli.write_config(cfg)
        self.run.executions[old] = [("a" * 32, "running")]
        with patch.object(jobs, "IDLE_WAIT_S", 0):
            jobs.apply(cfg, run=self.run)
        watch = self.run.job("Proactive · watch: Card drop")
        self.assertNotIn("watch:Card drop", jobs._load_registry()["replacing"])
        self.run.executions[old] = []
        radar_before = jobs._load_registry()["replacing"]["mail_radar"]
        self.assertIn(old, radar_before["retired"])
        self.assertEqual(self.run.states[old], "paused")
        self.run.calls.clear()

        def archive(key, jid):
            self.assertNotEqual(self.run.states.get(watch), "active")
            self.assertEqual(cli.phconf.load()["watches"], [])
            self.assertEqual(self.run.states[jid], "paused")
            return keep_history(key, jid)

        keep_history = jobs._keep_history
        with patch.object(jobs, "_keep_history", side_effect=archive) as copy:
            rc, output = self.command(["watch", "remove", "--name", "Card drop"])
        self.assertEqual(rc, 0, output)
        self.assertIn("removed 2, deferred 0", output)
        self.assertEqual({args[1] for args, _ in copy.call_args_list}, {old, watch})
        calls = [a[2:4] for a in self.run.calls]
        self.assertLess(calls.index(["pause", watch]), calls.index(["runs", old]))
        self.assertLess(calls.index(["runs", watch]), calls.index(["remove", watch]))
        self.assertEqual(cli.phconf.load()["watches"], [])
        self.assertNotIn(watch, self.run.live)
        self.assertNotIn("watch:Card drop", jobs._load_registry())
        self.assertNotIn(old, self.run.live)
        self.assertNotIn("replacing", jobs._load_registry())
        self.assertEqual(self.run.states[self.run.job("Proactive · mail radar")], "active")

    def test_watch_add_reconciles_authority_on_pending_replacement(self):
        with patch.object(jobs, "IDLE_WAIT_S", 0):
            old, new = self.pending_watch()
            cfg = cli.phconf.load()
            cfg["slack"]["enabled"] = False
            cli.write_config(cfg)
            self.assertEqual(self.command(ADD + ["--then", "tell"])[0], 0)
            self.assertIn(old, self.run.live)
            self.assertFalse(new in self.run.live and self.run.states.get(new, "active") == "active")

    def test_reapplying_revoked_authority_updates_the_same_watch_job(self):
        cfg = cli.phconf.load()
        cfg = cli.apply_answers(cfg, {"mobile.enabled": True, "authority.auto": ["alarms", "reminders"]})
        cli.write_config(cfg)
        self.assertEqual(self.command(ADD + ["--then", "alarm", "--urgent"])[0], 0)
        jid = self.run.job("Proactive · watch: Card drop")
        self.assertIn("ea_reminders.py alarm", self.run.prompt_of(jid))

        cfg = cli.phconf.load()
        cfg["authority"]["never"].append("alarms")
        say = self.run.job("Proactive · say")
        self.run.calls.clear()
        jobs.apply(cfg, run=self.run)
        self.assertEqual([a[2:4] for a in self.run.calls],
                         [["list", "--all"], ["pause", jid], ["edit", say], ["list", "--all"],
                          ["edit", jid], ["list", "--all"], ["resume", jid], ["list", "--all"]])
        sibling_edit = self.run.calls[2]
        self.assertFalse({"--schedule", "--timezone"}.intersection(sibling_edit[4:]), sibling_edit)
        self.assertEqual(self.run.states.get(say, "active"), "active")
        self.assertEqual(self.run.job("Proactive · watch: Card drop"), jid)
        argv = self.run.live[jid]
        updated = argv[argv.index("--prompt") + 1]
        self.assertNotIn("ea_reminders.py alarm", updated)
        self.assertIn("ea_reminders.py add", updated)

    def test_act_requires_an_exact_action_and_other_modes_reject_it(self):
        for flags in (["--then", "act"], ["--then", "tell", "--act", "open an issue"]):
            with self.subTest(flags=flags):
                self.assertNotEqual(self.command(ADD + flags)[0], 0)
        self.assertEqual(json.loads(self.config.read_text())["watches"], [])
        self.assertEqual(self.run.calls, [])

    def make_prompt(self, then, *, act=None, urgent=False, auto=(), never=()):
        cfg = cli.apply_answers(cli.base_config("custom"), {"mobile.enabled": True, "authority.auto": list(auto)})
        cfg["authority"]["never"].extend(never)
        watch = {"name": "Card drop", "schedule": "*/30 * * * *", "check": "the shop page",
                 "when": "preorders open", "then": then, "urgent": urgent}
        if act is not None:
            watch["act"] = act
        return jobs.prompt_for(jobs.watch_job(watch), cfg)

    def test_watch_prompt_has_condition_key_item_kind_and_silence(self):
        prompt = self.make_prompt("tell")
        for text in ("desk-contract.md", "the shop page", "preorders open", "watch-card-drop-<what changed>",
                     "kind digest", "urgent: false", "[]", "compose --job watch-card-drop", "[SILENT]"):
            self.assertIn(text, prompt)

    def test_alarm_prompt_falls_back_to_reminder_without_alarm_authority(self):
        prompt = self.make_prompt("alarm", urgent=True, auto=("reminders",))
        self.assertIn("Fall back to a reminder", prompt)
        self.assertIn("ea_reminders.py add", prompt)
        self.assertIn("urgent: true", prompt)
        active = self.make_prompt("alarm", urgent=True, auto=("alarms",))
        self.assertIn("phconf.allowed('alarms')", active)
        self.assertIn("ea_reminders.py alarm --key <key> --at", active)
        self.assertNotIn("ea_reminders.py alarm", prompt)
        self.assertNotIn("ea_reminders.py alarm", self.make_prompt("alarm", auto=("alarms",)))
        denied = self.make_prompt("alarm", urgent=True, auto=("alarms", "reminders"), never=("alarms",))
        self.assertNotIn("ea_reminders.py alarm", denied)
        self.assertIn("ea_reminders.py add", denied)

    def test_act_prompt_is_draft_first_unless_watch_actions_are_auto(self):
        exact = "copy the update to my private notes"
        for prompt in (self.make_prompt("act", act=exact),
                       self.make_prompt("act", act=exact, auto=("watch_actions",), never=("watch_actions",))):
            self.assertIn(exact, prompt)
            self.assertIn("prepare a draft and ask", prompt)
        prompt = self.make_prompt("act", act=exact, auto=("watch_actions",))
        self.assertIn(exact, prompt)
        self.assertIn("Do only the exact action", prompt)
        self.assertIn("phconf.allowed('watch_actions')", prompt)
        self.assertIn("Never pay or enter card details", prompt)
