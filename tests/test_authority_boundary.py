"""Configured authority is enforced where the action executes, not only in prompts or the CLI.

For every write the plugin can make on its own (phone reminders, alarms, trip leave reminders, calendar blocks,
calendar alerts, calendar deletes): 'never' and 'ask' put nothing on the fake phone bridge or the fake calendar
service, and only 'auto' does. There is no owner approval path in the scripts: an ask-first action the owner says
yes to in chat goes through Hermes's own tools, never these scripts. A malformed authority fails closed. A retry
that is no longer allowed ends in a terminal 'not_allowed' state. The health watchdog matches the jobs the
installer keeps by identity. The installed runtime carries a stamp, so a stale copy is reported (only setup installs it).
"""
import importlib.util
import io
import itertools
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
    "authority null": None,
    "tier null": {"auto": None},
    "tier string": {"auto": ["reminders"], "never": "payments"},
}


def profile_config(home: Path, config: dict) -> Path:
    """Write CONFIG where a runtime script started with HERMES_HOME=HOME reads it: the profile's own plugin-data."""
    data = home / "plugin-data" / "proactive-hermes"
    data.mkdir(parents=True, exist_ok=True)
    (data / "config.json").write_text(json.dumps(config))
    return data / "state"


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
        state = profile_config(home, {"reminders": {"enabled": True},
                                      "authority": {"ask_first": ["reminders", "alarms"]}})
        env = {**os.environ, "HERMES_HOME": str(home)}
        at = (datetime.now(phconf.tz()) + timedelta(hours=2)).isoformat()
        for argv in (["add", "--key", "k", "--title", "T"], ["alarm", "--key", "a", "--at", at, "--label", "L"]):
            with self.subTest(cmd=argv[0]):
                proc = subprocess.run([sys.executable, str(RUNTIME / "ea_reminders.py"), *argv],
                                      capture_output=True, text=True, env=env, timeout=60)
                self.assertEqual(proc.returncode, 5, proc.stderr)
                self.assertEqual(json.loads(proc.stdout)["status"], "not_allowed")
        logged = self.rows(state / reminders.RUN_LOG)
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

    def test_delete_needs_auto_cleanup(self):
        # Deleting its own block or alert is cleanup: only `cleanup` automatic allows it, whatever calendar_blocks
        # and delete_data say. Unlisted is ask.
        cases = [
            ({"auto": ["calendar_blocks"], "never": ["cleanup"]}, "never"),
            ({"auto": ["calendar_blocks"], "ask_first": ["cleanup"]}, "ask"),
            ({"auto": ["calendar_blocks"], "never": []}, "ask"),
            ({"auto": ["cleanup"], "ask_first": ["cleanup"]}, "ask"),
        ]
        for policy, level in cases:
            with self.subTest(policy=policy):
                self.configure(calendar={"enabled": True}, authority=policy)
                result, code = self.run_cli("delete", "--key", "ea:focus")
                self.assertEqual((result, code), ({"status": "not_allowed", "action": "cleanup", "authority": level,
                                                   "key": "ea:focus"}, 5))
        self.service.assert_not_called()
        self.assertEqual([(r["action"], r["authority"]) for r in self.rows(calendar.LOG)],
                         [("cleanup", level) for _policy, level in cases])
        self.configure(calendar={"enabled": True}, authority={"auto": ["cleanup"], "never": ["calendar_blocks",
                                                                                           "delete_data"]})
        self.assertEqual(self.run_cli("delete", "--key", "ea:focus")[0]["status"], "not_found")
        self.fake.fake_events["E1"] = {"id": "E1", "summary": "Focus", "extendedProperties": {"private": {
            calendar.TAG: "ea:focus", "created_by": calendar.CREATED_BY}}}
        self.assertEqual(self.run_cli("delete", "--key", "ea:focus"), ({"status": "deleted", "key": "ea:focus",
                                                                         "id": "E1"}, 0))
        self.assertEqual(self.fake.deletes, [{"calendarId": calendar.CAL, "eventId": "E1"}])

    def test_trip_block_goes_through_the_same_boundary(self):
        self.set_level("never")
        args = type("A", (), {"title": "Train", "kind": "train", "place": "Berlin", "depart": "x"})()
        window = {"start": self.start.isoformat(), "end": (self.start + timedelta(hours=1)).isoformat()}
        self.assertEqual(trip.create_block("ea:trip:x", args, window)["status"], "not_allowed")
        self.service.assert_not_called()

    def test_alert_subprocess_refuses_before_google(self):
        # End to end, the way ea_reminders.calendar_cli runs it: ask first means no Google client is ever built
        # (there is none to build here, so reaching it would print status error, exit 4).
        profile_config(self.tmp / "home", {"calendar": {"enabled": True},
                                           "authority": {"ask_first": ["calendar_blocks"]}})
        env = {**os.environ, "HERMES_HOME": str(self.tmp / "home")}
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
        cfg_path = phconf.config_path()
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


    def test_null_and_malformed_policy_render_deny_all_and_setup_rejects_them(self):
        import cli
        import jobs
        watch = {"name": "w", "schedule": "*/15 * * * *", "check": "c", "when": "w", "then": "act", "act": "x"}
        for name in ("authority null", "tier null", "tier string"):
            policy = MALFORMED[name]
            with self.subTest(policy=name):
                cfg = cli.base_config("custom")
                cfg["authority"], cfg["watches"] = policy, [watch]
                self.assertIn("authority config malformed", jobs.variables(cfg)["AUTO"])
                for job in jobs.JOBS + [jobs.watch_job(watch)]:
                    jobs.prompt_for(job, cfg)                      # renders, never crashes
                self.assertIn("Watch actions are not authorized", jobs.prompt_for(jobs.watch_job(watch), cfg))
                self.configure(authority=policy)
                self.assertEqual([phconf.allowed(a) for a in ("reminders", "cleanup")], ["never", "never"])
                self.assertTrue(ph_doctor.check_authority()[1].startswith("authority config malformed"))
        for answers in ({"authority": None}, {"authority": {"never": None}}, {"authority.auto": None}):
            with self.subTest(answers=answers), self.assertRaisesRegex(ValueError, "authority config malformed"):
                cli.apply_answers(cli.base_config("custom"), answers)


