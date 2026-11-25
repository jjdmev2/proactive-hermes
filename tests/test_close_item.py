"""Closing an item in chat closes it everywhere and keeps the desk from raising it again."""

import argparse
import io
import json
import os
import subprocess
import sys
import time
from contextlib import redirect_stderr
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

try:  # `discover -s tests -t .` imports the package; plain `discover -s tests` does not.
    from .test_desk import SILENT, FakeStore, OfflineTestCase, desk, desk_say, reminders
except ImportError:
    from test_desk import SILENT, FakeStore, OfflineTestCase, desk, desk_say, reminders
import ea_close  # noqa: E402  (test_desk puts runtime/ on sys.path)


class CloseBase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.configure(reminders={"enabled": True})
        self.enterContext(patch.object(reminders, "PLUGIN", self.tmp))
        (self.tmp / "bridge.py").touch()
        self.freeze_datetime(ea_close)
        self.store = FakeStore()
        self.enterContext(patch.object(reminders, "bridge", return_value=(None, self.store)))
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


class CloseItemTests(CloseBase):
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


INJECTION = "$(touch PWNED) `touch PWNED` ; touch PWNED && \"quoted\" 'single' \\ done\nsecond line"


class UnknownKeyTests(CloseBase):
    def test_a_key_in_no_record_is_not_closed_and_nothing_is_written(self):
        def snapshot():
            return {p.name: p.read_bytes() for p in self.tmp.iterdir() if not p.name.endswith(".lock")}

        before = snapshot()
        out = ea_close.close("typo-key", "owner: old", "dropped")
        self.assertEqual(out, {"status": "unknown_key"})
        self.assertEqual(snapshot(), before)
        self.assertFalse((self.tmp / "actions.json").exists())
        self.assertEqual(self.store.requests, [])

    def test_a_key_the_desk_only_said_or_looks_ahead_at_is_known(self):
        self.write("said.json", {"mail:thread-1": {"text": "Reply to Sam", "said_at": self.clock.isoformat()}})
        self.write("lookahead.json", {"items": {"la:cal:trip": {"title": "Trip to Lahti"}}})
        self.assertEqual(ea_close.close("mail:thread-1", "owner: skip", "dropped")["closed_in"], ["ledger"])
        self.assertEqual(ea_close.close("la:cal:trip", "owner: cancelled", "dropped")["closed_in"], ["ledger"])
        led = self.state("actions.json")
        self.assertEqual((led["mail:thread-1"]["closed_text"], led["la:cal:trip"]["closed_text"]),
                         ("Reply to Sam", "Trip to Lahti"))

    def test_a_ledger_only_key_is_known(self):
        self.write("actions.json", {"block-x": {"claimed_block": "2030-04-01T09:00:00+00:00"}})
        self.assertEqual(ea_close.close("block-x", "owner: gone", "dropped")["closed_in"], ["ledger"])

    def test_cli_prints_the_unknown_key_answer(self):
        self.assertEqual(json.loads(self.cli(ea_close, "--key", "nope", "--why", "x")), {"status": "unknown_key"})


class CloseRecordsTests(CloseBase):
    def test_close_records_when_and_what(self):
        self.write("said.json", {"planilla": {"text": "  Pay the\n payroll sheet ", "said_at": self.clock.isoformat()}})
        ea_close.close("planilla", "owner: paid", "done")
        row = self.state("actions.json")["planilla"]
        self.assertEqual((row["closed_at"], row["closed"], row["closed_text"]),
                         (self.clock.isoformat(), self.clock.isoformat(), "Pay the payroll sheet"))

    def test_text_falls_back_to_the_record_title(self):
        ea_close.close("promise-a", "owner: old", "dropped")
        ea_close.close("planilla", "owner: old", "dropped")
        led = self.state("actions.json")
        self.assertEqual((led["promise-a"]["closed_text"], led["planilla"]["closed_text"]),
                         ("Send the deck to Sam", "Payroll sheet"))

    def test_why_and_evidence_are_kept_apart_and_default_to_each_other(self):
        ea_close.close("planilla", "owner: paid it", "done", "bank receipt 123")
        row = self.state("actions.json")["planilla"]
        self.assertEqual((row["why"], row["evidence"]), ("owner: paid it", "bank receipt 123"))
        self.assertEqual(len(self.store.requests), 1)
        ea_close.close("goal-b", "", "done", "proof only")
        self.assertEqual(self.state("actions.json")["goal-b"]["evidence"], "proof only")
        self.assertNotIn("why", self.state("actions.json")["goal-b"])


