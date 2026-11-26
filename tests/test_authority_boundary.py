"""Configured authority is enforced where the action executes, not only in prompts or the CLI.

For every write the plugin can make on its own (phone reminders, alarms, trip leave reminders, calendar blocks,
calendar alerts, calendar deletes): 'never' and 'ask' put nothing on the fake phone bridge or the fake calendar
service; 'auto' does; the owner's explicit approval lets an ask-first action through, is recorded, never beats
'never' and is refused inside a scheduled run. The health watchdog expects exactly the jobs the installer keeps.
"""
import io
import json
import os
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
import ea_trip as trip  # noqa: E402
import ph_jobs  # noqa: E402

HERE = Path(__file__).resolve().parents[1]
RUNTIME = HERE / "runtime"
YES = "1 yes, remind me"
NOT_CRON = {phconf.CRON_ENV: ""}
IN_CRON = {phconf.CRON_ENV: "1"}


def authority(level: str, action: str) -> dict:
    return {"auto": [action] if level == "auto" else [], "ask_first": [action] if level == "ask" else [],
            "never": [action] if level == "never" else []}


class PhoneAuthority(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, NOT_CRON))
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

    def add(self, *extra):
        return json.loads(self.cli(reminders, "add", "--key", "k", "--title", "Call Alex", "--due", self.at, *extra))

    def alarm(self, *extra):
        return json.loads(self.cli(reminders, "alarm", "--key", "a", "--at", self.at, "--label", "Leave", *extra))

    def approval_file(self, text=YES):
        path = self.tmp / "approval.txt"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def audit(self):
        return [r for r in self.rows(self.tmp / "actions_log.jsonl") if r.get("kind") == "owner_approval"]

    def test_reminder_never_and_ask_reach_nothing_and_auto_queues(self):
        for level in ("never", "ask"):
            with self.subTest(level=level):
                self.set_level("reminders", level)
                self.assertEqual(self.add(), {"status": "not_allowed", "action": "reminders", "authority": level})
                self.assertEqual(reminders.add("k2", "Call Alex"),
                                 {"status": "not_allowed", "action": "reminders", "authority": level})
        self.assertEqual(self.store.requests, [])
        self.bridge.assert_not_called()
        self.alerts.assert_not_called()
        self.set_level("reminders", "auto")
        self.assertEqual(self.add()["status"], "queued")
        (op, args, _key, _ttl), = self.store.requests
        self.assertEqual((op, args["title"]), ("reminders.create", "Call Alex"))
        self.assertEqual(self.audit(), [])

    def test_alarm_never_and_ask_reach_nothing_and_auto_queues(self):
        for level in ("never", "ask"):
            with self.subTest(level=level):
                self.set_level("alarms", level)
                self.assertEqual(self.alarm(), {"status": "not_allowed", "action": "alarms", "authority": level})
                self.assertEqual(reminders.enqueue({"operation": "alarms.create", "key": "x", "at": self.at,
                                                    "label": "L"})["status"], "not_allowed")
        self.assertEqual(self.store.requests, [])
        self.alerts.assert_not_called()
        self.set_level("alarms", "auto")
        self.assertEqual(self.alarm()["status"], "queued")
        self.assertEqual([r[0] for r in self.store.requests], ["alarms.create"])

    def test_owner_approval_lets_ask_first_through_once_and_is_recorded(self):
        for action, run in (("reminders", self.add), ("alarms", self.alarm)):
            with self.subTest(action=action):
                self.set_level(action, "ask")
                receipt = run("--approval-file", self.approval_file())
                self.assertEqual(receipt["status"], "queued")
                item = self.read_json(self.tmp / reminders.OUTBOX)["k" if action == "reminders" else "a"]
                self.assertEqual(item["approval"]["text"], YES)
        self.assertEqual([(r["action"], r["approval"]) for r in self.audit()], [("reminders", YES), ("alarms", YES)])
        self.assertEqual(len(self.store.requests), 2)
        self.alerts.assert_not_called()  # calendar_blocks is not automatic: the approval covers the phone action only

    def test_approval_never_beats_never_blank_or_a_scheduled_run(self):
        for action, run in (("reminders", self.add), ("alarms", self.alarm)):
            self.set_level(action, "never")
            self.assertEqual(run("--approval-file", self.approval_file())["authority"], "never")
            self.set_level(action, "ask")
            self.assertEqual(run("--approval-file", self.approval_file("  \n"))["authority"], "ask")
            with patch.dict(os.environ, IN_CRON):
                self.assertEqual(run("--approval-file", self.approval_file())["authority"],
                                 "approval_in_scheduled_run")
        self.assertEqual(self.store.requests, [])
        self.assertEqual(self.audit(), [])

    def test_retry_follows_the_same_rule(self):
        self.set_level("reminders", "auto")
        rid = self.add()["request_id"]
        self.set_level("reminders", "ask")       # downgraded to ask: an unapproved item is not re-requested
        self.store.results[rid] = {"status": "expired"}
        self.cli(reminders, "tick")
        self.assertEqual(len(self.store.requests), 1)
        self.set_level("reminders", "ask")
        approved = json.loads(self.cli(reminders, "add", "--key", "ok", "--title", "T", "--due", self.at,
                                       "--approval-file", self.approval_file()))["request_id"]
        self.store.results[approved] = {"status": "expired"}
        self.cli(reminders, "tick")              # approved under ask: the owner's yes still covers the retry
        self.assertEqual(len(self.store.requests), 3)
        self.store.results[f"request-{len(self.store.requests)}"] = {"status": "expired"}
        self.set_level("reminders", "never")     # never stops even an approved item
        self.cli(reminders, "tick")
        self.assertEqual(len(self.store.requests), 3)

    def test_adopted_tool_reminder_is_not_recreated_without_authority(self):
        self.set_level("reminders", "ask")
        self.store.results["tool-1"] = {"status": "pending", "operation": "reminders.create", "arguments": {"title": "T"}}
        self.cli(reminders, "adopt", "--key", "t", "--request-id", "tool-1")
        self.store.results["tool-1"] = {"status": "expired"}
        self.cli(reminders, "tick")
        self.assertEqual(self.store.requests, [])

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