class Precedence(OfflineTestCase):
    """never > ask_first > auto, for every combination of the three levels, at every boundary."""

    LEVELS = ("auto", "ask_first", "never")

    @staticmethod
    def expected(tiers) -> str:
        return "never" if "never" in tiers else "ask" if "ask_first" in tiers else "auto" if "auto" in tiers else "ask"

    def combos(self):
        for n in range(len(self.LEVELS) + 1):
            yield from itertools.combinations(self.LEVELS, n)

    def test_every_combination_for_every_automatic_action(self):
        seen = set()
        for action in sorted(phconf.AUTO_ACTIONS):
            for tiers in self.combos():
                with self.subTest(action=action, tiers=tiers):
                    cfg = phconf.merge(phconf.DEFAULTS, {"authority": {t: [action] for t in self.LEVELS} | {
                        t: [] for t in self.LEVELS if t not in tiers}})
                    level = self.expected(tiers)
                    self.assertEqual(phconf.allowed(action, cfg=cfg), level)
                    self.assertEqual(phconf.authorize(action, cfg=cfg), (level == "auto", level))
                    seen.add(tiers)
        self.assertEqual(len(seen), 8)

    def test_an_action_that_cannot_be_automatic_is_never_auto(self):
        for tiers in self.combos():
            cfg = phconf.merge(phconf.DEFAULTS, {"authority": {t: ["send_email"] if t in tiers else []
                                                               for t in self.LEVELS}})
            self.assertEqual(phconf.allowed("send_email", cfg=cfg),
                             "never" if "never" in tiers else "ask")

    def test_ask_first_beats_auto_at_the_script_boundary(self):
        self.enterContext(patch.object(reminders, "PLUGIN", self.tmp))
        (self.tmp / "bridge.py").touch()
        store = FakeStore()
        self.enterContext(patch.object(reminders, "bridge", return_value=(None, store)))
        self.enterContext(patch.object(calendar, "service", side_effect=AssertionError("no Google client")))
        self.enterContext(patch.object(calendar, "LOG", self.tmp / "actions_log.jsonl"))
        self.configure(reminders={"enabled": True}, calendar={"enabled": True},
                       authority={"auto": ["reminders", "alarms", "calendar_blocks"],
                                  "ask_first": ["reminders", "alarms", "calendar_blocks"]})
        self.assertEqual(reminders.add("k", "T"), {"status": "not_allowed", "action": "reminders", "authority": "ask"})
        at = (datetime.now(phconf.tz()) + timedelta(hours=1)).isoformat()
        self.assertEqual(reminders.alarm("a", at, "L")["authority"], "ask")
        self.assertEqual(calendar.refused("ea:x")["authority"], "ask")
        self.assertFalse(phconf.calendar_alerts_on())
        self.assertEqual(store.requests, [])


