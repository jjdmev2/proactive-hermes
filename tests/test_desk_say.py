"""Offline delivery, outbox, and CLI regressions for desk_say (canonical Bot Chat door path).

Every test runs with DESK_SAY_DISABLE_DOOR=1 and door_deliver mocked: no test can reach the owner's chat.
"""

import fcntl
import io
import json
import os
import sqlite3
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))


def isolated_env(directory):
    """HOME and HERMES_HOME in DIRECTORY: config and state are that profile's own (no override variable)."""
    return {"HOME": str(directory), "HERMES_HOME": str(directory), "DESK_SAY_DISABLE_DOOR": "1"}


with TemporaryDirectory() as directory, patch.dict(os.environ, isolated_env(Path(directory))):
    import desk_say as say
    import phconf

REAL_DOOR = say.door_deliver
REAL_HIDE = say.hide_relay_cards
REAL_QUIET = say.quiet_now


class DeskSayTests(unittest.TestCase):
    SID = "offline-bot-chat"

    def setUp(self):
        tmp = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, isolated_env(tmp)))
        self.enterContext(patch.object(say, "quiet_now", return_value=False))   # quiet hours tested on their own
        phconf.config_path().parent.mkdir(parents=True, exist_ok=True)
        phconf.config_path().write_text(json.dumps({
            "owner": {"name": "Alex"}, "timezone": "Asia/Tokyo", "quiet_hours": {"start": 13, "end": 15},
        }))
        self.enterContext(patch.object(phconf, "CFG", phconf.load()))
        self.enterContext(patch.multiple(
            say, STATE=tmp, OUTBOX=tmp / "say_outbox.jsonl", SAY_LOG=tmp / "say_log.jsonl",
            STATE_DB=tmp / "state.db", PROFILE=tmp, AGENT=tmp,
        ))
        self.tip = self.enterContext(patch.object(say, "bot_chat_tip", return_value=self.SID))
        self.active = self.enterContext(patch.object(say, "turn_active", return_value=False))
        self.door = self.enterContext(patch.object(say, "door_deliver", side_effect=self._door_answer))
        self.hide = self.enterContext(patch.object(say, "hide_relay_cards", return_value=1))
        self.sleep = self.enterContext(patch.object(say.time, "sleep"))

    def _door_answer(self, mid, text, notes=""):
        return {"status": "queued", "admission_id": "adm-" + mid[:6], "session_id": self.SID}

    def texts(self):
        return [c.args[1] for c in self.door.call_args_list]

    @staticmethod
    def rows(path):
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as stream:
            return [json.loads(line) for line in stream]

    def cli(self, *argv):
        output = io.StringIO()
        with patch.object(sys, "argv", [say.__file__, *argv]), redirect_stdout(output):
            self.assertEqual(say.main(), 0)
        return output.getvalue()

    # --- delivery -------------------------------------------------------------------------
    def test_delivers_through_door_once_and_hides_relay_card(self):
        text = "Desk update"
        mid = sha256(b"k\nDesk update").hexdigest()[:24]
        receipt = say.deliver(text, "k", job="brief", wait_s=0)
        self.assertEqual(say.msg_id("k", text), mid)
        self.assertEqual((receipt["status"], receipt["id"], receipt["push"], receipt["admission_id"]),
                         ("delivered", mid, "door", "adm-" + mid[:6]))
        self.assertEqual(receipt["session_id"], self.SID)
        self.door.assert_called_once_with(mid, text, "")
        self.hide.assert_called_once()
        self.assertEqual(say.delivered_ids(), {mid})
        self.assertEqual(self.rows(say.OUTBOX), [])

    def test_same_key_and_text_is_duplicate(self):
        receipt = say.deliver("text", "k", wait_s=0)
        self.assertEqual(say.deliver("text", "k", wait_s=0), {"status": "duplicate", "id": receipt["id"]})
        self.assertEqual(self.door.call_count, 1)

    def test_refused_door_is_unverified_not_queued_or_retried(self):
        self.door.side_effect = None
        self.door.return_value = {"status": "refused", "refused": True, "reason": "admission_conflict"}
        receipt = say.deliver("text", "k", wait_s=0)
        self.assertEqual(receipt["status"], "unverified")
        self.assertIn("admission_conflict", receipt["reason"])
        self.assertEqual(self.rows(say.OUTBOX), [])
        self.assertEqual(say.flush(), [])
        self.assertEqual(say.deliver("text", "k", wait_s=0)["status"], "duplicate")
        self.assertEqual(self.door.call_count, 1)
        self.hide.assert_not_called()

    def test_door_exception_queues_then_flush_delivers(self):
        self.door.side_effect = OSError("gateway down")
        receipt = say.deliver("text", "k", wait_s=0)
        self.assertEqual(receipt["status"], "queued")
        self.assertIn("OSError: gateway down", receipt["reason"])
        self.assertEqual(say.delivered_ids(), set())
        self.door.side_effect = self._door_answer
        delivered, = say.flush()
        self.assertEqual((delivered["status"], delivered["id"]), ("delivered", receipt["id"]))
        self.assertEqual(self.rows(say.OUTBOX), [])
        self.assertEqual(self.door.call_count, 2)  # same id both times: the door dedupes on it

    def test_active_turn_queues_then_flushes_once(self):
        self.active.return_value = True
        queued = say.deliver("text", "k", wait_s=0)
        self.assertEqual(queued["status"], "queued")
        self.assertIn("mid-turn", queued["reason"])
        self.door.assert_not_called()
        self.sleep.assert_not_called()
        self.active.return_value = False
        receipt, = say.flush()
        self.assertEqual((receipt["status"], receipt["id"]), ("delivered", queued["id"]))
        self.assertEqual(say.OUTBOX.read_text(), "")
        self.assertEqual(say.flush(), [])
        self.assertEqual(self.texts(), ["text"])

    def test_push_flag_is_accepted_and_survives_queue(self):
        self.active.return_value = True
        say.deliver("queued", "k", wait_s=0, push_enabled=False)
        row, = self.rows(say.OUTBOX)
        self.assertIs(row["push"], False)
        self.active.return_value = False
        receipt, = say.flush()
        self.assertEqual(receipt["status"], "delivered")

    def test_repeat_queue_preserves_retry_count(self):
        self.active.return_value = True
        queued = say.deliver("text", "k", wait_s=0)
        self.assertEqual(say.flush(), [])
        self.assertEqual(self.rows(say.OUTBOX)[0]["attempts"], 1)
        self.assertEqual(say.deliver("text", "k", wait_s=0)["id"], queued["id"])
        self.assertEqual(say.flush(), [])
        self.assertEqual(self.rows(say.OUTBOX)[0]["attempts"], 2)
        self.active.return_value = False
        receipt, = say.flush()
        self.assertEqual(receipt["status"], "delivered")
        self.assertEqual(self.door.call_count, 1)

    def test_flush_can_deliver_while_foreground_sender_waits(self):
        self.active.return_value = True
        queued = say.deliver("text", "k", wait_s=0)

        def release_lease(_seconds):
            with (say.STATE / "say.lock").open("a") as probe:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.active.return_value = False
            receipt, = say.flush()
            self.assertEqual((receipt["status"], receipt["id"]), ("delivered", queued["id"]))

        self.sleep.side_effect = release_lease
        self.assertEqual(say.deliver("text", "k", wait_s=1), {"status": "duplicate", "id": queued["id"]})
        self.assertEqual(self.door.call_count, 1)

    def test_flush_does_not_repeat_an_interrupted_post(self):
        self.active.return_value = True
        queued = say.deliver("text", "k", wait_s=0)
        self.active.return_value = False
        self.door.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            say.flush()
        self.door.side_effect = self._door_answer
        self.assertEqual(say.flush(), [])
        self.assertEqual(self.rows(say.OUTBOX), [])
        self.assertEqual(say.deliver("text", "k", wait_s=0), {"status": "duplicate", "id": queued["id"]})
        self.assertEqual(self.door.call_count, 1)

    def test_flush_advances_attempts_and_logs_abandonment_at_limit(self):
        self.door.side_effect = OSError("gateway down")
        queued = say.deliver("text", "k", wait_s=0)
        for attempts in range(1, say.MAX_ATTEMPTS):
            with self.subTest(attempts=attempts):
                self.assertEqual(say.flush(), [])
                row, = self.rows(say.OUTBOX)
                self.assertEqual((row["id"], row["attempts"]), (queued["id"], attempts))
        self.assertEqual(say.flush(), [])
        row, = [r for r in self.rows(say.SAY_LOG) if r["status"] == "abandoned"]
        self.assertEqual((row["id"], row["key"]), (queued["id"], "k"))
        self.assertEqual(self.rows(say.OUTBOX), [])
        self.assertEqual(self.door.call_count, say.MAX_ATTEMPTS + 1)

    def test_empty_text_does_not_deliver_or_queue(self):
        self.assertEqual(say.deliver(" \n\t", "k", wait_s=0), {"status": "empty"})
        self.tip.assert_not_called()
        self.door.assert_not_called()
        self.assertFalse(say.OUTBOX.exists())
        self.assertFalse(say.SAY_LOG.exists())

    def test_receipt_write_failure_after_door_does_not_retry(self):
        write = say._append

        def fail_receipt(path, row):
            if path == say.SAY_LOG and row["status"] == "delivered":
                raise OSError("receipt write failed")
            write(path, row)

        with patch.object(say, "_append", side_effect=fail_receipt):
            receipt = say.deliver("text", "k", wait_s=0)
        self.assertEqual(receipt["status"], "delivered")
        self.assertIn("receipt write failed", receipt["log_error"])
        self.assertEqual(say.flush(), [])
        self.assertEqual(say.deliver("text", "k", wait_s=0)["status"], "duplicate")
        self.assertEqual(self.door.call_count, 1)

    def test_wait_rechecks_the_chat_tip(self):
        new_tip = "compressed-tip"
        self.tip.side_effect = [self.SID, new_tip]
        self.active.side_effect = [True, False]
        with patch.object(say.time, "monotonic", side_effect=[100, 100, 101]):
            receipt = say.deliver("text", "k", wait_s=1)
        self.sleep.assert_called_once_with(1)
        self.assertEqual(receipt["status"], "delivered")

    def test_unicode_line_separator_survives_flush(self):
        text = "first\u2028second"
        self.active.return_value = True
        self.assertEqual(say.deliver(text, "k", wait_s=0)["status"], "queued")
        self.active.return_value = False
        receipt, = say.flush()
        self.assertEqual(receipt["status"], "delivered")
        self.assertEqual(self.texts(), [text])

    # --- quiet hours ----------------------------------------------------------------------
    def test_quiet_now_uses_configured_timezone_and_same_day_hours(self):
        with patch.dict(os.environ, DESK_SAY_QUIET_OFF="1"), patch.object(say, "datetime") as clock:
            for utc_hour, quiet in ((3, False), (4, True), (5, True), (6, False)):
                with self.subTest(utc_hour=utc_hour):
                    utc = datetime(2030, 4, 1, utc_hour, tzinfo=timezone.utc)
                    clock.now.side_effect = utc.astimezone
                    self.assertEqual(REAL_QUIET(), quiet)   # no environment switch turns quiet hours off

    def test_quiet_hours_hold_non_urgent_until_morning(self):
        with patch.object(say, "quiet_now", return_value=True):
            queued = say.deliver("night text", "k", wait_s=0)
            self.assertEqual((queued["status"], queued["reason"]), ("queued", "quiet hours"))
            self.assertEqual(say.deliver("night text", "k", wait_s=0)["status"], "queued")
            self.assertEqual(say.flush(), [])
            row, = self.rows(say.OUTBOX)
            self.assertEqual(row["attempts"], 0)  # holding is not a failed attempt
        self.door.assert_not_called()
        receipt, = say.flush()  # 07:00: delivered once
        self.assertEqual((receipt["status"], receipt["id"]), ("delivered", queued["id"]))
        self.assertEqual(self.door.call_count, 1)

    def test_urgent_goes_through_quiet_hours(self):
        with patch.object(say, "quiet_now", return_value=True):
            receipt = say.deliver("P0", "k", wait_s=0, urgent=True)
        self.assertEqual(receipt["status"], "delivered")
        self.door.assert_called_once()

    def test_urgent_flag_survives_queue_through_quiet_hours(self):
        self.door.side_effect = OSError("gateway down")
        say.deliver("P0", "k", wait_s=0, urgent=True)
        self.door.side_effect = self._door_answer
        with patch.object(say, "quiet_now", return_value=True):
            receipt, = say.flush()
        self.assertEqual(receipt["status"], "delivered")

    # --- CLI ------------------------------------------------------------------------------
    def test_cli_flush_is_silent_even_when_it_delivers(self):
        self.active.return_value = True
        say.deliver("text", "k", wait_s=0)
        self.active.return_value = False
        self.assertEqual(self.cli("flush"), "")
        self.assertEqual(self.door.call_count, 1)

    def test_cli_say_prints_exactly_one_json_line(self):
        line, = self.cli("say", "--key", "k", "text").splitlines()
        receipt = json.loads(line)
        self.assertEqual((receipt["status"], receipt["key"], receipt["push"]), ("delivered", "k", "door"))

    def test_cli_no_push_is_accepted(self):
        receipt = json.loads(self.cli("say", "--key", "k", "--no-push", "text"))
        self.assertEqual(receipt["status"], "delivered")

    # --- safety guards --------------------------------------------------------------------
    def test_real_door_refuses_under_test_guard(self):
        with patch.dict(sys.modules, {"tools.bot_live_delivery": None}):
            with self.assertRaisesRegex(RuntimeError, "door disabled"):
                REAL_DOOR("abc", "text")

    def test_relay_message_wraps_text_verbatim(self):
        msg = say.relay_message("0123456789", "line one\nline two")
        self.assertTrue(msg.startswith("DESK RELAY 01234567."))
        self.assertIn("<<<\nline one\nline two\n>>>", msg)

    def test_relay_notes_are_outside_verbatim_markers(self):
        text = " first line\nsecond line \n"
        notes = "1 = item-one\n2 = item-two"
        for value in ("", notes):
            with self.subTest(notes=value):
                msg = say.relay_message("0" * 24, text, notes=value)
                header, body = msg.split("\n<<<\n", 1)
                self.assertEqual(body, text + "\n>>>")
                if value:
                    self.assertTrue(header.endswith("\nNOTES FOR YOU, never shown to Alex and never repeated:\n" + notes))
                else:
                    self.assertNotIn("NOTES FOR YOU", header)

    def test_empty_notes_relay_matches_legacy_bytes(self):
        expected = (b"DESK RELAY 01234567. Reply to Alex with exactly the text between the markers, "
                    b"word for word, nothing before or after, no tools.\n<<<\nline one\nline two\n>>>")
        self.assertEqual(say.relay_message("0123456789", "line one\nline two").encode(), expected)
        self.assertEqual(say.relay_message("0123456789", "line one\nline two", notes="").encode(), expected)

    def test_notes_do_not_change_id_or_dedupe(self):
        first = say.deliver("text", "k", wait_s=0, notes="1 = k")
        second = say.deliver("text", "k", wait_s=0)
        self.assertEqual(first["status"], "delivered")
        self.assertEqual(first["id"], sha256(b"k\ntext").hexdigest()[:24])
        self.assertEqual(second, {"status": "duplicate", "id": first["id"]})
        self.door.assert_called_once_with(first["id"], "text", "1 = k")

    def test_notes_survive_quiet_hours_hold(self):
        notes = "1 = k\nprivate context"
        with patch.object(say, "quiet_now", return_value=True):
            queued = say.deliver("text", "k", wait_s=0, notes=notes)
            self.assertEqual(queued["status"], "queued")
            self.assertEqual(say.flush(), [])
            row, = self.rows(say.OUTBOX)
            self.assertEqual(row["notes"], notes)
        receipt, = say.flush()
        self.assertEqual(receipt["id"], queued["id"])
        self.door.assert_called_once_with(queued["id"], "text", notes)

    def test_queued_notes_survive_failed_door_and_retry_without_notes(self):
        notes = "1 = k"
        self.door.side_effect = OSError("gateway down")
        queued = say.deliver("text", "k", wait_s=0, notes=notes)
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(say.deliver("text", "k", wait_s=0)["status"], "queued")
        self.assertEqual(self.rows(say.OUTBOX)[-1]["notes"], notes)
        self.assertEqual(say.flush(), [])
        self.assertEqual(self.rows(say.OUTBOX)[0]["notes"], notes)
        self.door.side_effect = self._door_answer
        receipt, = say.flush()
        self.assertEqual(receipt["status"], "delivered")
        self.assertEqual(receipt["id"], queued["id"])
        self.assertEqual([call.args[2] for call in self.door.call_args_list], [notes] * 4)

    def test_flush_accepts_outbox_rows_written_before_notes(self):
        mid = say.msg_id("k", "text")
        say._append(say.OUTBOX, {"id": mid, "key": "k", "text": "text"})
        receipt, = say.flush()
        self.assertEqual(receipt["status"], "delivered")
        self.door.assert_called_once_with(mid, "text", "")

    def test_cli_say_passes_notes(self):
        receipt = json.loads(self.cli("say", "--key", "k", "--notes", "1 = k", "text"))
        self.assertEqual(receipt["status"], "delivered")
        self.door.assert_called_once_with(receipt["id"], "text", "1 = k")


