"""Configured authority is enforced where the action executes, not only in prompts or the CLI.

For every write the plugin can make on its own (phone reminders, alarms, trip leave reminders, calendar blocks,
calendar alerts, calendar deletes): 'never' and 'ask' put nothing on the fake phone bridge or the fake calendar
service, and only 'auto' does. There is no owner approval path in the scripts: an ask-first action the owner says
yes to in chat goes through Hermes's own tools, never these scripts. A malformed authority fails closed. A retry
that is no longer allowed ends in a terminal 'not_allowed' state. The health watchdog matches the jobs the
installer keeps by identity. The installed runtime carries a stamp, so a stale copy is reported and refreshed.
"""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

try:  # `discover -s tests -t .` imports the package; plain `discover -s tests` does not.
    from .test_desk import FakeStore, OfflineTestCase, phconf, reminders
    from .test_trip import FakeCalendar
except ImportError:
    from test_desk import FakeStore, OfflineTestCase, phconf, reminders
    from test_trip import FakeCalendar
import ea_calendar_block as calendar  # noqa: E402  (test_desk puts runtime/ on sys.path)
import ea_close  # noqa: E402
import ea_trip as trip  # noqa: E402
import ph_doctor  # noqa: E402
import ph_jobs  # noqa: E402

HERE = Path(__file__).resolve().parents[1]
RUNTIME = HERE / "runtime"
PHONE = ("reminders", "alarms")
MALFORMED = {
    "never as a string": {"auto": ["reminders", "alarms", "calendar_blocks"], "never": "calendar_blocks"},
    "auto as a string": {"auto": "reminders"},
    "auto with a non-string": {"auto": ["reminders", 7]},
    "ask_first as a string": {"auto": ["reminders", "alarms", "calendar_blocks"], "ask_first": "send_email"},
    "authority not an object": "reminders",
}


def authority(level: str, action: str) -> dict:
    return {"auto": [action] if level == "auto" else [], "ask_first": [action] if level == "ask" else [],
            "never": [action] if level == "never" else []}


