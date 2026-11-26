"""Hermetic coverage for the installer and the chat setup answer format."""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

import cli
import jobs


class FakeCron:
    """`hermes cron` and cron.plugin_api.ensure_named_job (the only public way to set enabled_toolsets)."""
    def __init__(self):
        self.calls = []
        self.live = {}
        self.toolsets = {}
        self.names = {}
        self.ensured = []
        self.next_id = 0

    def ensure(self, name, *, prompt, schedule, deliver=None, enabled_toolsets=None, home=None):
        assert home == os.environ["HERMES_HOME"], home
        self.ensured.append(name)
        for jid, known in self.names.items():
            if known == name and jid in self.live:
                return {"id": jid, "name": name, "enabled_toolsets": self.toolsets.get(jid)}
        self.next_id += 1
        jid = f"{self.next_id:012x}"
        self.live[jid] = ["ensure", "--name", name, "--deliver", deliver, "--", schedule, prompt]
        self.toolsets[jid], self.names[jid] = enabled_toolsets, name
        return {"id": jid, "name": name, "enabled_toolsets": enabled_toolsets}

    def option(self, jid, flag):
        argv = self.live[jid]
        return argv[argv.index(flag) + 1] if flag in argv else None

    def job(self, name):
        """The live job id named NAME."""
        (jid,) = [jid for jid in self.live if self.names.get(jid) == name]
        return jid

    def schedule_of(self, jid):
        argv = self.live[jid]
        return self.option(jid, "--schedule") if argv[2:3] == ["edit"] else argv[argv.index("--") + 1]

    def prompt_of(self, jid):
        argv = self.live[jid]
        return self.option(jid, "--prompt") if argv[2:3] == ["edit"] else argv[-1]

    def __call__(self, argv, *, env, **kwargs):
        assert argv[:2] == ["hermes", "cron"], argv
        assert env["HERMES_HOME"] == os.environ["HERMES_HOME"], argv
        self.calls.append(argv)
        action = argv[2]
        if action == "list":
            assert argv[3:] == ["--all"], argv
            stdout = "Scheduled Jobs (profile: custom)\n\n" + "".join(
                f"  {jid} [active]\n    Deliver:   local\n" for jid in self.live
            ) if self.live else "No scheduled jobs in profile 'custom'.\n"
        elif action == "create":
            self.next_id += 1
            jid = f"{self.next_id:012x}"
            self.live[jid] = argv
            self.names[jid] = argv[argv.index("--name") + 1]
            stdout = f"Created job: {jid}\n  Name: Proactive · fixture\n  ⚠  Scheduler is not ready.\n"
        elif action == "edit":
            jid = argv[3]
            self.live[jid] = argv   # an edit keeps enabled_toolsets (verified against Hermes)
            self.names[jid] = argv[argv.index("--name") + 1]
            stdout = f"Updated job: {jid}\n"
        elif action == "remove":
            jid = argv[3]
            del self.live[jid]
            stdout = f"Removed job: Proactive · fixture ({jid})\n"
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


