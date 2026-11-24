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
    def __init__(self):
        self.calls = []
        self.live = {}
        self.next_id = 0

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
            stdout = f"Created job: {jid}\n  Name: Proactive · fixture\n  ⚠  Scheduler is not ready.\n"
        elif action == "edit":
            jid = argv[3]
            self.live[jid] = argv
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
        self.cfg = cli.base_config("custom")
        self.cfg["timezone"] = "Europe/Berlin"
        self.cfg["features"].update({key: key == "health" for key in self.cfg["features"]})

    def test_first_apply_creates_local_script_jobs_then_second_apply_is_read_only(self):
        self.assertEqual([r["action"] for r in jobs.apply(self.cfg, run=self.run)].count("create"), 2)
        creates = list(self.run.live.values())
        for argv, script in zip(creates, ("ea_health.py", "desk_tick.py")):
            for flag, value in (("--deliver", "local"), ("--failure-deliver", "local"),
                                ("--timezone", "Europe/Berlin"), ("--catch-up", "off"),
                                ("--script", f"proactive-hermes/{script}")):
                self.assertEqual(argv[argv.index(flag) + 1], value)
            self.assertIn("--no-wrap-response", argv)
            self.assertIn("--no-agent", argv)
            self.assertTrue((self.home / "scripts" / "proactive-hermes" / script).is_file())
        self.assertTrue((self.home / "plugin-data" / "proactive-hermes" / "jobs.json").is_file())
        self.run.calls.clear()
        jobs.apply(self.cfg, run=self.run)
        self.assertFalse([argv for argv in self.run.calls if argv[2] != "list"])
        self.assertEqual(len(self.run.live), 2)

    def test_disabled_feature_removes_managed_jobs_only(self):
        jobs.apply(self.cfg, run=self.run)
        managed = set(self.run.live)
        self.run.live["abcdef123456"] = ["unrelated job"]
        self.cfg["features"]["health"] = False
        rows = jobs.apply(self.cfg, run=self.run)
        self.assertEqual([row["action"] for row in rows].count("remove"), 2)
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
        self.assertCountEqual(jobs.remove_all(run=self.run), ["health", "tick"])
        self.assertEqual(self.run.live, {})

    def test_external_removal_recreates_only_missing_managed_job(self):
        jobs.apply(self.cfg, run=self.run)
        first, second = self.run.live
        del self.run.live[first]
        self.run.calls.clear()
        jobs.apply(self.cfg, run=self.run)
        self.assertEqual([a[2] for a in self.run.calls], ["list", "create"])
        self.assertIn(second, self.run.live)
        self.assertNotIn(first, self.run.live)

    def test_dry_run_and_legacy_registry_migration_keep_existing_ids(self):
        rows = jobs.apply(self.cfg, dry_run=True, run=self.run)
        self.assertEqual([row["action"] for row in rows].count("create"), 2)
        self.assertFalse(list(self.home.iterdir()))
        self.assertEqual(self.run.calls, [])

        jobs.apply(self.cfg, run=self.run)
        path = self.home / "plugin-data" / "proactive-hermes" / "jobs.json"
        ids = {key: entry["id"] for key, entry in json.loads(path.read_text()).items()}
        path.write_text(json.dumps(ids))
        self.run.calls.clear()
        jobs.apply(self.cfg, run=self.run)
        self.assertEqual([argv[2] for argv in self.run.calls], ["list", "edit", "edit"])
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

    def test_invalid_timezone_raises(self):
        for key in ("timezone", "daily_call.timezone"):
            with self.subTest(key=key), self.assertRaises(ZoneInfoNotFoundError):
                cli.apply_answers(cli.base_config("custom"), {key: "Invalid/Zone"})