class TextFileOptionTests(CloseBase):
    """Untrusted text comes in through files, never through the command line."""

    def run_cli(self, *args, stdin=""):
        env = {**os.environ, "EA_STATE_DIR": str(self.tmp)}
        return subprocess.run([sys.executable, ea_close.__file__, *args], input=stdin, text=True, capture_output=True,
                              cwd=self.tmp, env=env, timeout=60)

    def setUp(self):
        super().setUp()
        self.configure(reminders={"enabled": False})  # the subprocess must not open any phone bridge

    def test_why_file_and_evidence_file_round_trip_literally_and_run_nothing(self):
        why, proof = self.tmp / "why.txt", self.tmp / "evidence.txt"
        why.write_text(INJECTION + "\n", encoding="utf-8")
        proof.write_text("proof `touch PWNED` $(touch PWNED)\n", encoding="utf-8")
        run = self.run_cli("--key", "planilla", "--why-file", str(why), "--evidence-file", str(proof), "--status", "dropped")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse((self.tmp / "PWNED").exists())
        self.assertFalse(Path("PWNED").exists())
        row = self.state("actions.json")["planilla"]
        self.assertEqual((row["why"], row["evidence"], row["status"]),
                         (INJECTION, "proof `touch PWNED` $(touch PWNED)", "dropped"))

    def test_stdin_dash_and_only_one_trailing_newline_is_stripped(self):
        run = self.run_cli("--key", "planilla", "--why-file", "-", stdin="owner: old\n\n")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.state("actions.json")["planilla"]["evidence"], "owner: old\n")

    def test_both_forms_or_a_missing_file_are_usage_errors_and_write_nothing(self):
        f = self.tmp / "why.txt"
        f.write_text("x")
        for args in (("--key", "planilla", "--why", "a", "--why-file", str(f)),
                     ("--key", "planilla", "--why-file", str(self.tmp / "missing.txt")),
                     ("--key", "planilla", "--why-file", "-", "--evidence-file", "-"),
                     ("--key", "planilla")):
            run = self.run_cli(*args)
            self.assertEqual(run.returncode, 2, args)
        self.assertFalse((self.tmp / "actions.json").exists())

    def test_in_process_files_feed_close(self):
        f = self.tmp / "why.txt"
        f.write_text("$(touch PWNED)\n")
        out = json.loads(self.cli(ea_close, "--key", "goal-b", "--why-file", str(f)))
        self.assertEqual(out["closed_in"], ["ledger", "goal"])
        self.assertEqual(self.state("goals.json")["goals"][0]["evidence"], "$(touch PWNED)")


class RecurringKeyTests(OfflineTestCase):
    """A closed key with a stable name (a monthly bill) is not silenced forever."""

    def setUp(self):
        super().setUp()
        self.patch_paths(desk, "SAID", "DIGEST", "HELD", "PING_LOG", "RUN_LOG", "LEDGER", "PENDING", "RESOLVE_WAIT")
        self.enterContext(patch.object(desk, "now", lambda: self.clock))
        self.enterContext(patch.object(desk_say, "deliver", return_value={"status": "queued"}))
        self.enterContext(patch.object(reminders, "bridge", side_effect=AssertionError("no phone in this test")))
        self.freeze_datetime(ea_close)

    @staticmethod
    def bill(text):
        return {"key": "bill", "kind": "action", "text": text, "change": text}

    def compose(self, text):
        return self.cli(desk, "compose", "--job", "test", "--print-only", stdin=json.dumps([self.bill(text)]))

    def advance(self, **delta):
        self.clock += timedelta(**delta)

    def close_bill(self):
        out = ea_close.close("bill", "owner: paid", "done")
        self.assertEqual(out["closed_in"], ["ledger"])

    def test_same_text_stays_closed_and_a_new_text_is_blocked_for_seven_days_only(self):
        self.assertEqual(self.compose("Pay the electricity bill (84 EUR)"), "Pay the electricity bill (84 EUR)\n")
        self.close_bill()
        self.advance(days=1)
        self.assertEqual(self.compose("Pay the electricity bill (84 EUR)"), SILENT)
        self.assertEqual(self.compose("Pay the electricity bill (91 EUR)"), SILENT)  # inside 7 days
        self.advance(days=5, hours=23)  # 6d23h after the close
        self.assertEqual(self.compose("Pay the electricity bill (91 EUR)"), SILENT)
        self.advance(hours=2)  # 7d1h: only the same text is still held
        self.assertEqual(self.compose("  pay the  ELECTRICITY bill (84 EUR) "), SILENT)
        self.assertEqual(self.compose("Pay the electricity bill (91 EUR)"), "Pay the electricity bill (91 EUR)\n")

    def test_a_row_closed_before_text_and_time_were_recorded_stays_closed(self):
        (self.tmp / "actions.json").write_text(json.dumps({"bill": {"status": "dropped", "evidence": "owner: old"}}))
        self.advance(days=90)
        self.assertEqual(self.compose("Pay the electricity bill (91 EUR)"), SILENT)

    def test_old_closed_stamp_without_text_stays_closed(self):
        (self.tmp / "actions.json").write_text(json.dumps(
            {"bill": {"status": "done", "closed": (self.clock - timedelta(days=60)).isoformat()}}))
        self.assertEqual(self.compose("Pay the electricity bill (91 EUR)"), SILENT)

    def test_the_skip_is_logged(self):
        self.compose("Pay the bill")
        self.close_bill()
        self.compose("Pay the bill")
        self.assertTrue([r for r in self.rows(desk.RUN_LOG) if r.get("skipped_closed") == ["bill"]])

    def test_overflow_rows_for_a_key_closed_meanwhile_do_not_speak(self):
        (self.tmp / "actions.json").write_text(json.dumps({"bill": {"status": "done", "evidence": "owner: paid"}}))
        (self.tmp / "digest_queue.jsonl").write_text(json.dumps({"at": self.clock.isoformat(), **self.bill("Pay the bill")}) + "\n")
        self.assertEqual(self.cli(desk, "compose", "--job", "test", "--print-only", stdin="[]"), SILENT)
        self.assertEqual(self.rows(desk.PING_LOG), [])