class CalendarAuthority(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, NOT_CRON))
        self.enterContext(patch.object(calendar, "LOG", self.tmp / "actions_log.jsonl"))
        self.enterContext(patch.object(calendar, "TZ", phconf.tz()))
        self.fake = FakeCalendar()
        self.service = self.enterContext(patch.object(calendar, "service", return_value=self.fake))
        self.start = datetime(2030, 4, 2, 9, tzinfo=phconf.tz())

    def set_level(self, level):
        self.configure(calendar={"enabled": True}, authority=authority(level, "calendar_blocks"))

    def run_cli(self, *args):
        output = io.StringIO()
        with patch.object(sys, "argv", ["ea_calendar_block.py", *args]), redirect_stdout(output):
            try:
                calendar.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return json.loads(output.getvalue().strip().splitlines()[-1]), code

    def create(self, *extra):
        return self.run_cli("create", "--key", "ea:focus", "--title", "Focus", "--start", self.start.isoformat(),
                            "--end", (self.start + timedelta(minutes=30)).isoformat(), *extra)

    def alert(self, *extra):
        return self.run_cli("alert", "--key", "ea:alert:k", "--title", "Leave", "--at", self.start.isoformat(), *extra)

    def approval_file(self, text=YES):
        path = self.tmp / "approval.txt"
        path.write_text(text, encoding="utf-8")
        return str(path)

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

    def test_auto_creates_block_and_alert(self):
        self.set_level("auto")
        self.assertEqual(self.create()[0]["status"], "created")
        self.assertEqual(self.alert()[0]["status"], "created")
        self.assertEqual(len(self.fake.inserts), 2)

    def test_owner_approval_for_ask_first_is_recorded_and_never_beats_never(self):
        self.set_level("ask")
        result, code = self.create("--approval-file", self.approval_file())
        self.assertEqual((result["status"], code), ("created", 0))
        rows = self.rows(calendar.LOG)
        self.assertEqual([(r["kind"], r.get("approval")) for r in rows if r["kind"] == "owner_approval"],
                         [("owner_approval", YES)])
        self.assertEqual([r.get("approved_by") for r in rows if r["kind"] == "calendar_block"], ["owner"])
        with patch.dict(os.environ, IN_CRON):
            self.assertEqual(self.alert("--approval-file", self.approval_file())[0]["authority"],
                             "approval_in_scheduled_run")
        self.set_level("never")
        self.assertEqual(self.create("--approval-file", self.approval_file())[0]["authority"], "never")
        self.assertEqual(len(self.fake.inserts), 1)

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
               "HERMES_HOME": str(self.tmp / "home"), phconf.CRON_ENV: ""}
        proc = subprocess.run([sys.executable, str(RUNTIME / "ea_calendar_block.py"), "alert", "--key", "ea:alert:k",
                               "--title", "T", "--at", self.start.isoformat()],
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 5, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], "not_allowed")


class ManagedJobCount(OfflineTestCase):
    """The watchdog's expected count is the installer's own set, for complete default and preset configs."""

    def installed(self, cfg):
        import jobs
        with patch.object(jobs, "_load_registry", return_value={}), patch.object(jobs.phconf, "CFG", cfg):
            rows = jobs.apply(cfg, dry_run=True, run=lambda *a, **k: self.fail("dry run must not call cron"))
        return [r["job"] for r in rows if r["action"] != "skip"]

    def health(self, cfg):
        import importlib.util
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

    def test_job_registry_matches_templates(self):
        import jobs
        self.assertEqual([j["key"] for j in jobs.JOBS], list(ph_jobs.MANAGED_KEYS))

    def test_expected_count_equals_installed_jobs(self):
        for name, (cfg, count) in self.configs().items():
            with self.subTest(config=name):
                installed = self.installed(cfg)
                self.assertEqual(len(installed), count)
                self.assertEqual(len(ph_jobs.expected(cfg)), count)
                # The raw count of true feature flags is what the watchdog used to compare against.
                self.assertNotEqual(sum(map(bool, cfg["features"].values())), count)

    def test_watchdog_is_quiet_at_the_installed_count_and_faults_below_it(self):
        for name, (cfg, count) in self.configs().items():
            with self.subTest(config=name):
                health = self.health(cfg)
                with patch.object(phconf, "CFG", cfg):
                    health.check_jobs([{"name": f"Proactive · job {i}"} for i in range(count)])
                    self.assertEqual(health.faults, {})
                    health.check_jobs([{"name": f"Proactive · job {i}"} for i in range(count - 1)]
                                      + [{"name": "Other job"}])
                self.assertEqual(list(health.faults.values()),
                                 [("desk", f"only {count - 1} desk jobs exist, expected {count}")])
