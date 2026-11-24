"""Hermetic regressions for the resolve-before-ask health step."""

import importlib.util
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import DEFAULT, patch

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))


class ResolveHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(TemporaryDirectory()))
        config = self.tmp / "config.json"
        config.write_text(json.dumps({"owner": {"name": "Alex"}, "features": {"resolve_first": True}}),
                          encoding="utf-8")
        state = self.tmp / "state"
        home = self.tmp / "profiles" / "example"
        self.enterContext(patch.dict("os.environ", EA_STATE_DIR=str(state), HERMES_HOME=str(home),
                                     PROACTIVE_HERMES_CONFIG=str(config)))
        import phconf
        self.enterContext(patch.object(phconf, "CFG", phconf.load()))
        self.enterContext(patch.object(phconf, "hermes_home", return_value=home))
        spec = importlib.util.spec_from_file_location("test_ea_health", RUNTIME / "ea_health.py")
        global health
        health = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(health)
        self.clock = datetime(2025, 2, 12, 12, tzinfo=timezone.utc)
        self.enterContext(patch.object(health, "now", return_value=self.clock))
        self.wait = state / "resolve_wait.jsonl"

    @staticmethod
    def append(path, row):
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    def run_row(self, *keys, age=timedelta()):
        self.append(health.RUN_LOG, {"at": (self.clock - age).isoformat(), "unresolved": list(keys)})

    def wait_row(self, age):
        self.append(self.wait, {"at": (self.clock - age).isoformat(), "key": "waiting"})

    def test_missing_files_are_healthy(self):
        health.check_resolve()
        self.assertEqual(health.faults, {})
        self.assertEqual(list(health.STATE_DIR.iterdir()), [])

    def test_only_distinct_unresolved_keys_in_last_24_hours_count(self):
        self.run_row("a", "b", age=timedelta(hours=23, minutes=59))
        self.run_row("a", "a")
        self.run_row("old", age=timedelta(hours=24, seconds=1))
        self.run_row("future", age=-timedelta(minutes=1))
        self.append(health.RUN_LOG, {"at": self.clock.isoformat()})  # pre-feature row
        health.check_resolve()
        self.assertEqual(health.faults, {})

    def test_three_distinct_unresolved_keys_fault_and_count_changes_keep_same_key(self):
        self.run_row("a", "b", age=timedelta(hours=24))
        self.run_row("b", "c")
        health.check_resolve()
        self.assertEqual(health.faults, {"resolve step skipped on n items": ("desk", "resolve step skipped on 3 items")})
        self.run_row("d")
        health.check_resolve()
        self.assertEqual(health.faults, {"resolve step skipped on n items": ("desk", "resolve step skipped on 4 items")})

    def test_only_wait_rows_older_than_60_minutes_fault(self):
        self.wait_row(timedelta(minutes=60))
        health.check_resolve()
        self.assertEqual(health.faults, {})
        self.wait_row(timedelta(minutes=60, seconds=1))
        self.wait_row(timedelta(hours=2))
        health.check_resolve()
        self.assertEqual(health.faults, {"resolve wait queue stuck": ("desk", "resolve wait queue stuck")})

    def test_bad_json_or_row_shapes_do_not_mask_valid_rows(self):
        for path in (health.RUN_LOG, self.wait):
            path.write_text('{"at":\n[]\n{"at":"bad date"}\n', encoding="utf-8")
        self.append(health.RUN_LOG, {"at": self.clock.isoformat(), "unresolved": "not a list"})
        self.run_row("a", "b", "c", {}, None)
        self.wait_row(timedelta(hours=2))
        health.check_resolve()
        self.assertEqual(health.faults, {
            "resolve step skipped on n items": ("desk", "resolve step skipped on 3 items"),
            "resolve wait queue stuck": ("desk", "resolve wait queue stuck"),
        })

    def test_expected_job_count_uses_enabled_features_and_plugin_prefix(self):
        with patch.dict(health.phconf.CFG, features={"a": True, "b": False, "c": True}):
            health.check_jobs([{"name": "Proactive · one"}, {"name": "Other job"}])
        self.assertEqual(health.faults, {
            "only n desk jobs exist, expected n": ("desk", "only 1 desk jobs exist, expected 2"),
        })

    def test_reminders_disabled_skips_bridge_database(self):
        with patch.object(health.sqlite3, "connect") as connect:
            health.check_device()
        connect.assert_not_called()

    def test_google_auth_message_uses_configured_owner(self):
        with patch.object(health.subprocess, "run") as run:
            run.return_value.stdout = '{"status": "error", "error": "401 expired"}'
            health.check_google()
        self.assertEqual(list(health.faults.values()), [
            ("owner", "Alex, reconnect Google so I can read and block time on your calendar."),
        ])

    def test_local_delivery_uses_configured_profile(self):
        with patch.object(health.subprocess, "run") as run, patch.object(health, "log"):
            health.check_local_delivery([{"id": "example-job", "deliver": "origin"}])
        run.assert_called_once_with(["hermes", "-p", "example", "cron", "edit", "example-job",
                                     "--deliver", "local"], capture_output=True, text=True, timeout=60)

    def test_main_runs_resolve_step_without_other_health_effects(self):
        self.run_row("a", "b", "c")
        # Do not invoke other probes, delivery checks, persistence, or notification code.
        stubs = dict.fromkeys(("check_local_delivery", "check_jobs", "check_contract",
                              "check_say_outbox", "check_say_receipts"), DEFAULT)
        with patch.multiple(health, **stubs), patch.object(health, "load_jobs", return_value=[]), \
                patch.object(health, "report", return_value=[]), patch.object(health, "log") as log:
            self.assertEqual(health.main(), 0)
            log.assert_not_called()
        self.assertEqual(health.faults, {"resolve step skipped on n items": ("desk", "resolve step skipped on 3 items")})


if __name__ == "__main__":
    unittest.main()