class JobSetupTests(unittest.TestCase):
    def setUp(self):
        home = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"HERMES_HOME": home}, clear=True))
        self.home = Path(home)
        self.run = FakeCron()
        self.enterContext(patch.object(jobs, "_plugin_ensure", self.run.ensure))
        self.cfg = cli.base_config("custom")
        self.cfg["timezone"] = "Europe/Berlin"
        self.cfg["features"].update({key: key == "health" for key in self.cfg["features"]})

    SCRIPTS = {"Proactive · health": "ea_health.py", "Proactive · tick": "desk_tick.py", "Proactive · say": "say_job.py"}

    def by_name(self):
        return {self.run.option(jid, "--name"): jid for jid in self.run.live}

    def test_first_apply_creates_local_script_jobs_then_second_apply_is_read_only(self):
        self.assertEqual([r["action"] for r in jobs.apply(self.cfg, run=self.run)].count("create"), 3)
        for name, jid in self.by_name().items():
            script = self.SCRIPTS[name]
            argv = self.run.live[jid]
            for flag, value in (("--deliver", "bot-chat" if script == "say_job.py" else "local"),
                                ("--failure-deliver", "local"), ("--timezone", "Europe/Berlin"),
                                ("--catch-up", "off"), ("--script", f"proactive-hermes/{script}")):
                self.assertEqual(argv[argv.index(flag) + 1], value)
            self.assertIn("--no-wrap-response", argv)
            self.assertIn("--no-agent", argv)
            self.assertTrue((self.home / "scripts" / "proactive-hermes" / script).is_file())
        self.assertEqual(set(self.by_name()), set(self.SCRIPTS))
        self.assertEqual(self.run.ensured, [])   # no-agent jobs have no toolsets: plain `hermes cron create`
        self.assertTrue((self.home / "plugin-data" / "proactive-hermes" / "jobs.json").is_file())
        self.run.calls.clear()
        jobs.apply(self.cfg, run=self.run)
        self.assertFalse([argv for argv in self.run.calls if argv[2] != "list"])
        self.assertEqual(len(self.run.live), 3)

    def test_say_job_delivers_to_bot_chat_and_only_exists_when_something_speaks(self):
        jobs.apply(self.cfg, run=self.run)
        say = self.by_name()["Proactive · say"]
        self.assertEqual(self.run.option(say, "--deliver"), "bot-chat")
        self.assertEqual(self.run.option(say, "--failure-deliver"), "local")
        self.assertEqual(self.run.live[say][-2], "*/2 7-22 * * *")
        self.cfg["features"]["health"] = False
        jobs.apply(self.cfg, run=self.run)
        self.assertEqual(self.run.live, {})

    def test_agent_jobs_get_narrow_toolsets_through_ensure_named_job_then_edit(self):
        self.cfg["features"].update(mail_radar=True, promises=True, lookahead=True)
        self.cfg["mail"]["gmail_api"] = True
        jobs.apply(self.cfg, run=self.run)
        names = self.by_name()
        want = {"Proactive · mail radar": ["terminal", "file", "skills"],
                "Proactive · promise tracker": ["terminal", "file", "skills", "session_search"],
                "Proactive · look-ahead": ["terminal", "file", "skills", "web"]}
        for name, toolsets in want.items():
            jid = names[name]
            self.assertEqual(self.run.toolsets[jid], toolsets)
            self.assertNotIn("browser", self.run.toolsets[jid])
            # The edit right after ensure_named_job sets what ensure cannot: script, skills, timezone, wrap, catch-up.
            self.assertEqual(self.run.live[jid][2], "edit")
            self.assertEqual(self.run.option(jid, "--timezone"), "Europe/Berlin")
        self.assertEqual(self.run.option(names["Proactive · mail radar"], "--script"), "proactive-hermes/ea_radar_gate.py")
        self.assertCountEqual(self.run.ensured, list(want))
        for name in ("Proactive · health", "Proactive · tick", "Proactive · say"):
            self.assertIsNone(self.run.toolsets.get(names[name]))
        reg = json.loads((self.home / "plugin-data" / "proactive-hermes" / "jobs.json").read_text())
        self.assertEqual(reg["mail_radar"]["toolsets"], want["Proactive · mail radar"])

        # A prompt change edits in place: `hermes cron edit` keeps enabled_toolsets.
        self.cfg["owner"]["name"] = "Sam"
        self.run.calls.clear()
        self.run.ensured.clear()
        jobs.apply(self.cfg, run=self.run)
        self.assertEqual(self.by_name(), names)
        self.assertEqual(self.run.ensured, [])
        self.assertEqual(self.run.toolsets[names["Proactive · mail radar"]], want["Proactive · mail radar"])

    def test_upgrade_without_recorded_toolsets_replaces_the_agent_job(self):
        self.cfg["features"]["mail_radar"] = True
        self.cfg["mail"]["gmail_api"] = True
        jobs.apply(self.cfg, run=self.run)
        path = self.home / "plugin-data" / "proactive-hermes" / "jobs.json"
        reg = json.loads(path.read_text())
        old = reg["mail_radar"]["id"]
        self.run.toolsets[old] = None                     # a job from a release before toolsets
        del reg["mail_radar"]["toolsets"]
        path.write_text(json.dumps(reg))
        rows = {r["job"]: r["action"] for r in jobs.apply(self.cfg, run=self.run)}
        self.assertEqual(rows["Proactive · mail radar"], "replace")
        new = json.loads(path.read_text())["mail_radar"]["id"]
        self.assertNotEqual(new, old)
        self.assertNotIn(old, self.run.live)
        self.assertEqual(self.run.toolsets[new], ["terminal", "file", "skills"])

    def test_unrecorded_job_with_the_same_name_is_never_adopted(self):
        self.cfg["features"]["mail_radar"] = True
        self.cfg["mail"]["gmail_api"] = True
        self.run.live["abcdef123456"] = ["--name", "Proactive · mail radar"]
        self.run.names["abcdef123456"] = "Proactive · mail radar"
        with self.assertRaisesRegex(RuntimeError, "hermes cron remove abcdef123456"):
            jobs.apply(self.cfg, run=self.run)
        self.assertIn("abcdef123456", self.run.live)

    def test_disabled_feature_removes_managed_jobs_only(self):
        jobs.apply(self.cfg, run=self.run)
        managed = set(self.run.live)
        self.run.live["abcdef123456"] = ["unrelated job"]
        self.cfg["features"]["health"] = False
        rows = jobs.apply(self.cfg, run=self.run)
        self.assertEqual([row["action"] for row in rows].count("remove"), 3)
        self.assertEqual({a[3] for a in self.run.calls if a[2] == "remove"}, managed)
        self.assertEqual(jobs.remove_all(run=self.run), [])
        self.assertEqual(set(self.run.live), {"abcdef123456"})

    def test_schedule_and_timezone_change_edit_existing_jobs(self):
        jobs.apply(self.cfg, run=self.run)
        managed = set(self.run.live)
        self.cfg["schedule"]["health"] = "13 * * * *"
        self.cfg["timezone"] = "Asia/Tokyo"
        self.run.calls.clear()
        jobs.apply(self.cfg, run=self.run)
        edits = [argv for argv in self.run.calls if argv[2] == "edit"]
        self.assertEqual({argv[3] for argv in edits}, managed)
        self.assertTrue(any(argv[argv.index("--schedule") + 1] == "13 * * * *" for argv in edits))
        for argv in edits:
            self.assertEqual(argv[argv.index("--timezone") + 1], "Asia/Tokyo")
        self.assertEqual(set(self.run.live), managed)
        self.assertCountEqual(jobs.remove_all(run=self.run), ["health", "tick", "say"])
        self.assertEqual(self.run.live, {})

    def test_external_removal_recreates_only_missing_managed_job(self):
        jobs.apply(self.cfg, run=self.run)
        first, *rest = self.run.live
        del self.run.live[first]
        self.run.calls.clear()
        jobs.apply(self.cfg, run=self.run)
        self.assertEqual([a[2] for a in self.run.calls], ["list", "create"])
        for jid in rest:
            self.assertIn(jid, self.run.live)
        self.assertNotIn(first, self.run.live)

    def test_dry_run_and_legacy_registry_migration_keep_existing_ids(self):
        rows = jobs.apply(self.cfg, dry_run=True, run=self.run)
        self.assertEqual([row["action"] for row in rows].count("create"), 3)
        self.assertFalse(list(self.home.iterdir()))
        self.assertEqual(self.run.calls, [])

        jobs.apply(self.cfg, run=self.run)
        path = self.home / "plugin-data" / "proactive-hermes" / "jobs.json"
        ids = {key: entry["id"] for key, entry in json.loads(path.read_text()).items()}
        path.write_text(json.dumps(ids))
        self.run.calls.clear()
        jobs.apply(self.cfg, run=self.run)
        self.assertEqual([argv[2] for argv in self.run.calls], ["list", "edit", "edit", "edit"])
        self.assertEqual({key: entry["id"] for key, entry in json.loads(path.read_text()).items()}, ids)
        self.run.calls.clear()
        jobs.apply(self.cfg, run=self.run)
        self.assertEqual([argv[2] for argv in self.run.calls], ["list"])

    def test_parse_created_id(self):
        self.assertEqual(jobs._created_job_id("notice\nCreated job: 2ec52da04420\n  Name: x\n"), "2ec52da04420")
        for output in ("some warning mentions 2ec52da04420", "Created job: invalid-id\n",
                       "Created job: 2ec52da04420\nCreated job: dacd388d0e04\n"):
            with self.subTest(output=output), self.assertRaisesRegex(RuntimeError, "job ID"):
                jobs._created_job_id(output)


