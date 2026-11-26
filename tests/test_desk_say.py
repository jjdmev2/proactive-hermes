"""Offline outbox, say-job and CLI regressions for desk_say (public cron Bot Chat delivery).

Every test runs with DESK_SAY_DISABLE_DOOR=1 and run_say_job mocked: no test can reach the owner's chat.
"""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
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

REAL_RUN = say.run_say_job
REAL_QUIET = say.quiet_now
JID = "0123456789ab"


class DeskSayTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, isolated_env(tmp)))
        self.quiet = self.enterContext(patch.object(say, "quiet_now", return_value=False))
        phconf.config_path().parent.mkdir(parents=True, exist_ok=True)
        phconf.config_path().write_text(json.dumps({
            "owner": {"name": "Alex"}, "timezone": "Asia/Tokyo", "quiet_hours": {"start": 13, "end": 15},
        }))
        self.enterContext(patch.object(phconf, "CFG", phconf.load()))
        self.enterContext(patch.multiple(say, STATE=tmp, OUTBOX=tmp / "say_outbox.jsonl", SAY_LOG=tmp / "say_log.jsonl"))
        self.job = self.enterContext(patch.object(say, "say_job_id", return_value=JID))
        self.printed: list[str] = []
        # The say job as cron runs it: `desk_say.py next`, whose stdout cron hands to Bot Chat.
        self.runs = self.enterContext(patch.object(say, "run_say_job", side_effect=self._run_job))

    def _run_job(self, jid):
        self.assertEqual(jid, JID)
        out = io.StringIO()
        with redirect_stdout(out), patch.object(say, "confirm", return_value=0):
            say.main(["next"])
        if out.getvalue():
            self.printed.append(out.getvalue())
        return True

    def texts(self):
        return [p.split("\n\n", 1)[1].removesuffix("\n") for p in self.printed]

    @staticmethod
    def rows(path):
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as stream:
            return [json.loads(line) for line in stream]

    def cli(self, *argv):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(say.main(list(argv)), 0)
        return output.getvalue()

    # --- delivery -------------------------------------------------------------------------
    def test_deliver_queues_then_the_say_job_prints_it_once(self):
        receipt = say.deliver("Desk update", key="k", job="test")
        self.assertEqual(receipt["status"], "handed")
        self.assertEqual(self.texts(), ["Desk update"])
        self.assertEqual(self.printed[0], say.INSTRUCTION + "\n\nDesk update\n")
        self.assertEqual(self.rows(say.OUTBOX), [])
        self.assertEqual([r["status"] for r in self.rows(say.SAY_LOG)], ["handed"])
        self.assertEqual(say.deliver("Desk update", key="k", job="test")["status"], "duplicate")
        self.assertEqual(self.runs.call_count, 1)

    def test_job_output_is_one_short_instruction_and_the_text_verbatim(self):
        text = "Morning Alex, 2 things:\n1. Reply to Sam\n2. Pay the bill \u2028 today"
        out = say.job_output(text)
        self.assertEqual(out, say.INSTRUCTION + "\n\n" + text)
        self.assertNotIn("DESK RELAY", out)
        self.assertNotIn("never shown", out)
        self.assertNotIn("\n", say.INSTRUCTION)

    def test_nothing_due_prints_nothing(self):
        self.assertEqual(self.cli("next"), "")
        self.assertEqual(self.rows(say.SAY_LOG), [])

    def test_kick_failure_keeps_the_message_queued_for_the_schedule(self):
        self.runs.side_effect = RuntimeError("cron down")
        receipt = say.deliver("Later", key="k", job="test")
        self.assertEqual(receipt["status"], "queued")
        self.assertIn("RuntimeError", receipt["kick"])
        self.assertEqual([r["text"] for r in self.rows(say.OUTBOX)], ["Later"])
        self.runs.side_effect = self._run_job
        say.flush()   # desk_tick's safety net
        self.assertEqual(self.texts(), ["Later"])
        self.assertEqual(self.rows(say.OUTBOX), [])

    def test_no_say_job_keeps_the_message_queued(self):
        self.job.return_value = None
        receipt = say.deliver("Wait", key="k")
        self.assertEqual(receipt["status"], "queued")
        self.assertIn("hermes proactive setup", receipt["kick"])
        self.runs.assert_not_called()

    def test_a_burst_drains_one_message_per_run(self):
        with patch.object(say, "kick", return_value={"kick": "held"}):
            for n in range(3):
                say.deliver(f"line {n}", key=f"k{n}")
        say.flush()
        self.assertEqual(self.texts(), ["line 0", "line 1", "line 2"])
        self.assertEqual(self.runs.call_count, 3)

    def test_kick_stops_when_a_run_takes_nothing(self):
        self.runs.side_effect = lambda jid: True   # the scheduler holds the job: nothing printed
        receipt = say.deliver("Held", key="k")
        self.assertEqual((receipt["status"], self.runs.call_count), ("queued", 1))

    def test_urgent_goes_first(self):
        with patch.object(say, "kick", return_value={}):
            say.deliver("normal", key="a")
            say.deliver("urgent", key="b", urgent=True)
        say.flush()
        self.assertEqual(self.texts(), ["urgent", "normal"])

    def test_empty_text_does_not_queue(self):
        self.assertEqual(say.deliver("  \n", key="k")["status"], "empty")
        self.assertFalse(say.OUTBOX.exists())

    def test_unicode_line_separator_survives_the_outbox(self):
        text = "first\u2028second"
        with patch.object(say, "kick", return_value={}):
            say.deliver(text, key="k")
        say.flush()
        self.assertEqual(self.texts(), [text])

    def test_a_handed_message_is_never_printed_again(self):
        say.deliver("Once", key="k")
        # A stale outbox row (e.g. a crash before the rewrite) cannot bring it back.
        with say.OUTBOX.open("a") as fh:
            fh.write(json.dumps({"id": say.msg_id("k", "Once"), "key": "k", "text": "Once"}) + "\n")
        with patch.object(say, "confirm", return_value=0):
            self.assertEqual(self.cli("next"), "")
        self.assertEqual(self.rows(say.OUTBOX), [])

    # --- quiet hours ----------------------------------------------------------------------
    def test_quiet_now_uses_configured_timezone_and_same_day_hours(self):
        with patch.object(say, "datetime") as clock:
            clock.now.side_effect = lambda tz: datetime(2026, 1, 1, 4, 30, tzinfo=timezone.utc).astimezone(tz)
            self.assertTrue(REAL_QUIET())   # 13:30 in Tokyo
            clock.now.side_effect = lambda tz: datetime(2026, 1, 1, 6, 30, tzinfo=timezone.utc).astimezone(tz)
            self.assertFalse(REAL_QUIET())  # 15:30 in Tokyo

    def test_quiet_hours_hold_non_urgent_until_morning(self):
        self.quiet.return_value = True
        receipt = say.deliver("Morning item", key="k")
        self.assertEqual((receipt["status"], receipt["reason"]), ("queued", "quiet hours"))
        self.runs.assert_not_called()
        with patch.object(say, "confirm", return_value=0):
            self.assertEqual(self.cli("next"), "")   # a scheduled run at night takes nothing
        self.quiet.return_value = False
        self.cli("flush")
        self.assertEqual(self.texts(), ["Morning item"])

    def test_urgent_goes_through_quiet_hours(self):
        self.quiet.return_value = True
        self.assertEqual(say.deliver("Fire", key="k", urgent=True)["status"], "handed")
        self.assertEqual(self.texts(), ["Fire"])

    def test_urgent_flag_survives_queue_through_quiet_hours(self):
        self.quiet.return_value = True
        self.runs.side_effect = RuntimeError("down")
        say.deliver("Fire", key="k", urgent=True)
        self.runs.side_effect = self._run_job
        say.flush()
        self.assertEqual(self.texts(), ["Fire"])

    # --- confirmation (read only) ---------------------------------------------------------
    def test_confirm_marks_delivered_only_when_the_exact_reply_is_in_bot_chat(self):
        say.deliver("Line one", key="a")
        say.deliver("Line two", key="b")
        self.assertEqual(say.confirm(replies=["Line one", "Line two, reworded"]), 1)
        self.assertEqual(say.delivered_ids(), {say.msg_id("a", "Line one")})
        self.assertEqual(say.confirm(replies=["Line two"]), 1)
        self.assertEqual(len(say.delivered_ids()), 2)
        self.assertEqual(say.confirm(replies=["Line two"]), 0)

    def test_confirm_never_raises_without_a_store(self):
        say.deliver("x", key="k")
        with patch.object(say, "bot_chat_replies", side_effect=RuntimeError("no store")):
            self.assertEqual(say.confirm(), 0)

    # --- CLI ------------------------------------------------------------------------------
    def test_cli_flush_is_silent_even_when_it_delivers(self):
        with patch.object(say, "kick", return_value={}):
            say.deliver("Quiet", key="k")
        self.assertEqual(self.cli("flush"), "")
        self.assertEqual(self.texts(), ["Quiet"])

    def test_cli_say_prints_exactly_one_json_line(self):
        output = self.cli("say", "--key", "k", "Hello")
        self.assertEqual(len(output.splitlines()), 1)
        self.assertEqual(json.loads(output)["status"], "handed")

    def test_cli_has_no_notes_flag(self):
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                patch.object(sys, "stderr", io.StringIO()):
            say.main(["say", "--key", "k", "--notes", "private", "x"])

    def test_real_kick_refuses_under_test_guard(self):
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            REAL_RUN(JID)

    def test_say_job_script_prints_one_message(self):
        import runpy
        with patch.object(say, "kick", return_value={}):
            say.deliver("From the job", key="k")
        out = io.StringIO()
        with redirect_stdout(out), patch.object(say, "confirm", return_value=0), \
                self.assertRaises(SystemExit) as done:
            runpy.run_path(str(Path(say.__file__).with_name("say_job.py")), run_name="__main__")
        self.assertEqual(done.exception.code, 0)
        self.assertEqual(out.getvalue(), say.INSTRUCTION + "\n\nFrom the job\n")


class NoPrivateDeliveryOrStateDbWrites(unittest.TestCase):
    def test_runtime_never_writes_hermes_state_db_or_uses_the_private_door(self):
        runtime = Path(say.__file__).resolve().parent
        for path in runtime.glob("*.py"):
            text = path.read_text()
            with self.subTest(file=path.name):
                for needle in ("UPDATE messages", "INSERT INTO messages", "tools.bot_live_delivery",
                               "_gateway_runner_ref", "from cron.jobs import"):
                    self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
