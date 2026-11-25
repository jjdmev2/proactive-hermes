"""Closing an item in chat closes it everywhere and keeps the desk from raising it again."""

import io
import json
from contextlib import redirect_stderr
from unittest.mock import patch

try:  # `discover -s tests -t .` imports the package; plain `discover -s tests` does not.
    from .test_desk import FakeStore, OfflineTestCase, desk, desk_say, reminders
except ImportError:
    from test_desk import FakeStore, OfflineTestCase, desk, desk_say, reminders
import ea_close  # noqa: E402  (test_desk puts runtime/ on sys.path)


class CloseItemTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.configure(reminders={"enabled": True})
        self.enterContext(patch.object(reminders, "PLUGIN", self.tmp))
        (self.tmp / "bridge.py").touch()
        self.freeze_datetime(ea_close)
        self.store = FakeStore()
        self.enterContext(patch.object(reminders, "bridge", return_value=(None, self.store)))
        self.enterContext(patch.object(reminders, "wake"))
        self.write("promises.json", {"promise-a": {"status": "open", "what": "Send the deck to Sam"}})
        self.write("goals.json", {"goals": [{"key": "goal-b", "status": "open", "title": "Ship the beta"}]})
        self.write("reminder_outbox.json", {
            "planilla": {"key": "planilla", "status": "landed", "title": "Payroll sheet", "reminder_id": "R"},
            "promise-a": {"key": "promise-a", "status": "landed", "title": "Deck for Sam", "reminder_id": "RP"},
            "old": {"key": "old", "status": "done", "title": "Payroll old", "reminder_id": "RO"}})

    def write(self, name, data):
        (self.tmp / name).write_text(json.dumps(data))

    def state(self, name):
        return self.read_json(self.tmp / name)

    def test_close_marks_ledger_promise_and_phone(self):
        out = ea_close.close("promise-a", "owner: old", "dropped")
        self.assertEqual(out["closed_in"], ["ledger", "promise", "phone reminder"])
        self.assertEqual(out["reminder"]["status"], "closing")
        led = self.state("actions.json")["promise-a"]
        self.assertEqual((led["status"], led["evidence"], led["closed"]), ("dropped", "owner: old", self.clock.isoformat()))
        promise = self.state("promises.json")["promise-a"]
        self.assertEqual((promise["status"], promise["evidence"], promise["closed_at"]),
                         ("dropped", "owner: old", self.clock.isoformat()))
        self.assertEqual(self.store.requests[0][:2], ("reminders.complete", {"id": "RP"}))
        self.assertEqual(self.state("reminder_outbox.json")["promise-a"]["status"], "closing")

    def test_close_marks_goal_and_status_done(self):
        out = ea_close.close("goal-b", "proof", "done")
        self.assertEqual(out["closed_in"], ["ledger", "goal"])
        goal = self.state("goals.json")["goals"][0]
        self.assertEqual((goal["status"], goal["evidence"]), ("done", "proof"))
        self.assertEqual(self.state("actions.json")["goal-b"]["status"], "done")
        self.assertEqual(out["reminder"], {"status": "no_reminder", "key": "goal-b"})
        self.assertEqual(self.store.requests, [])

    def test_only_open_records_are_rewritten_and_unrelated_ones_survive(self):
        self.write("promises.json", {"promise-a": {"status": "done", "evidence": "sent"}, "other": {"status": "open"}})
        self.write("actions.json", {"other": {"status": "open", "claimed_reminder": "x"}})
        out = ea_close.close("promise-a", "again", "dropped")
        self.assertNotIn("promise", out["closed_in"])
        self.assertEqual(self.state("promises.json")["promise-a"], {"status": "done", "evidence": "sent"})
        self.assertEqual(self.state("actions.json")["other"], {"status": "open", "claimed_reminder": "x"})

    def test_ledger_row_keeps_what_the_desk_already_recorded(self):
        self.write("actions.json", {"planilla": {"reminder_id": "R", "reminder_status": "landed"}})
        ea_close.close("planilla", "owner: old invite", "dropped")
        row = self.state("actions.json")["planilla"]
        self.assertEqual((row["reminder_id"], row["reminder_status"], row["status"]), ("R", "landed", "dropped"))

    def test_phone_reminder_already_closing_or_done_is_reported_not_repeated(self):
        first = ea_close.close("planilla", "owner: old", "dropped")
        again = ea_close.close("planilla", "owner: old", "dropped")
        self.assertEqual((first["reminder"]["status"], again["reminder"]["status"]), ("closing", "closing"))
        self.assertIn("phone reminder", again["closed_in"])
        self.assertEqual(len(self.store.requests), 1)

    def test_one_unreadable_record_does_not_stop_the_others(self):
        (self.tmp / "promises.json").write_text("{not json")
        out = ea_close.close("promise-a", "owner: old", "dropped")
        self.assertEqual(out["closed_in"], ["ledger", "phone reminder"])
        self.assertEqual(list(out["errors"]), ["promise"])
        self.assertEqual((self.tmp / "promises.json").read_text(), "{not json")  # never overwritten

    def test_a_phone_failure_is_reported_and_records_stay_closed(self):
        with patch.object(reminders, "bridge", side_effect=OSError("bridge down")):
            out = ea_close.close("planilla", "owner: old", "dropped")
        self.assertEqual(out["closed_in"], ["ledger"])
        self.assertEqual(out["reminder"]["status"], "error")
        self.assertEqual(self.state("actions.json")["planilla"]["status"], "dropped")

    def test_find_by_title_key_and_text_in_every_record(self):
        keys = lambda text: sorted((r["where"], r["key"]) for r in ea_close.find(text))  # noqa: E731
        self.assertEqual(keys("payroll"), [("reminder", "planilla")])  # "old" is already done
        self.assertEqual(keys("deck"), [("promise", "promise-a"), ("reminder", "promise-a")])
        self.assertEqual(keys("beta"), [("goal", "goal-b")])
        self.assertEqual(keys("nothing like this"), [])

    def test_find_skips_closed_items_and_tolerates_missing_files(self):
        ea_close.close("goal-b", "proof", "done")
        self.assertEqual(ea_close.find("beta"), [])
        (self.tmp / "goals.json").unlink()
        (self.tmp / "promises.json").write_text("[]")
        self.assertEqual([r["key"] for r in ea_close.find("payroll")], ["planilla"])

    def test_cli_closes_and_finds_with_json_output(self):
        found = json.loads(self.cli(ea_close, "--find", "sam"))
        self.assertEqual(sorted((r["where"], r["key"]) for r in found), [("promise", "promise-a"), ("reminder", "promise-a")])
        out = json.loads(self.cli(ea_close, "--key", "planilla", "--why", "owner: old invite", "--status", "dropped"))
        self.assertEqual((out["key"], out["status"], out["closed_in"]), ("planilla", "dropped", ["ledger", "phone reminder"]))
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):  # --why is required
            self.cli(ea_close, "--key", "planilla")


class DeskSkipsClosedTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.patch_paths(desk, "SAID", "DIGEST", "HELD", "PING_LOG", "RUN_LOG", "LEDGER", "PENDING", "RESOLVE_WAIT")
        self.enterContext(patch.object(desk, "now", lambda: self.clock))
        self.enterContext(patch.object(desk_say, "deliver", return_value={"status": "queued"}))
        self.enterContext(patch.object(reminders, "bridge", side_effect=AssertionError("no phone in this test")))

    @staticmethod
    def item(key):
        return {"key": key, "kind": "action", "text": f"Work on {key}", "change": "open"}

    def compose(self, *keys):
        return self.cli(desk, "compose", "--job", "test", "--print-only", stdin=json.dumps([self.item(k) for k in keys]))

    def close(self, key, status):
        (self.tmp / "actions.json").write_text(json.dumps(
            {**(json.loads((self.tmp / "actions.json").read_text()) if (self.tmp / "actions.json").exists() else {}),
             key: {"status": status, "evidence": "owner: old invite"}}))

    def test_closed_keys_reads_only_closed_statuses_of_the_ledger(self):
        (self.tmp / "actions.json").write_text(json.dumps({
            "a": {"status": "done"}, "b": {"status": "Dropped"}, "c": {"status": "cancelled"}, "d": {"status": "resolved"},
            "e": {"status": "closed"}, "open": {"status": "open"}, "claimed": {"claimed_reminder": "x"}, "junk": "done"}))
        self.assertEqual(desk.closed_keys(), {"a", "b", "c", "d", "e"})

    def test_closed_keys_survives_missing_and_malformed_ledgers(self):
        self.assertEqual(desk.closed_keys(), set())
        (self.tmp / "actions.json").write_text("[1]")
        self.assertEqual(desk.closed_keys(), set())
        (self.tmp / "actions.json").write_text("{nope")
        self.assertEqual(desk.closed_keys(), set())

    def test_compose_never_raises_a_closed_item_and_logs_the_skip(self):
        self.close("planilla", "dropped")
        self.assertEqual(self.compose("planilla"), "[SILENT]\n")
        self.assertEqual(self.rows(desk.PING_LOG), [])
        self.assertEqual([r for r in self.rows(desk.RUN_LOG) if "skipped_closed" in r],
                         [{"at": self.clock.isoformat(timespec="seconds"), "job": "test", "skipped_closed": ["planilla"]}])

    def test_compose_still_raises_the_open_items_next_to_a_closed_one(self):
        self.close("planilla", "done")
        self.assertEqual(self.compose("planilla", "other"), "Work on other\n")
        self.assertEqual([r["keys"] for r in self.rows(desk.PING_LOG)], [["other"]])

    def test_open_ledger_status_does_not_silence_an_item(self):
        self.close("planilla", "open")
        self.assertEqual(self.compose("planilla"), "Work on planilla\n")
        self.assertFalse([r for r in self.rows(desk.RUN_LOG) if "skipped_closed" in r])