class HideRelayCardsTests(unittest.TestCase):
    """hide_relay_cards against a throwaway sqlite file, never the live state.db."""

    def setUp(self):
        tmp = Path(self.enterContext(TemporaryDirectory()))
        self.db_path = tmp / "state.db"
        db = sqlite3.connect(self.db_path)
        db.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT);
            CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                                   display_metadata TEXT);
            INSERT INTO sessions VALUES ('old', NULL), ('tip', 'old'), ('other', NULL);
            INSERT INTO messages VALUES
              (1, 'old', 'user', 'DESK RELAY aaaa. Reply...', '{"gateway_input_owner": "x"}'),
              (2, 'old', 'assistant', 'the reply', NULL),
              (3, 'tip', 'user', 'DESK RELAY bbbb. Reply...', NULL),
              (4, 'tip', 'user', 'a genuine message from Alex', NULL),
              (5, 'other', 'user', 'DESK RELAY cccc. Reply...', NULL),
              (6, 'other', 'assistant', 'reply elsewhere', NULL);
        """)
        db.commit()
        db.close()
        self.enterContext(patch.dict(os.environ, isolated_env(tmp)))
        self.enterContext(patch.object(say, "STATE_DB", self.db_path))
        self.enterContext(patch.object(say, "bot_chat_tip", return_value="tip"))

    def meta(self, rid):
        db = sqlite3.connect(self.db_path)
        try:
            raw = db.execute("SELECT display_metadata FROM messages WHERE id=?", (rid,)).fetchone()[0]
        finally:
            db.close()
        return json.loads(raw) if raw else None

    def test_hides_only_answered_relay_cards_in_the_bot_chat_lineage(self):
        self.assertEqual(say._hide_relay_cards(0), 1)
        self.assertEqual(self.meta(1), {"gateway_input_owner": "x", "model_only": True})
        self.assertIsNone(self.meta(3))  # no reply yet: stays until the reply lands
        self.assertIsNone(self.meta(4))  # the owner's own message: never touched
        self.assertIsNone(self.meta(5))  # another session: never touched
        self.assertEqual(say._hide_relay_cards(0), 0)  # idempotent

    def test_disabled_under_test_guard(self):
        with patch.dict("os.environ", DESK_SAY_DISABLE_DOOR="1"):
            self.assertEqual(REAL_HIDE(), 0)
        self.assertEqual(self.meta(1), {"gateway_input_owner": "x"})


if __name__ == "__main__":
    unittest.main()