class PhoneAuthority(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(reminders, "PLUGIN", self.tmp))
        (self.tmp / "bridge.py").touch()
        self.freeze_datetime(reminders)
        self.store = FakeStore()
        self.bridge = self.enterContext(patch.object(reminders, "bridge", return_value=(None, self.store)))
        # Calendar alerts are covered below; here any attempt is recorded, never executed.
        self.alerts = self.enterContext(patch.object(reminders, "calendar_cli", return_value={"status": "created"}))
        self.at = (self.clock + timedelta(hours=2)).isoformat()

    def set_level(self, action, level, **extra):
        self.configure(reminders={"enabled": True}, **{"authority": authority(level, action), **extra})

    def add(self, *extra, key="k", code=0):
        return json.loads(self.cli(reminders, "add", "--key", key, "--title", "Call Alex", "--due", self.at, *extra,
                                   code=code))

    def alarm(self, *extra, key="a", code=0):
        return json.loads(self.cli(reminders, "alarm", "--key", key, "--at", self.at, "--label", "Leave", *extra,
                                   code=code))

    def refusals(self):
        return [r for r in self.rows(self.tmp / reminders.RUN_LOG) if "not_allowed" in r]

    def test_reminder_never_and_ask_reach_nothing_exit_5_and_auto_queues(self):
        for level in ("never", "ask"):
            with self.subTest(level=level):
                self.set_level("reminders", level)
                self.assertEqual(self.add(code=5), {"status": "not_allowed", "action": "reminders", "authority": level})
                self.assertEqual(reminders.add("k2", "Call Alex"),
                                 {"status": "not_allowed", "action": "reminders", "authority": level})
        self.assertEqual(self.store.requests, [])
        self.bridge.assert_not_called()
        self.alerts.assert_not_called()
        self.set_level("reminders", "auto")
        self.assertEqual(self.add()["status"], "queued")
        (op, args, _key, _ttl), = self.store.requests
        self.assertEqual((op, args["title"]), ("reminders.create", "Call Alex"))

    def test_alarm_never_and_ask_reach_nothing_exit_5_are_logged_and_auto_queues(self):
        for level in ("never", "ask"):
            with self.subTest(level=level):
                self.set_level("alarms", level)
                self.assertEqual(self.alarm(code=5), {"status": "not_allowed", "action": "alarms", "authority": level})
                self.assertEqual(reminders.enqueue({"operation": "alarms.create", "key": "x", "at": self.at,
                                                    "label": "L"})["status"], "not_allowed")
        self.assertEqual(self.store.requests, [])
        self.alerts.assert_not_called()
        # The pre-check refusal in alarm() is logged like a reminder refusal (and enqueue's own, for the direct call).
        self.assertEqual([(r["not_allowed"], r["authority"]) for r in self.refusals()],
                         [("a", "never"), ("x", "never"), ("a", "ask"), ("x", "ask")])
        self.set_level("alarms", "auto")
        self.assertEqual(self.alarm()["status"], "queued")
        self.assertEqual([r[0] for r in self.store.requests], ["alarms.create"])

    def test_no_approval_path_exists(self):
        # The owner-approval text bypass is gone: no flag, no cron check, no logged approval kind.
        self.set_level("reminders", "ask")
        approval = self.tmp / "yes.txt"
        approval.write_text("1 yes, remind me", encoding="utf-8")
        for cmd in (["add", "--key", "k", "--title", "T"], ["alarm", "--key", "a", "--at", self.at, "--label", "L"]):
            for flag in ("--approval-file", "--approval"):
                with self.subTest(cmd=cmd[0], flag=flag), self.assertRaises(SystemExit) as exit_:
                    with redirect_stdout(io.StringIO()), patch.object(sys, "stderr", io.StringIO()):
                        self.cli(reminders, *cmd, flag, str(approval))
                self.assertEqual(exit_.exception.code, 2)
        self.assertFalse(hasattr(phconf, "CRON_ENV") or hasattr(phconf, "scheduled_run"))
        self.assertEqual(phconf.authorize("reminders"), (False, "ask"))
        with self.assertRaises(TypeError):
            phconf.authorize("reminders", "yes")  # noqa: the second positional (approval text) no longer exists
        with self.assertRaises(TypeError):
            reminders.add("k", "T", approval="yes")
        self.assertEqual(self.store.requests, [])

    def test_denied_retry_becomes_terminal_not_allowed(self):
        for level in ("ask", "never"):
            with self.subTest(level=level):
                key = f"r-{level}"
                self.set_level("reminders", "auto")
                rid = self.add(key=key)["request_id"]
                self.set_level("reminders", level)          # downgraded after it was queued
                self.store.results[rid] = {"status": "expired"}
                requests = len(self.store.requests)
                self.cli(reminders, "tick")
                item = self.read_json(self.tmp / reminders.OUTBOX)[key]
                self.assertEqual((item["status"], item["authority"]), ("not_allowed", level))
                self.assertEqual(self.read_json(self.tmp / reminders.LEDGER)[key]["reminder_status"], "not_allowed")
                lookups = len(self.store.lookups)
                self.cli(reminders, "tick")                 # never re-polled, never re-requested, logged once
                self.cli(reminders, "tick")
                self.assertEqual(len(self.store.lookups), lookups)
                self.assertEqual(len(self.store.requests), requests)
                logged = [(r["action"], r["authority"], r.get("retry")) for r in self.refusals()
                          if r["not_allowed"] == key]
                self.assertEqual(logged, [("reminders", level, True)])
                self.assertEqual(ea_close.find(key), [])     # no longer listed as open

    def test_item_queued_under_the_old_approval_bug_is_stopped(self):
        # An outbox item written by the removed approval path (status queued, an "approval" record, ask first).
        self.set_level("reminders", "ask")
        self.store.results["old-1"] = {"status": "expired"}
        (self.tmp / reminders.OUTBOX).write_text(json.dumps({"old": {
            "key": "old", "title": "Old reminder", "due": self.at, "attempts": 1, "request_id": "old-1",
            "created": self.clock.isoformat(), "status": "queued",
            "approval": {"text": "yes", "at": self.clock.isoformat()}}}))
        self.assertEqual([r["key"] for r in ea_close.find("old")], ["old"])
        self.cli(reminders, "tick")
        self.cli(reminders, "tick")
        self.assertEqual(self.store.requests, [])
        self.assertEqual(self.store.lookups, ["old-1"])
        self.assertEqual(self.read_json(self.tmp / reminders.OUTBOX)["old"]["status"], "not_allowed")
        self.assertEqual(ea_close.find("old"), [])
        self.assertEqual(len([r for r in self.refusals() if r.get("retry")]), 1)

    def test_retry_continues_while_auto(self):
        self.set_level("reminders", "auto")
        rid = self.add()["request_id"]
        self.store.results[rid] = {"status": "expired"}
        self.cli(reminders, "tick")
        self.assertEqual(len(self.store.requests), 2)
        self.assertEqual(self.read_json(self.tmp / reminders.OUTBOX)["k"]["status"], "queued")

    def test_adopted_tool_reminder_is_not_recreated_without_authority(self):
        self.set_level("reminders", "ask")
        self.store.results["tool-1"] = {"status": "pending", "operation": "reminders.create", "arguments": {"title": "T"}}
        self.cli(reminders, "adopt", "--key", "t", "--request-id", "tool-1")
        self.store.results["tool-1"] = {"status": "expired"}
        self.cli(reminders, "tick")
        self.assertEqual(self.store.requests, [])
        self.assertEqual(self.read_json(self.tmp / reminders.OUTBOX)["t"]["status"], "not_allowed")

    def test_revoked_required_authority_stops_a_trip_retry(self):
        self.configure(reminders={"enabled": True}, authority={"auto": ["reminders", "leave_time"]})
        rid = reminders.add("trip:z", "Leave now", due=self.at, required_authority=("leave_time",))["request_id"]
        self.configure(reminders={"enabled": True}, authority={"auto": ["reminders"]})
        self.store.results[rid] = {"status": "expired"}
        self.cli(reminders, "tick")
        item = self.read_json(self.tmp / reminders.OUTBOX)["trip:z"]
        self.assertEqual((item["status"], item["error"]), ("not_allowed", "retry refused: leave_time is ask"))
        self.assertEqual(len(self.store.requests), 1)

    def test_trip_leave_reminder_needs_reminders_too(self):
        for level in ("never", "ask"):
            self.configure(reminders={"enabled": True},
                           authority={"auto": ["leave_time"], "ask_first": ["reminders"] if level == "ask" else [],
                                      "never": ["reminders"] if level == "never" else []})
            receipt = reminders.add("trip:x", "Leave now", due=self.at, priority=None,
                                    required_authority=("leave_time", "reminders"))
            self.assertEqual(receipt["status"], "not_allowed")
        self.configure(reminders={"enabled": True}, authority={"auto": ["reminders"]})  # leave_time missing
        self.assertEqual(reminders.add("trip:y", "Leave now", due=self.at, required_authority=("leave_time",)),
                         {"status": "not_allowed", "action": "leave_time", "authority": "ask"})
        self.assertEqual(self.store.requests, [])

    def test_time_critical_reminder_gets_no_calendar_alert_without_calendar_authority(self):
        for level in ("ask", "never"):
            self.configure(reminders={"enabled": True}, calendar={"enabled": True},
                           authority={"auto": ["reminders"], "ask_first": ["calendar_blocks"] if level == "ask" else [],
                                      "never": ["calendar_blocks"] if level == "never" else []})
            self.assertEqual(reminders.add(f"crit-{level}", "Now", due=self.at, priority="high")["status"], "queued")
        self.alerts.assert_not_called()

    def test_malformed_authority_refuses_every_phone_action(self):
        for name, policy in MALFORMED.items():
            with self.subTest(policy=name):
                self.configure(reminders={"enabled": True}, calendar={"enabled": True}, authority=policy)
                for action in phconf.AUTO_ACTIONS:
                    self.assertEqual(phconf.allowed(action), "never", action)
                self.assertEqual(self.add(key=f"m-{len(name)}", code=5)["authority"], "never")
                self.assertEqual(self.alarm(key=f"a-{len(name)}", code=5)["authority"], "never")
                self.assertFalse(phconf.calendar_alerts_on())
        self.assertEqual(self.store.requests, [])
        self.alerts.assert_not_called()