class SetupAuthorityValidation(OfflineTestCase):
    """Setup validates every authority input before merging: nothing is silently dropped."""

    def setUp(self):
        super().setUp()
        import cli
        self.cli = cli

    def apply(self, answers, preset="custom"):
        return self.cli.apply_answers(self.cli.base_config(preset), answers)

    def test_dotted_levels_are_applied_not_dropped(self):
        cfg = self.apply({"authority.auto": "calendar_blocks", "authority.ask_first": "send_email,bookings",
                          "authority.never": ["payments", "delete_data"]})
        self.assertEqual(cfg["authority"], {"auto": ["calendar_blocks"], "ask_first": ["send_email", "bookings"],
                                            "never": ["payments", "delete_data"]})
        nested = self.apply({"authority": {"never": ["payments", "calendar_blocks"]}})
        self.assertEqual(nested["authority"]["never"], ["payments", "calendar_blocks"])
        self.assertEqual(phconf.allowed("calendar_blocks", cfg=nested), "never")

    def test_unknown_levels_actions_and_keys_are_errors(self):
        cases = [
            ({"authority.sometimes": "reminders"}, "unknown answer key 'authority.sometimes'"),
            ({"authority": {"sometimes": ["reminders"]}}, "unknown approval level 'sometimes'"),
            ({"authority.never": "reminders,teleport"}, "authority.never: invalid action\\(s\\) 'teleport'"),
            ({"authority": {"ask_first": ["teleport"]}}, "authority.ask_first: invalid action\\(s\\) 'teleport'"),
            ({"authority.auto": "send_email"}, "authority.auto: invalid action\\(s\\) 'send_email'"),
            ({"features.weekly_review": False}, "unknown answer key\\(s\\) 'features.weekly_review'"),
            ({"authority.never": "payments", "authority": {"never": ["bookings"]}}, "given twice"),
        ]
        for answers, message in cases:
            with self.subTest(answers=answers), self.assertRaisesRegex(ValueError, message):
                self.apply(answers)

    def test_conflicting_levels_are_errors_for_every_pair(self):
        for first, second in itertools.combinations(("auto", "ask_first", "never"), 2):
            with self.subTest(levels=(first, second)), \
                    self.assertRaisesRegex(ValueError, f"'reminders' is in both {first} and {second}"):
                self.apply({f"authority.{first}": "reminders", f"authority.{second}": "reminders",
                            "mobile.enabled": True})
        # Against a level the preset already holds: Juan's setup has reminders automatic.
        with self.assertRaisesRegex(ValueError, "'reminders' is in both auto and never"):
            self.apply({"authority.never": "payments,reminders", "mobile.enabled": True}, preset="juan")
        with self.assertRaisesRegex(ValueError, "'send_email' is in both ask_first and never"):
            self.apply({"authority": {"never": ["send_email"]}})

    def test_setup_cli_refuses_and_writes_nothing(self):
        import argparse
        before = phconf.config_path().read_text()
        answers = self.tmp / "answers.json"
        answers.write_text(json.dumps({"owner.name": "Alex", "authority.never": "calendar_blocks",
                                       "authority.auto": "calendar_blocks"}))
        args = argparse.Namespace(proactive_command="setup", preset="custom", answers=str(answers), dry_run=False)
        with patch.object(sys, "stderr", io.StringIO()) as err, redirect_stdout(io.StringIO()):
            self.assertEqual(self.cli.handle(args), 2)
        self.assertIn("'calendar_blocks' is in both auto and never", err.getvalue())
        self.assertEqual(phconf.config_path().read_text(), before)

    def test_write_config_refuses_a_conflict(self):
        cfg = self.cli.base_config("custom")
        cfg["authority"]["auto"] = ["reminders"]
        cfg["authority"]["ask_first"].append("reminders")
        with self.assertRaisesRegex(ValueError, "each action belongs to exactly one level"):
            self.cli.write_config(cfg)

    def test_vocabulary_matches_the_labels(self):
        import jobs
        self.assertEqual(set(jobs.LABELS), phconf.ACTIONS)
        for preset in ("custom", "juan"):
            self.assertEqual(phconf.authority_errors(self.cli.base_config(preset)), [])