class SetupAnswersTests(unittest.TestCase):
    def test_flat_answers_parse_hours_lists_bools_and_timezone(self):
        cfg = cli.apply_answers(cli.base_config("juan"), {
            "owner.name": "Alex", "timezone": "Europe/Berlin", "quiet_hours": "22-7",
            "holidays": "DE, US,,", "slack.enabled": "y", "slack.channels": "#team, #product",
            "daily_call.enabled": "yes", "daily_call.timezone": "Europe/Berlin",
            "daily_call.poll_start": "10:30", "daily_call.poll_end": "12:00",
            "daily_call.channels": "#team", "mail.gmail_api": "y",
            "mail.himalaya_accounts": "none", "calendar.enabled": "n",
            "features": {"weekly_review": False},
        })
        self.assertEqual(cfg["owner"]["name"], "Alex")
        self.assertEqual(cfg["timezone"], "Europe/Berlin")
        self.assertEqual(cfg["quiet_hours"], {"start": 22, "end": 7})
        self.assertEqual(cfg["holidays"], ["DE", "US"])
        self.assertEqual(cfg["slack"]["channels"], ["#team", "#product"])
        self.assertEqual(cfg["daily_call"]["channels"], ["#team"])
        self.assertEqual(cfg["mail"]["himalaya_accounts"], [])
        self.assertIs(cfg["mail"]["gmail_api"], True)
        self.assertIs(cfg["calendar"]["enabled"], False)
        self.assertIs(cfg["features"]["slack_sweep"], True)
        self.assertIs(cfg["features"]["work_recap"], True)
        self.assertIs(cfg["features"]["weekly_review"], False)
        self.assertEqual(cfg["schedule"]["work_recap_after_daily"], "*/5 10-12 * * 1,2,3,4,5")

    def test_authority_answers_and_mobile_enabled(self):
        cfg = cli.apply_answers(cli.base_config("custom"), {
            "authority.auto": "calendar_blocks, reminders, alarms, leave_time, watch_actions",
            "mobile.enabled": "yes",
        })
        self.assertEqual(cfg["authority"]["auto"], [
            "calendar_blocks", "reminders", "alarms", "leave_time", "watch_actions",
        ])
        self.assertIs(cfg["reminders"]["enabled"], True)
        self.assertEqual(cli.apply_answers(cfg, {"authority.auto": "none"})["authority"]["auto"], [])

    def test_invalid_authority_answers_explain_bad_value(self):
        for answers in ({"authority.auto": "calendar_blocks, payments"},
                        {"authority.auto": ["payments"]},
                        {"authority": {"auto": ["payments"]}}):
            with self.subTest(answers=answers), self.assertRaisesRegex(ValueError, r"authority\.auto.*payments"):
                cli.apply_answers(cli.base_config("custom"), answers)

    def test_interview_retries_an_invalid_authority_answer(self):
        from io import StringIO
        from contextlib import redirect_stdout

        cfg = cli.base_config("custom")
        questions = [q for q in cli.QUESTIONS if q[0] in {"authority.auto", "mobile.enabled"}]
        output = StringIO()
        with patch.object(cli, "detect", return_value={}), patch.object(cli, "QUESTIONS", questions), \
                patch("builtins.input", side_effect=["payments", "reminders", "yes"]), redirect_stdout(output):
            cli.interview(cfg)
        self.assertIn("That did not work", output.getvalue())
        self.assertIn("payments", output.getvalue())
        self.assertEqual(cfg["authority"]["auto"], ["reminders"])
        self.assertIs(cfg["reminders"]["enabled"], True)

    def test_mobile_disabled_strips_phone_authority(self):
        cfg = cli.apply_answers(cli.base_config("juan"), {
            "mobile": {"enabled": False}, "reminders": {"enabled": True},
        })
        self.assertEqual(cfg["authority"]["auto"], ["calendar_blocks", "cleanup"])
        self.assertIs(cfg["reminders"]["enabled"], False)

    def test_juan_preset_authority(self):
        cfg = cli.base_config("juan")
        auto = ["calendar_blocks", "reminders", "alarms", "leave_time", "cleanup"]
        self.assertEqual(cfg["authority"]["auto"], auto)
        cfg = cli.apply_answers(cfg, {"mobile.enabled": True})
        self.assertEqual(cfg["authority"]["auto"], auto)

    def test_invalid_timezone_raises(self):
        for key in ("timezone", "daily_call.timezone"):
            with self.subTest(key=key), self.assertRaises(ZoneInfoNotFoundError):
                cli.apply_answers(cli.base_config("custom"), {key: "Invalid/Zone"})