class PhoneExitCode(OfflineTestCase):
    def test_refused_add_and_alarm_exit_5_end_to_end(self):
        home = self.tmp / "home"
        (home / "plugins" / reminders.BRIDGE_PLUGIN).mkdir(parents=True)
        (home / "plugins" / reminders.BRIDGE_PLUGIN / "bridge.py").write_text("raise SystemExit('bridge touched')\n")
        cfg = self.tmp / "phone.json"
        cfg.write_text(json.dumps({"reminders": {"enabled": True}, "authority": {"ask_first": ["reminders", "alarms"]}}))
        env = {**os.environ, "PROACTIVE_HERMES_CONFIG": str(cfg), "EA_STATE_DIR": str(self.tmp / "state"),
               "HERMES_HOME": str(home)}
        at = (datetime.now(phconf.tz()) + timedelta(hours=2)).isoformat()
        for argv in (["add", "--key", "k", "--title", "T"], ["alarm", "--key", "a", "--at", at, "--label", "L"]):
            with self.subTest(cmd=argv[0]):
                proc = subprocess.run([sys.executable, str(RUNTIME / "ea_reminders.py"), *argv],
                                      capture_output=True, text=True, env=env, timeout=60)
                self.assertEqual(proc.returncode, 5, proc.stderr)
                self.assertEqual(json.loads(proc.stdout)["status"], "not_allowed")
        logged = self.rows(self.tmp / "state" / reminders.RUN_LOG)
        self.assertEqual([r["not_allowed"] for r in logged if "not_allowed" in r], ["k", "a"])