class NoEnvironmentOverride(OfflineTestCase):
    """The authority file and the state dir are the active profile's own; the caller's environment cannot move them."""

    def test_config_and_state_ignore_override_variables(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "config.json").write_text(json.dumps({"authority": {"auto": ["reminders"]}}))
        with patch.dict(os.environ, PROACTIVE_HERMES_CONFIG=str(elsewhere / "config.json"),
                        EA_STATE_DIR=str(elsewhere), HERMES_AGENT_ROOT=str(elsewhere)):
            self.assertEqual(phconf.config_path(), phconf.hermes_home() / "plugin-data" / "proactive-hermes" / "config.json")
            self.assertEqual(phconf.state_dir(), self.tmp)
            self.assertEqual(phconf.load()["authority"]["auto"], [])
            self.assertNotEqual(phconf.agent_root(), elsewhere)

    def test_a_script_ignores_an_override_config(self):
        # End to end: a forged config that makes reminders automatic is never read.
        home = self.tmp / "home"
        (home / "plugins" / reminders.BRIDGE_PLUGIN).mkdir(parents=True)
        (home / "plugins" / reminders.BRIDGE_PLUGIN / "bridge.py").write_text("raise SystemExit('bridge touched')\n")
        state = profile_config(home, {"reminders": {"enabled": True}})
        forged = self.tmp / "forged.json"
        forged.write_text(json.dumps({"reminders": {"enabled": True}, "authority": {"auto": ["reminders"]}}))
        env = {**os.environ, "HERMES_HOME": str(home), "PROACTIVE_HERMES_CONFIG": str(forged),
               "EA_STATE_DIR": str(self.tmp / "forged-state")}
        proc = subprocess.run([sys.executable, str(RUNTIME / "ea_reminders.py"), "add", "--key", "k", "--title", "T"],
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual((proc.returncode, json.loads(proc.stdout)["authority"]), (5, "ask"), proc.stderr)
        self.assertTrue((state / reminders.RUN_LOG).is_file())
        self.assertFalse((self.tmp / "forged-state").exists())

    def test_runtime_scripts_read_only_justified_variables(self):
        import re
        allowed = {"HERMES_HOME", "DESK_SAY_DISABLE_DOOR"}
        for f in sorted(RUNTIME.glob("*.py")):
            text = f.read_text()
            read = set(re.findall(r"environ\.get\(\s*[\"']([A-Z_]+)", text)) | set(
                re.findall(r"getenv\(\s*[\"']([A-Z_]+)", text)) | set(re.findall(r"environ\[\s*[\"']([A-Z_]+)[\"']\s*\](?!\s*=)", text))
            with self.subTest(script=f.name):
                self.assertLessEqual(read, allowed)


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
            # Renamed without the desk prefix: its recorded id still counts (ids are matched before the filter).
            health.check_jobs([dict(j, name="My own tick") if j["id"] == tick_id else j for j in live])
        self.assertEqual(health.faults, {})

    def record(self, **ids):
        registry = phconf.data_dir() / "jobs.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps({key: {"id": jid, "spec": "x"} for key, jid in ids.items()}))

    def test_one_live_row_never_satisfies_two_expected_jobs(self):
        import cli
        cfg = cli.base_config("custom")
        health = self.health(cfg)
        live = [{"id": f"id{i}", "name": n} for i, n in enumerate(self.installed(cfg))]
        ids = {ph_jobs.job_name(k): j["id"] for k in ph_jobs.expected(cfg) for j in live if j["name"] == ph_jobs.job_name(k)}
        tick, watchdog = ph_jobs.job_name("tick"), ph_jobs.job_name("health")
        with patch.object(phconf, "CFG", cfg):
            # The health job is gone and a stale registry points health at the tick job's id: the tick row is
            # claimed once (by id, for health), so tick itself is missing.
            self.record(health=ids[tick], tick=ids[tick])
            health.check_jobs([j for j in live if j["name"] != watchdog])
            self.assertEqual(list(health.faults.values()), [("desk", f"desk job missing: {tick}")])
            health.faults.clear()
            # The tick job renamed to the health job's name, health gone: the row counts for tick (its recorded id),
            # never also for health by the name it carries.
            self.record(tick=ids[tick], health=ids[watchdog])
            renamed = [dict(j, name=watchdog) if j["name"] == tick else j for j in live if j["name"] != watchdog]
            health.check_jobs(renamed)
            self.assertEqual(list(health.faults.values()), [("desk", f"desk job missing: {watchdog}")])
            health.faults.clear()
            # Two expected jobs, one live row carrying both the name of one and no recorded id: one is missing.
            self.record()
            health.check_jobs([j for j in live if j["name"] != watchdog] + [dict(j, id="other") for j in live
                                                                             if j["name"] == tick])
            self.assertEqual(list(health.faults.values()), [("desk", f"desk job missing: {watchdog}")])
            health.faults.clear()
            # Every job present, ids recorded or not: quiet.
            health.check_jobs(live)
            self.record(**{k: ids[ph_jobs.job_name(k)] for k in ph_jobs.expected(cfg)})
            health.check_jobs(live)
        self.assertEqual(health.faults, {})


class RuntimeStamp(OfflineTestCase):
    """install_runtime stamps the copy (desk contract included); doctor reports a stale or drifted one; only setup
    installs it, one complete generation at a time, only inside this profile's Hermes home."""

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

    def generations(self):
        gens = phconf.scripts_dir().parent / phconf.GENERATIONS
        return sorted(p.name for p in gens.iterdir() if not p.name.startswith(".")) if gens.is_dir() else []

    def test_missing_then_ok_then_stale(self):
        self.assertEqual(phconf.runtime_status(HERE)[0], "missing")
        dest = self.install()
        self.assertEqual(dest, phconf.scripts_dir())
        stamp = json.loads((dest / phconf.RUNTIME_STAMP).read_text())
        self.assertEqual({k: stamp[k] for k in ("digest", "source")},
                         {"digest": phconf.runtime_digest(HERE), "source": str(HERE)})
        self.assertEqual(sorted(stamp["files"]), sorted([p.name for p in RUNTIME.glob("*.py")] + ["desk-contract.md"]))
        self.assertEqual(stamp["files"]["desk-contract.md"],
                         phconf.file_digest((dest / "desk-contract.md").read_bytes()))
        self.assertEqual(phconf.runtime_status(HERE), ("ok", "installed runtime matches the plugin"))
        self.assertEqual(phconf.runtime_status()[0], "ok")          # from the stamp's source, as the copy runs it
        (dest / "ea_reminders.py").write_text("# an older release\n")
        self.assertEqual(phconf.runtime_status(HERE), ("stale", "runtime out of date: run hermes proactive setup"))
        self.install()
        self.assertEqual(phconf.runtime_status(HERE)[0], "ok")
        # An install from before stamps existed (no stamp file) is stale too.
        (dest / phconf.RUNTIME_STAMP).unlink()
        self.assertEqual(phconf.runtime_status(HERE)[0], "stale")

    def test_contract_missing_or_changed_is_reported(self):
        dest = self.install()
        (dest / "desk-contract.md").write_text("an edited contract\n")
        self.assertEqual(phconf.runtime_status(HERE),
                         ("stale", "installed desk-contract.md changed since setup: run hermes proactive setup"))
        (dest / "desk-contract.md").unlink()
        self.assertEqual(phconf.runtime_status(HERE), ("stale", phconf.NO_CONTRACT))
        self.assertEqual(ph_doctor.check_runtime(), (False, phconf.NO_CONTRACT))
        # Without a plugin checkout to compare with, the stamp alone still catches it.
        with patch.object(phconf, "runtime_stamp", return_value={**phconf.runtime_stamp(), "source": ""}):
            self.assertEqual(phconf.runtime_status(), ("stale", phconf.NO_CONTRACT))
        self.install()
        self.assertEqual(phconf.runtime_status(HERE)[0], "ok")
        with patch.object(phconf, "runtime_stamp", return_value={**phconf.runtime_stamp(), "source": ""}):
            self.assertEqual(phconf.runtime_status()[0], "unknown")
            (dest / "desk-contract.md").write_text("drift\n")
            self.assertEqual(phconf.runtime_status()[0], "stale")

    def test_stamp_without_a_file_list_is_stale(self):
        dest = self.install()
        stamp = json.loads((dest / phconf.RUNTIME_STAMP).read_text())
        stamp.pop("files")
        (dest / phconf.RUNTIME_STAMP).write_text(json.dumps(stamp))
        self.assertEqual(phconf.runtime_status(HERE), ("stale", phconf.STALE))

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

    def test_plugin_load_never_writes_the_runtime(self):
        from unittest.mock import MagicMock
        plugin = self.plugin_module()
        self.assertFalse(hasattr(plugin, "_refresh_runtime"))
        dest = self.install()
        (dest / "ea_health.py").write_text("# old\n")
        with patch.object(plugin, "_start_mail_push"):
            plugin.register(MagicMock())
        self.assertEqual((dest / "ea_health.py").read_text(), "# old\n")   # only setup installs it
        self.assertEqual(ph_doctor.check_runtime(), (False, phconf.STALE))

    def test_each_install_is_one_generation_swapped_atomically(self):
        dest = self.install()
        self.assertTrue(dest.is_symlink())
        first = os.readlink(dest)
        self.assertTrue(first.startswith(phconf.GENERATIONS + "/"))
        self.assertEqual(self.install(), dest)                     # nothing changed: no new generation
        self.assertEqual((os.readlink(dest), len(self.generations())), (first, 1))
        (dest / "ea_desk.py").write_text("# drifted\n")
        self.install()
        second = os.readlink(dest)
        self.assertNotEqual(second, first)
        # The old generation stays complete (a job that started on it keeps it), the new one is complete too.
        old = dest.parent / first
        self.assertEqual((old / "ea_desk.py").read_text(), "# drifted\n")
        self.assertEqual(sorted(p.name for p in old.iterdir()), sorted(p.name for p in (dest.parent / second).iterdir()))
        self.assertEqual(phconf.runtime_status(HERE)[0], "ok")
        (dest / "ea_desk.py").write_text("# drifted again\n")
        self.install()
        third = os.readlink(dest)
        # The active generation and the two before it stay, so a job still running on one can finish.
        self.assertEqual(self.generations(), sorted(g.split("/")[1] for g in (first, second, third)))
        (dest / "ea_desk.py").write_text("# drifted a third time\n")
        self.install()
        self.assertEqual(self.generations(), sorted(g.split("/")[1] for g in (second, third, os.readlink(dest))))

    def test_a_job_resolves_one_generation_for_its_whole_run(self):
        dest = self.install()
        go = self.tmp / "go"
        script = ("import sys, time, phconf\nfrom pathlib import Path\nprint(sys.path[0], flush=True)\n"
                  f"while not Path({str(go)!r}).exists(): time.sleep(0.05)\n"
                  "import ea_health\nprint(ea_health.HERE, flush=True)\n")
        (dest / "probe.py").write_text(script)
        proc = subprocess.Popen([sys.executable, str(dest / "probe.py")], stdout=subprocess.PIPE, text=True,
                                env=os.environ.copy())
        started_in = proc.stdout.readline().strip()
        (dest / "ea_desk.py").write_text("# drifted\n")
        self.install()                                             # swapped while the job runs
        go.touch()
        out, _ = proc.communicate(timeout=30)
        self.assertNotEqual(str(dest.resolve()), started_in)
        self.assertEqual(out.strip(), started_in)                  # the lazy import came from the same generation

    def test_interrupted_copy_leaves_the_active_generation_intact(self):
        import jobs
        dest = self.install()
        active = os.readlink(dest)
        before = {p.name: p.read_bytes() for p in dest.iterdir() if p.is_file()}
        (dest / "ea_desk.py").write_text("# drifted\n")
        before["ea_desk.py"] = b"# drifted\n"
        real, calls = jobs._write_new, []

        def flaky(path, data, mode):
            calls.append(path.name)
            if len(calls) == 5:
                raise OSError("disk full")
            real(path, data, mode)

        with patch.object(jobs, "_write_new", flaky), self.assertRaises(OSError):
            self.install()
        self.assertEqual(os.readlink(dest), active)
        self.assertEqual({p.name: p.read_bytes() for p in dest.iterdir() if p.is_file()}, before)
        self.assertEqual(self.generations(), [active.split("/")[1]])   # the partial copy is gone
        self.assertEqual(sorted(p.name for p in (dest.parent / phconf.GENERATIONS).iterdir() if p.name.startswith(".")),
                         [".install.lock"])
        self.install()
        self.assertEqual(phconf.runtime_status(HERE)[0], "ok")

    def test_legacy_directory_is_kept_and_foreign_files_survive_upgrades(self):
        legacy = phconf.scripts_dir()
        legacy.mkdir(parents=True)
        (legacy / "phconf.py").write_text("# legacy copy\n")
        (legacy / "my_notes.txt").write_text("mine\n")
        out = io.StringIO()
        with redirect_stdout(out):
            dest = self.install()
        self.assertTrue(dest.is_symlink())
        self.assertEqual(phconf.runtime_status(HERE)[0], "ok")
        kept = [p for p in dest.parent.iterdir() if p.name.startswith(f"{phconf.PLUGIN}.legacy-")]
        self.assertEqual(len(kept), 1)
        self.assertEqual({p.name: p.read_text() for p in kept[0].iterdir()},
                         {"phconf.py": "# legacy copy\n", "my_notes.txt": "mine\n"})   # nothing deleted
        self.assertIn(str(kept[0]), out.getvalue())
        self.assertEqual([p.name for p in dest.parent.iterdir() if p.name.startswith(".proactive-hermes.")], [])
        (dest / "my_tool.py").write_text("# mine\n")             # added inside the active generation
        for n in range(2):                                         # two upgrades
            previous = os.readlink(dest)
            (dest / "ea_desk.py").write_text(f"# drifted {n}\n")
            with redirect_stdout(io.StringIO()) as quiet:
                self.install()
            self.assertEqual(quiet.getvalue(), "")                  # the legacy move is mentioned once
            self.assertNotEqual(os.readlink(dest), previous)
            self.assertTrue((dest.parent / previous / "phconf.py").is_file())   # the previous generation stays
            self.assertEqual((dest / "my_notes.txt").read_text(), "mine\n")
            self.assertEqual((dest / "my_tool.py").read_text(), "# mine\n")
        self.assertEqual(phconf.runtime_status(HERE)[0], "ok")
        self.assertEqual((kept[0] / "my_notes.txt").read_text(), "mine\n")

    def test_symlinked_destination_is_refused_and_nothing_is_written(self):
        import argparse
        import cli
        import jobs
        outside = self.root / "outside"
        outside.mkdir()
        home = phconf.hermes_home()
        home.mkdir(parents=True, exist_ok=True)
        cases = {
            "scripts dir": (home / "scripts", outside),
            "generations dir": (home / "scripts" / phconf.GENERATIONS, outside),
            "pointer elsewhere": (phconf.scripts_dir(), outside),
            "pointer to a foreign generation": (phconf.scripts_dir(), Path(phconf.GENERATIONS) / "../../outside"),
        }
        answers = self.tmp / "answers.json"
        answers.write_text(json.dumps({"owner.name": "Changed"}))
        before = phconf.config_path().read_text()
        for name, (link, target) in cases.items():
            with self.subTest(case=name):
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target)
                try:
                    with self.assertRaisesRegex(jobs.UnsafeRuntimeDestination, "refusing to install the runtime"):
                        self.install()
                    ok, note = ph_doctor.check_runtime()             # doctor names the blocking symlink
                    self.assertFalse(ok)
                    self.assertIn("setup would refuse: refusing to install the runtime: ", note)
                    self.assertIn(str(link), note)
                    args = argparse.Namespace(proactive_command="setup", preset="custom", answers=str(answers),
                                              dry_run=False)
                    with patch.object(sys, "stderr", io.StringIO()) as err, redirect_stdout(io.StringIO()):
                        self.assertEqual(cli.handle(args), 1)
                    self.assertIn(str(link), err.getvalue())
                    self.assertEqual(phconf.config_path().read_text(), before)   # refused before config.json
                    self.assertEqual(list(outside.iterdir()), [])
                finally:
                    link.unlink()