DRIVER = """
import argparse, json, os, sys, time
sys.path.insert(0, {runtime!r})
import ea_close, ea_desk
tag, mode, n, go = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
while not os.path.exists(go):
    time.sleep(0.001)
for i in range(n):
    key = f"{{tag}}-{{i}}"
    ns = argparse.Namespace(key=key, list=False, claim=None, set=json.dumps({{"n": i}}))
    ea_desk.cmd_ledger(ns)
    if mode == "close":
        ea_close.close(key, "t", "done")
"""


class LedgerLockTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.configure(reminders={"enabled": False})
        self.ledger = self.tmp / "actions.json"

    def test_two_processes_writing_the_ledger_lose_no_update(self):
        script = self.tmp / "driver.py"
        script.write_text(DRIVER.format(runtime=str(Path(ea_close.__file__).parent)))
        go, n = self.tmp / "go", 120
        procs = [subprocess.Popen([sys.executable, str(script), tag, mode, str(n), str(go)], env=os.environ.copy(),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                 for tag, mode in (("a", "set"), ("b", "close"), ("c", "set"))]
        time.sleep(0.5)  # let all three finish importing, then release them together
        go.touch()
        for proc in procs:
            _, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err)
        led = json.loads(self.ledger.read_text())
        self.assertEqual(sorted(led), sorted(f"{t}-{i}" for t in "abc" for i in range(n)))
        self.assertTrue(all(led[f"b-{i}"]["status"] == "done" and led[f"b-{i}"]["n"] == i for i in range(n)))
        self.assertTrue(all(led[f"{t}-{i}"]["n"] == i for t in "ac" for i in range(n)))

    def test_lock_is_a_file_next_to_the_ledger_and_reentrant(self):
        with desk.ledger_lock(self.ledger):
            with desk.ledger_lock(self.ledger):  # no deadlock inside a held block
                self.assertTrue((self.tmp / "actions.json.lock").exists())
        with desk.ledger_lock(lambda: self.ledger):
            pass

    def test_lock_decorates_a_writer_and_resolves_the_path_at_call_time(self):
        calls = []

        @desk.ledger_lock(lambda: calls.append("path") or self.ledger)
        def writer(value):
            return value

        self.assertEqual((writer(1), writer(2), calls), (1, 2, ["path", "path"]))

    def test_a_held_lock_blocks_another_process(self):
        holder = subprocess.Popen(
            [sys.executable, "-c", "import sys, time; sys.path.insert(0, sys.argv[1]); import ea_desk; from pathlib import Path\n"
             "with ea_desk.ledger_lock(Path(sys.argv[2])):\n print('held', flush=True); time.sleep(1.0)",
             str(Path(desk.__file__).parent), str(self.ledger)],
            stdout=subprocess.PIPE, text=True, env=os.environ.copy())
        self.assertEqual(holder.stdout.readline().strip(), "held")
        started = time.monotonic()
        with desk.ledger_lock(self.ledger):
            waited = time.monotonic() - started
        holder.stdout.close()
        holder.wait(timeout=10)
        self.assertGreater(waited, 0.5)
