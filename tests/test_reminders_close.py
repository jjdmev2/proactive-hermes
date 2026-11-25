"""Finished desk items tick their phone reminder off, and never touch reminders the desk did not create."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

try:  # `discover -s tests -t .` imports the package; plain `discover -s tests` does not.
    from .test_desk import FakeStore, OfflineTestCase, desk, phconf, reminders
except ImportError:
    from test_desk import FakeStore, OfflineTestCase, desk, phconf, reminders


class CloseTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.configure(reminders={"enabled": True})
        self.enterContext(patch.object(reminders, "PLUGIN", self.tmp))
        (self.tmp / "bridge.py").touch()
        self.freeze_datetime(reminders)
        self.store = FakeStore()
        self.bridge = self.enterContext(patch.object(reminders, "bridge", return_value=(None, self.store)))
        self.write(reminders.OUTBOX, {
            "promise-x": {"key": "promise-x", "status": "landed", "reminder_id": "R1"},
            "alias-x": {"key": "alias-x", "status": "landed", "reminder_id": "R2"},
            "goal-x": {"key": "goal-x", "status": "landed", "reminder_id": "R3"},
            "action-x": {"key": "action-x", "status": "landed", "reminder_id": "R4"},
            "still-open": {"key": "still-open", "status": "landed", "reminder_id": "R5"},
            "never-landed": {"key": "never-landed", "status": "failed"},
            "alarm-x": {"key": "alarm-x", "operation": "alarms.create", "status": "landed", "alarm_id": "A1"},
        })
        self.write(reminders.PROMISES, {"promise-x": {"status": "done", "evidence": "sent"},
                                        "promise-aliased": {"status": "Dropped", "evidence": "sent to the owner"},
                                        "never-landed": {"status": "done"}, "alarm-x": {"status": "done"},
                                        "still-open": {"status": "open"}})
        self.write(reminders.GOALS, {"goals": [{"key": "goal-x", "status": "done", "evidence": "shipped"},
                                               {"key": "other", "status": "open"}]})
        self.write(reminders.LEDGER, {"action-x": {"status": "dropped", "evidence": "owner: old"},
                                      "still-open": {"reminder_status": "landed"}})
        self.enterContext(patch.dict(reminders.ALIASES, {"alias-x": "promise-aliased"}))

    def write(self, name, data):
        (self.tmp / name).write_text(json.dumps(data))

    def box(self):
        return self.read_json(self.tmp / reminders.OUTBOX)

    def complete_requests(self):
        return [(args["id"], key) for op, args, key, _ttl in self.store.requests if op == "reminders.complete"]


class FinishedKeysTests(CloseTestCase):
    def test_reads_promises_goals_and_ledger_with_evidence(self):
        self.assertEqual(reminders.finished_keys(), {
            "promise-x": "sent", "promise-aliased": "sent to the owner", "never-landed": "promise closed",
            "alarm-x": "promise closed", "goal-x": "shipped", "action-x": "owner: old"})

    def test_ledger_closed_marker_counts_and_open_rows_do_not(self):
        self.write(reminders.LEDGER, {"a": {"closed": "2030-04-01T09:00:00+00:00"}, "b": {"status": "open"}})
        self.assertEqual(reminders.finished_keys()["a"], "2030-04-01T09:00:00+00:00")
        self.assertNotIn("b", reminders.finished_keys())

    def test_missing_or_unreadable_records_are_empty_not_fatal(self):
        (self.tmp / reminders.PROMISES).write_text("{not json")
        (self.tmp / reminders.GOALS).unlink()
        self.write(reminders.LEDGER, ["not", "a", "mapping"])
        self.assertEqual(reminders.finished_keys(), {})
        self.assertEqual(self.cli(reminders, "tick"), "")
        self.bridge.assert_not_called()

    def test_goals_may_be_a_bare_list(self):
        self.write(reminders.GOALS, [{"key": "goal-x", "status": "done"}, "junk", {"status": "done"}])
        self.assertEqual(reminders.finished_keys()["goal-x"], "goal closed")

    def test_plugin_ships_without_aliases(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("fresh_reminders", reminders.__file__)
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        self.assertEqual(fresh.ALIASES, {})


class CloseFinishedTests(CloseTestCase):
    def test_finished_items_are_completed_on_phone_and_the_rest_untouched(self):
        box = self.box()
        self.assertTrue(reminders.close_finished(self.store, box))
        self.assertEqual(sorted(i for i, _ in self.complete_requests()), ["R1", "R2", "R3", "R4"])
        self.assertEqual({k: v["status"] for k, v in box.items() if v["status"] == "closing"},
                         dict.fromkeys(["promise-x", "alias-x", "goal-x", "action-x"], "closing"))
        for key in ("still-open", "never-landed", "alarm-x"):  # open, never landed, and an alarm
            self.assertNotEqual(box[key]["status"], "closing")
        self.assertEqual(box["promise-x"]["close_evidence"], "sent")
        self.assertEqual(self.box()["goal-x"]["close_request_id"], box["goal-x"]["close_request_id"])  # saved
        self.assertFalse(hasattr(reminders, "wake"))

    def test_close_succeeded_is_recorded_in_outbox_and_ledger(self):
        box = self.box()
        reminders.close_finished(self.store, box)
        for rid in [v["close_request_id"] for v in box.values() if v.get("close_request_id")]:
            self.store.results[rid] = {"status": "succeeded"}
        self.assertTrue(reminders.check_closing(self.store, box))
        self.assertEqual(box["promise-x"]["status"], "done")
        self.assertEqual(self.read_json(self.tmp / reminders.LEDGER)["promise-x"]["reminder_status"], "done")
        self.assertEqual(self.box()["promise-x"]["status"], "done")
        self.assertEqual(self.read_json(self.tmp / reminders.LEDGER)["action-x"]["status"], "dropped")  # kept

    def test_done_items_are_not_closed_twice(self):
        box = self.box()
        reminders.close_finished(self.store, box)
        for rid in [v["close_request_id"] for v in box.values() if v.get("close_request_id")]:
            self.store.results[rid] = {"status": "succeeded"}
        reminders.check_closing(self.store, box)
        count = len(self.store.requests)
        self.assertFalse(reminders.close_finished(self.store, box))
        self.assertFalse(reminders.check_closing(self.store, box))
        self.assertEqual(len(self.store.requests), count)

    def test_reminder_deleted_by_hand_counts_as_done(self):
        box = self.box()
        reminders.close_finished(self.store, box)
        self.store.results["request-1"] = {"status": "failed", "error": "Reminder not found"}
        reminders.check_closing(self.store, box)
        self.assertEqual(box["promise-x"]["status"], "done")
        self.store.results["request-2"] = {"status": "failed", "error": "permission denied"}
        reminders.check_closing(self.store, box)
        self.assertEqual((box["alias-x"]["status"], box["alias-x"]["close_error"]), ("close_failed", "permission denied"))
        self.assertFalse(reminders.close_finished(self.store, box))  # close_failed is not silently retried

    def test_expired_close_is_retried_with_a_new_bridge_key(self):
        box = self.box()
        reminders.close_finished(self.store, box)
        self.store.results["request-1"] = {"status": "expired"}
        reminders.check_closing(self.store, box)
        keys = [key for rid, key in self.complete_requests() if rid == "R1"]
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])
        self.assertTrue(all(len(k) <= 128 for k in keys))
        self.assertEqual((box["promise-x"]["status"], box["promise-x"]["close_attempts"]), ("closing", 2))

    def test_long_item_keys_keep_distinct_attempts_inside_the_bridge_limit(self):
        key = "k" * 300
        box = {key: {"key": key, "status": "landed", "reminder_id": "RL"}}
        self.write(reminders.PROMISES, {key: {"status": "done"}})
        reminders.close_finished(self.store, box)
        self.store.results["request-1"] = {"status": "expired"}
        reminders.check_closing(self.store, box)
        first, second = [k for _, k in self.complete_requests()]
        self.assertNotEqual(first, second)
        self.assertLessEqual(max(len(first), len(second)), 128)

    def test_close_attempts_stop_at_the_limit(self):
        box = self.box()
        reminders.close_finished(self.store, box)
        box["promise-x"]["close_attempts"] = reminders.MAX_ATTEMPTS
        self.store.results["request-1"] = {"status": "expired"}
        reminders.check_closing(self.store, box)
        self.assertEqual(box["promise-x"]["status"], "close_failed")

    def test_a_failed_request_leaves_the_item_landed_for_the_next_tick(self):
        box = self.box()
        with patch.object(self.store, "request", side_effect=OSError("bridge down")):
            self.assertFalse(reminders.close_finished(self.store, box))
        self.assertEqual(box["promise-x"]["status"], "landed")
        rows = self.rows(self.tmp / reminders.RUN_LOG)
        self.assertEqual({r["close_error"] for r in rows}, {"promise-x", "alias-x", "goal-x", "action-x"})


class TickTests(CloseTestCase):
    def test_tick_closes_finished_items_then_confirms_on_a_later_tick(self):
        self.assertEqual(self.cli(reminders, "tick"), "")
        self.assertEqual(sorted(i for i, _ in self.complete_requests()), ["R1", "R2", "R3", "R4"])
        self.assertEqual(self.box()["goal-x"]["status"], "closing")
        self.cli(reminders, "tick")  # still pending: nothing new is requested
        self.assertEqual(len(self.store.requests), 4)
        for rid in ("request-1", "request-2", "request-3", "request-4"):
            self.store.results[rid] = {"status": "succeeded"}
        self.cli(reminders, "tick")
        self.assertEqual({k for k, v in self.box().items() if v["status"] == "done"},
                         {"promise-x", "alias-x", "goal-x", "action-x"})
        self.assertEqual(len(self.store.requests), 4)
        self.assertEqual(sorted(r["closed"] for r in self.rows(self.tmp / reminders.RUN_LOG) if "closed" in r),
                         ["action-x", "alias-x", "goal-x", "promise-x"])
        self.bridge.reset_mock()
        self.cli(reminders, "tick")  # nothing queued, closing or finished-and-landed: no bridge needed
        self.bridge.assert_not_called()

    def test_tick_with_nothing_finished_does_not_open_the_bridge(self):
        for name in (reminders.PROMISES, reminders.GOALS, reminders.LEDGER):
            (self.tmp / name).unlink()
        self.cli(reminders, "tick")
        self.bridge.assert_not_called()

    def test_tick_still_retries_queued_creates_while_closing(self):
        self.write(reminders.OUTBOX, {
            "promise-x": {"key": "promise-x", "status": "landed", "reminder_id": "R1"},
            "new": {"key": "new", "title": "New", "status": "queued", "attempts": 1, "request_id": "old-request"}})
        self.store.results["old-request"] = {"status": "succeeded", "result": {"data": {"id": "R9"}}}
        self.cli(reminders, "tick")
        box = self.box()
        self.assertEqual((box["promise-x"]["status"], box["new"]["status"], box["new"]["reminder_id"]),
                         ("closing", "landed", "R9"))

    def test_lookup_failure_neither_retries_nor_marks_done(self):
        self.cli(reminders, "tick")
        before = len(self.store.requests)
        with patch.object(self.store, "result", side_effect=OSError("read failed")):
            self.cli(reminders, "tick")
        self.assertEqual(len(self.store.requests), before)
        self.assertEqual(self.box()["promise-x"]["status"], "closing")

    def test_pending_and_running_close_requests_are_never_replaced(self):
        self.cli(reminders, "tick")
        for status in ("pending", "running"):
            self.store.results["request-1"] = {"status": status}
            self.cli(reminders, "tick")
        self.assertEqual(len(self.store.requests), 4)

    def test_expired_close_is_requeued_on_the_next_tick(self):
        self.cli(reminders, "tick")
        self.store.results["request-1"] = {"status": "expired"}
        self.cli(reminders, "tick")
        self.assertEqual([i for i, _ in self.complete_requests()].count("R1"), 2)


class AuthorityTests(CloseTestCase):
    def test_never_blocks_automatic_and_explicit_closing(self):
        self.configure(reminders={"enabled": True}, authority={"never": ["reminders"]})
        self.assertEqual(self.cli(reminders, "tick"), "")
        self.assertEqual(json.loads(self.cli(reminders, "close", "--key", "promise-x")),
                         {"status": "not_allowed", "key": "promise-x"})
        self.assertEqual(self.store.requests, [])
        self.assertEqual(self.box()["promise-x"]["status"], "landed")

    def test_revoked_item_authority_stops_closing_and_close_retries(self):
        box = self.box()
        box["promise-x"]["required_authority"] = ["leave_time", "reminders"]
        self.write(reminders.OUTBOX, box)
        self.configure(reminders={"enabled": True}, authority={"auto": ["reminders", "leave_time"]})
        self.cli(reminders, "tick")
        self.assertIn("R1", [i for i, _ in self.complete_requests()])
        self.store.results["request-1"] = {"status": "expired"}
        self.configure(reminders={"enabled": True}, authority={"auto": ["reminders"]})
        before = len(self.store.requests)
        self.cli(reminders, "tick")
        self.assertEqual(len(self.store.requests), before)
        self.assertEqual(self.box()["promise-x"]["status"], "closing")

    def test_ask_first_default_still_closes_reminders_the_desk_created(self):
        self.assertEqual(phconf.allowed("reminders"), "ask")
        self.assertEqual(json.loads(self.cli(reminders, "close", "--key", "still-open"))["status"], "closing")


class CloseCommandTests(CloseTestCase):
    def close(self, *args):
        return json.loads(self.cli(reminders, "close", *args))

    def test_close_queues_one_complete_request_and_remembers_why(self):
        result = self.close("--key", "still-open", "--evidence", "owner: done")
        self.assertEqual((result["status"], result["key"], result["request_id"]), ("closing", "still-open", "request-1"))
        self.assertEqual(self.store.requests[0][:2], ("reminders.complete", {"id": "R5"}))
        row = self.box()["still-open"]
        self.assertEqual((row["status"], row["close_evidence"], row["close_attempts"]), ("closing", "owner: done", 1))
        self.assertFalse(hasattr(reminders, "wake"))  # the desk never wakes the phone itself
        self.assertEqual(self.close("--key", "still-open"), {"status": "closing", "key": "still-open"})
        self.assertEqual(len(self.store.requests), 1)

    def test_default_evidence_is_neutral(self):
        self.close("--key", "still-open")
        self.assertEqual(self.box()["still-open"]["close_evidence"], "closed by owner")

    def test_close_without_a_desk_created_reminder_changes_nothing(self):
        before = (self.tmp / reminders.OUTBOX).read_bytes()
        for key in ("missing", "never-landed", "alarm-x"):
            self.assertEqual(self.close("--key", key), {"status": "no_reminder", "key": key})
        self.assertEqual((self.tmp / reminders.OUTBOX).read_bytes(), before)
        self.assertEqual(self.store.requests, [])

    def test_close_of_a_finished_item_reports_done(self):
        box = self.box()
        box["still-open"]["status"] = "done"
        self.write(reminders.OUTBOX, box)
        self.assertEqual(self.close("--key", "still-open"), {"status": "done", "key": "still-open"})

    def test_disabled_outbox_does_nothing(self):
        self.configure(reminders={"enabled": False})
        self.assertEqual(self.close("--key", "still-open"), {"status": "disabled"})
        self.assertEqual(json.loads(self.cli(reminders, "tick")), {"status": "disabled"})
        self.bridge.assert_not_called()


class LedgerLockTests(CloseTestCase):
    def test_ledger_set_waits_for_whoever_holds_the_shared_actions_lock(self):
        if not hasattr(reminders.ledger_set, "__wrapped__"):
            self.skipTest("ea_reminders.ledger_set is not under the actions.json lock yet (see ea_desk.ledger_lock)")
        ledger = phconf.state_dir() / reminders.LEDGER
        code = ("import sys, time; sys.path.insert(0, sys.argv[1]); import ea_desk; from pathlib import Path\n"
                "with ea_desk.ledger_lock(Path(sys.argv[2])):\n print('held', flush=True); time.sleep(1.0)")
        holder = subprocess.Popen([sys.executable, "-c", code, str(Path(desk.__file__).parent), str(ledger)],
                                  stdout=subprocess.PIPE, text=True, env=os.environ.copy())
        self.assertEqual(holder.stdout.readline().strip(), "held")
        started = time.monotonic()
        reminders.ledger_set("k", {"reminder_status": "queued"})
        waited = time.monotonic() - started
        holder.stdout.close()
        holder.wait(timeout=10)
        self.assertGreater(waited, 0.5)
        self.assertEqual(self.read_json(ledger)["k"]["reminder_status"], "queued")