class CalendarAuthority(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(calendar, "LOG", self.tmp / "actions_log.jsonl"))
        self.enterContext(patch.object(calendar, "TZ", phconf.tz()))
        self.fake = FakeCalendar()
        self.service = self.enterContext(patch.object(calendar, "service", return_value=self.fake))
        self.start = datetime(2030, 4, 2, 9, tzinfo=phconf.tz())

    def set_level(self, level):
        self.configure(calendar={"enabled": True}, authority=authority(level, "calendar_blocks"))

    def run_cli(self, *args):
        output = io.StringIO()
        with patch.object(sys, "argv", ["ea_calendar_block.py", *args]), redirect_stdout(output), \
                patch.object(sys, "stderr", io.StringIO()):
            try:
                calendar.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        text = output.getvalue().strip()
        return (json.loads(text.splitlines()[-1]) if text else None), code

    def create(self, *extra):
        return self.run_cli("create", "--key", "ea:focus", "--title", "Focus", "--start", self.start.isoformat(),
                            "--end", (self.start + timedelta(minutes=30)).isoformat(), *extra)

    def alert(self, *extra):
        return self.run_cli("alert", "--key", "ea:alert:k", "--title", "Leave", "--at", self.start.isoformat(), *extra)

    def test_never_and_ask_create_and_alert_nothing(self):
        for level in ("never", "ask"):
            self.set_level(level)
            for name, run in (("create", self.create), ("alert", self.alert)):
                with self.subTest(level=level, cmd=name):
                    result, code = run()
                    self.assertEqual((result["status"], result["authority"], code), ("not_allowed", level, 5))
            res, code = calendar.make_alert(self.fake, "ea:alert:z", "T", self.start)
            self.assertEqual((res["status"], code), ("not_allowed", 5))
        self.service.assert_not_called()
        self.assertEqual(self.fake.inserts, [])
        self.assertEqual({r["kind"] for r in self.rows(calendar.LOG)}, {"calendar_not_allowed"})

    def test_auto_creates_block_and_alert(self):
        self.set_level("auto")
        self.assertEqual(self.create()[0]["status"], "created")
        self.assertEqual(self.alert()[0]["status"], "created")
        self.assertEqual(len(self.fake.inserts), 2)
        rows = self.rows(calendar.LOG)
        self.assertEqual([r["kind"] for r in rows], ["calendar_block", "calendar_alert"])
        self.assertFalse(any("approved_by" in r or "approval" in r for r in rows))

    def test_no_approval_flag(self):
        self.set_level("ask")
        approval = self.tmp / "yes.txt"
        approval.write_text("yes", encoding="utf-8")
        for run in (self.create, self.alert):
            self.assertEqual(run("--approval-file", str(approval)), (None, 2))
        self.service.assert_not_called()
        self.assertEqual(self.fake.inserts, [])

    def test_malformed_authority_refuses_every_write_and_delete(self):
        for name, policy in MALFORMED.items():
            with self.subTest(policy=name):
                self.configure(calendar={"enabled": True}, authority=policy)
                for run in (self.create, self.alert, lambda: self.run_cli("delete", "--key", "ea:focus")):
                    result, code = run()
                    self.assertEqual((result["status"], result["authority"], code), ("not_allowed", "never", 5))
        self.service.assert_not_called()

    def test_delete_honours_never_only(self):
        self.set_level("never")
        result, code = self.run_cli("delete", "--key", "ea:focus")
        self.assertEqual((result["status"], code), ("not_allowed", 5))
        self.service.assert_not_called()
        self.set_level("ask")  # cleanup of the plugin's own event stays possible under ask first
        self.assertEqual(self.run_cli("delete", "--key", "ea:focus")[0]["status"], "not_found")

    def test_trip_block_goes_through_the_same_boundary(self):
        self.set_level("never")
        args = type("A", (), {"title": "Train", "kind": "train", "place": "Berlin", "depart": "x"})()
        window = {"start": self.start.isoformat(), "end": (self.start + timedelta(hours=1)).isoformat()}
        self.assertEqual(trip.create_block("ea:trip:x", args, window)["status"], "not_allowed")
        self.service.assert_not_called()

    def test_alert_subprocess_refuses_before_google(self):
        # End to end, the way ea_reminders.calendar_cli runs it: ask first means no Google client is ever built
        # (there is none to build here, so reaching it would print status error, exit 4).
        cfg = self.tmp / "cal.json"
        cfg.write_text(json.dumps({"calendar": {"enabled": True}, "authority": {"ask_first": ["calendar_blocks"]}}))
        env = {**os.environ, "PROACTIVE_HERMES_CONFIG": str(cfg), "EA_STATE_DIR": str(self.tmp / "state"),
               "HERMES_HOME": str(self.tmp / "home")}
        proc = subprocess.run([sys.executable, str(RUNTIME / "ea_calendar_block.py"), "alert", "--key", "ea:alert:k",
                               "--title", "T", "--at", self.start.isoformat()],
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 5, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], "not_allowed")


