"""Authority decisions and watch lifecycle without any real cron or device calls."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import cli
import jobs
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


class WatchTests(unittest.TestCase):
    def setUp(self):
        home = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"HERMES_HOME": home}, clear=True))
        self.home = Path(home)
        self.run = FakeCron()
        self.enterContext(patch.object(jobs, "_plugin_ensure", self.run.ensure))
        self.enterContext(patch.object(jobs, "api_problem", return_value=None))
        cfg = cli.base_config("custom")
        cfg["features"] = dict.fromkeys(cfg["features"], False)
        self.config = cli.write_config(cfg)
        self.registry = self.home / "plugin-data" / "proactive-hermes" / "jobs.json"
        self.parser = argparse.ArgumentParser()
        cli.setup_parser(self.parser)

    def command(self, argv):
        args = self.parser.parse_args(argv)
        out, err = StringIO(), StringIO()
        apply = jobs.apply

        def apply_fake(cfg, **kwargs):
            return apply(cfg, run=self.run, **kwargs)

        with patch.object(jobs, "apply", side_effect=apply_fake), redirect_stdout(out), redirect_stderr(err):
            result = cli.handle(args)
        return result, out.getvalue() + err.getvalue()

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
        self.assertEqual([a[2] for a in self.run.calls], ["list"])
        self.run.live["abcdef123456"] = ["unrelated job"]
        self.assertEqual(self.command(["watch", "remove", "--name", "Card drop"])[0], 0)
        self.assertEqual(set(self.run.live), {"abcdef123456"})   # no watch and nothing else: no say job either
        self.assertEqual(json.loads(self.registry.read_text()), {})
        self.assertEqual(json.loads(self.config.read_text())["watches"], [])

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

    def test_settled_watch_is_fenced_before_unrelated_radar_archive_fails(self):
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
        copy = jobs._keep_history
        def fail_radar(key, jid):
            if jid == old:
                self.assertNotEqual(jobs._job_states(self.run).get(watch), "active")
                raise OSError("fixture radar archive failure")
            return copy(key, jid)
        with patch.object(jobs, "_keep_history", side_effect=fail_radar):
            rc, output = self.command(["watch", "remove", "--name", "Card drop"])
        self.assertEqual(rc, 1)
        self.assertIn("fixture radar archive failure", output)
        self.assertEqual(cli.phconf.load()["watches"], [])
        self.assertNotEqual(jobs._job_states(self.run).get(watch), "active")
        self.assertNotIn("watch:Card drop", jobs._load_registry())

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
        self.run.calls.clear()
        jobs.apply(cfg, run=self.run)
        self.assertEqual([a[2] for a in self.run.calls if a[2] != "list"], ["pause", "edit", "resume"])
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
