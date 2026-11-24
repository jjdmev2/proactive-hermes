"""Offline alarm outbox tests; the fake phone never runs native operations."""

from datetime import timedelta
import json
from unittest.mock import patch

from .test_desk import FakeStore, OfflineTestCase, reminders


class AlarmTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.configure(reminders={"enabled": True}, authority={"auto": ["alarms"]})
        self.enterContext(patch.object(reminders, "PLUGIN", self.tmp))
        (self.tmp / "bridge.py").touch()
        self.freeze_datetime(reminders)
        self.store = FakeStore()
        self.bridge = self.enterContext(patch.object(reminders, "bridge", return_value=(None, self.store)))
        self.wake = self.enterContext(patch.object(reminders, "wake"))
        self.at = "2030-04-03T18:00:00+02:00"

    def alarm(self, key="urgent", at=None, label="Leave for Berlin"):
        return json.loads(self.cli(reminders, "alarm", "--key", key, "--at", at or self.at, "--label", label))

    def test_repeats_queue_only_one_exact_alarm_request(self):
        first = self.alarm()
        self.assertEqual(first["status"], "queued")
        self.assertEqual(self.alarm()["status"], "exists")
        (op, args, key, ttl), = self.store.requests
        self.assertEqual((op, args, ttl), ("alarms.create", {"at": self.at, "label": "Leave for Berlin"}, reminders.TTL))
        self.assertTrue(key)
        self.wake.assert_called_once_with("fake-phone")

    def test_retry_requires_confirmed_expiry_and_stops_when_landed(self):
        rid = self.alarm()["request_id"]
        for status in ("pending", "running"):
            self.store.results[rid] = {"status": status}
            self.assertEqual(self.cli(reminders, "tick"), "")
        with patch.object(self.store, "result", side_effect=OSError("read failed")):
            self.assertEqual(self.cli(reminders, "tick"), "")
        self.assertEqual(len(self.store.requests), 1)
        self.store.results[rid] = {"status": "expired"}
        self.cli(reminders, "tick")
        self.assertEqual(len(self.store.requests), 2)
        self.assertNotEqual(self.store.requests[0][2], self.store.requests[1][2])
        self.assertEqual(self.store.requests[1][:2], self.store.requests[0][:2])
        self.store.results["request-2"] = {"status": "succeeded", "result": {"data": {"id": "alarm-1"}}}
        self.cli(reminders, "tick")
        self.cli(reminders, "tick")
        ledger = self.read_json(self.tmp / reminders.LEDGER)["urgent"]
        self.assertEqual((ledger["alarm_status"], ledger["alarm_id"]), ("landed", "alarm-1"))
        self.assertNotIn("reminder_id", ledger)
        self.assertEqual(len(self.store.requests), 2)

    def test_no_authority_or_revoked_authority(self):
        rid = self.alarm()["request_id"]
        self.configure(reminders={"enabled": True}, authority={"auto": ["reminders"], "ask": ["alarms"]})
        self.assertEqual(self.alarm("new"), {"status": "not_allowed"})
        self.store.results[rid] = {"status": "expired"}
        self.cli(reminders, "tick")
        self.assertEqual(len(self.store.requests), 1)

    def test_daily_cap_is_separate_from_reminders_and_resets_locally(self):
        self.configure(reminders={"enabled": True, "per_day": 1, "alarms_per_day": 2}, authority={"auto": ["alarms"]})
        self.assertEqual(self.alarm("one")["status"], "queued")
        result = self.cli(reminders, "add", "--key", "normal", "--title", "Regular reminder")
        self.assertEqual(json.loads(result)["status"], "queued")
        self.assertEqual(self.alarm("two")["status"], "queued")
        self.assertEqual(self.alarm("three"), {"status": "refused", "reason": "daily cap 2 alarms reached"})
        self.assertEqual(len(self.store.requests), 3)
        self.clock += timedelta(days=1)
        self.assertEqual(self.alarm("three")["status"], "queued")

    def test_missing_bridge_and_disabled_outbox(self):
        self.configure(reminders={"enabled": False}, authority={"auto": ["alarms"]})
        self.assertEqual(self.alarm(), {"status": "disabled"})
        self.configure(reminders={"enabled": True}, authority={"auto": ["alarms"]})
        (self.tmp / "bridge.py").unlink()
        self.assertEqual(self.alarm(), {"status": "disabled"})
        self.bridge.assert_not_called()

    def test_alarm_needs_timezone_and_bridge_sized_label(self):
        self.assertEqual(self.alarm(at="2030-04-03T18:00:00")["status"], "error")
        self.assertEqual(self.alarm(label="x" * 101)["status"], "error")
        self.bridge.assert_not_called()