class MalformedAuthoritySetupAndDoctor(OfflineTestCase):
    def test_well_formed_policies_are_unchanged(self):
        import cli
        for preset in ("custom", "juan"):
            self.assertEqual(phconf.authority_problem(cli.base_config(preset)), "")
        cfg = phconf.merge(phconf.DEFAULTS, {"authority": {"auto": ["reminders"], "never": ["alarms"]}})
        self.assertEqual([phconf.allowed(a, cfg=cfg) for a in ("reminders", "alarms", "calendar_blocks")],
                         ["auto", "never", "ask"])

    def test_doctor_reports_malformed_authority(self):
        self.configure(authority={"never": "calendar_blocks"})
        self.assertEqual(ph_doctor.check_authority(),
                         (False, "authority config malformed: authority.never must be a list of action names"))
        self.configure(authority={"auto": ["reminders"]})
        self.assertEqual(ph_doctor.check_authority(), (True, ""))
        self.configure(authority={"never": "calendar_blocks"})
        with patch.object(ph_doctor, "check_gateway", return_value=(True, "running")), \
                patch.object(ph_doctor, "check_bot_chat", return_value=(True, "found")), \
                patch.object(ph_doctor, "check_mobile", return_value=(True, "")):
            row = ph_doctor.report()["checks"]["authority"]
        self.assertFalse(row["ok"])
        self.assertIn("authority config malformed", row["note"])
        self.assertIn("hermes proactive setup", row["fix"])

    def test_setup_refuses_to_write_a_malformed_policy(self):
        import argparse
        import cli
        cfg_path = Path(os.environ["PROACTIVE_HERMES_CONFIG"])
        before = cfg_path.read_text()
        for name, policy in MALFORMED.items():
            with self.subTest(policy=name):
                with self.assertRaisesRegex(ValueError, "authority config malformed"):
                    cli.apply_answers(cli.base_config("custom"), {"authority": policy})
                bad = cli.base_config("custom")
                bad["authority"] = policy
                with self.assertRaisesRegex(ValueError, "authority config malformed"):
                    cli.write_config(bad)
                answers = self.tmp / "answers.json"
                answers.write_text(json.dumps({"owner.name": "Alex", "authority": policy}))
                args = argparse.Namespace(proactive_command="setup", preset="custom", answers=str(answers),
                                          dry_run=False)
                with patch.object(sys, "stderr", io.StringIO()) as err, redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.handle(args), 2)
                self.assertIn("authority config malformed", err.getvalue())
        self.assertEqual(cfg_path.read_text(), before)


class ManagedJobIdentity(OfflineTestCase):
    """The watchdog expects the installer's own set of jobs, by identity (name or recorded id), not by count."""

    def installed(self, cfg):
        import jobs
        with patch.object(jobs, "_load_registry", return_value={}), patch.object(jobs.phconf, "CFG", cfg):
            rows = jobs.apply(cfg, dry_run=True, run=lambda *a, **k: self.fail("dry run must not call cron"))
        return [r["job"] for r in rows if r["action"] != "skip"]

    def health(self, cfg):
        with patch.object(phconf, "CFG", cfg):
            spec = importlib.util.spec_from_file_location("authority_test_health", RUNTIME / "ea_health.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        return mod

    def configs(self):
        import cli
        custom = cli.base_config("custom")
        juan = cli.base_config("juan")
        connected = cli.base_config("custom")
        connected["calendar"]["enabled"] = True
        connected["slack"]["enabled"] = True
        connected["features"].update(slack_sweep=True, lookahead=True, work_recap=True)  # daily_call stays off
        connected["watches"] = [{"name": "Card drop", "schedule": "*/15 * * * *", "check": "c", "when": "w",
                                 "then": "tell"}]
        return {"custom defaults": (custom, 7), "juan preset": (juan, 12), "custom connected": (connected, 11)}

    def test_job_registry_and_names_match_templates(self):
        import jobs
        self.assertEqual([j["key"] for j in jobs.JOBS], list(ph_jobs.MANAGED_KEYS))
        self.assertEqual([jobs.PREFIX + j["name"] for j in jobs.JOBS], [ph_jobs.job_name(k) for k in ph_jobs.MANAGED_KEYS])

    def test_expected_jobs_equal_installed_jobs(self):
        for name, (cfg, count) in self.configs().items():
            with self.subTest(config=name):
                installed = self.installed(cfg)
                self.assertEqual(len(installed), count)
                self.assertEqual([ph_jobs.job_name(k) for k in ph_jobs.expected(cfg)], installed)

    def test_watchdog_is_quiet_with_every_job_and_names_the_missing_one_despite_a_duplicate(self):
        for name, (cfg, _count) in self.configs().items():
            with self.subTest(config=name):
                health = self.health(cfg)
                live = [{"id": f"id{i}", "name": n} for i, n in enumerate(self.installed(cfg))]
                with patch.object(phconf, "CFG", cfg):
                    health.check_jobs(live)
                    self.assertEqual(health.faults, {})
                    # Same count as before: tick is gone, health is there twice. A count check saw nothing.
                    broken = [j for j in live if j["name"] != ph_jobs.job_name("tick")]
                    broken.append(next(j for j in live if j["name"] == ph_jobs.job_name("health")))
                    self.assertEqual(len(broken), len(live))
                    health.check_jobs(broken + [{"name": "Other job"}])
                self.assertEqual(list(health.faults.values()), [("desk", "desk job missing: Proactive \u00b7 tick")])

    def test_recorded_job_id_counts_as_identity(self):
        import cli
        cfg = cli.base_config("custom")
        health = self.health(cfg)
        live = [{"id": f"id{i}", "name": n} for i, n in enumerate(self.installed(cfg))]
        renamed = [dict(j, name="Proactive \u00b7 my tick") if j["name"] == ph_jobs.job_name("tick") else j
                   for j in live]
        tick_id = next(j["id"] for j in live if j["name"] == ph_jobs.job_name("tick"))
        with patch.object(phconf, "CFG", cfg):
            health.check_jobs(renamed)
            self.assertEqual(list(health.faults.values()), [("desk", "desk job missing: Proactive \u00b7 tick")])
            health.faults.clear()
            registry = phconf.data_dir() / "jobs.json"
            registry.parent.mkdir(parents=True, exist_ok=True)
            registry.write_text(json.dumps({"tick": {"id": tick_id, "spec": "x"}}))
            health.check_jobs(renamed)
        self.assertEqual(health.faults, {})


class RuntimeStamp(OfflineTestCase):
    """install_runtime stamps the copy; doctor reports a stale one; plugin load refreshes it."""

    def install(self):
        import jobs
        with patch.object(jobs.phconf, "CFG", phconf.load()):
            return jobs.install_runtime(phconf.load())

    def plugin_module(self):
        spec = importlib.util.spec_from_file_location("ph_plugin_under_test", HERE / "__init__.py",
                                                      submodule_search_locations=[str(HERE)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        self.addCleanup(sys.modules.pop, spec.name, None)
        self.addCleanup(sys.modules.pop, spec.name + ".jobs", None)
        spec.loader.exec_module(mod)
        return mod

    def test_missing_then_ok_then_stale(self):
        self.assertEqual(phconf.runtime_status(HERE)[0], "missing")
        dest = self.install()
        self.assertEqual(dest, phconf.scripts_dir())
        stamp = json.loads((dest / phconf.RUNTIME_STAMP).read_text())
        self.assertEqual(stamp, {"digest": phconf.runtime_digest(HERE), "source": str(HERE)})
        self.assertEqual(phconf.runtime_status(HERE), ("ok", "installed runtime matches the plugin"))
        self.assertEqual(phconf.runtime_status()[0], "ok")          # from the stamp's source, as the copy runs it
        (dest / "ea_reminders.py").write_text("# an older release\n")
        self.assertEqual(phconf.runtime_status(HERE), ("stale", "runtime out of date: run hermes proactive setup"))
        self.install()
        self.assertEqual(phconf.runtime_status(HERE)[0], "ok")
        # An install from before stamps existed (no stamp file) is stale too.
        (dest / phconf.RUNTIME_STAMP).unlink()
        self.assertEqual(phconf.runtime_status(HERE)[0], "stale")

    def test_plugin_source_change_is_stale(self):
        source = self.tmp / "plugin"
        shutil.copytree(RUNTIME, source / "runtime", ignore=shutil.ignore_patterns("__pycache__"))
        (source / phconf.CONTRACT).parent.mkdir(parents=True)
        shutil.copy2(HERE / phconf.CONTRACT, source / phconf.CONTRACT)
        self.install()
        self.assertEqual(phconf.runtime_status(source)[0], "ok")
        (source / phconf.CONTRACT).write_text("a new contract\n")   # a contract change alone counts
        self.assertEqual(phconf.runtime_status(source)[0], "stale")

    def test_doctor_reports_stale_runtime(self):
        dest = self.install()
        self.assertEqual(ph_doctor.check_runtime(), (True, "installed runtime matches the plugin"))
        (dest / "phconf.py").write_text("# old\n")
        self.assertEqual(ph_doctor.check_runtime(), (False, "runtime out of date: run hermes proactive setup"))
        with patch.object(ph_doctor, "check_gateway", return_value=(True, "running")), \
                patch.object(ph_doctor, "check_bot_chat", return_value=(True, "found")), \
                patch.object(ph_doctor, "check_mobile", return_value=(True, "")):
            row = ph_doctor.report()["checks"]["runtime"]
        self.assertEqual((row["ok"], row["note"]), (False, "runtime out of date: run hermes proactive setup"))
        self.assertIn("hermes proactive setup", row["fix"])

    def test_plugin_load_refreshes_only_an_installed_stale_runtime(self):
        plugin = self.plugin_module()
        cfg_path = phconf.config_path()
        cfg_path.unlink()
        self.assertEqual(plugin._refresh_runtime(), "not set up")
        self.assertFalse(phconf.scripts_dir().exists())
        cfg_path.write_text(json.dumps({"owner": {"name": "Alex"}}))
        self.assertEqual(plugin._refresh_runtime(), "missing")      # first install stays with setup
        self.assertFalse((phconf.scripts_dir() / "phconf.py").exists())
        dest = self.install()
        self.assertEqual(plugin._refresh_runtime(), "ok")
        (dest / "ea_health.py").write_text("# old\n")
        (dest / phconf.RUNTIME_STAMP).write_text(json.dumps({"digest": "old", "source": str(HERE)}))
        self.assertEqual(plugin._refresh_runtime(), "updated")
        self.assertEqual((dest / "ea_health.py").read_bytes(), (RUNTIME / "ea_health.py").read_bytes())
        self.assertEqual(phconf.runtime_status(HERE)[0], "ok")
        self.assertEqual(sorted(p.name for p in dest.iterdir() if p.name.startswith(".") and p.name != phconf.RUNTIME_STAMP),
                         [])                                       # no temp files left behind
