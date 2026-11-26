"""Offline outbox, say-job and CLI regressions for desk_say (public cron Bot Chat delivery).

Every test runs with DESK_SAY_DISABLE_DOOR=1 and run_say_job mocked: no test can reach the owner's chat.
"""

import io
import json
import os
import subprocess
import sys
import time
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
        self.enterContext(patch.multiple(say, STATE=tmp, OUTBOX=tmp / "say_outbox.jsonl", SAY_LOG=tmp / "say_log.jsonl",
                                         STATUS_LOG=tmp / "say_status.json"))
        self.job = self.enterContext(patch.object(say, "say_job_id", return_value=JID))
        self.route = self.enterContext(patch.object(say, "say_route", return_value=("bot-chat",)))
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
        """The messages between the start and end markers of each printed relay."""
        out = []
        for p in self.printed:
            head, rest = p.split("\n" + say.START + "\n", 1)
            self.assertTrue(head.startswith(say.RELAY_TAG))
            self.assertTrue(rest.endswith("\n" + say.END + "\n"), rest)
            out.append(rest.removesuffix("\n" + say.END + "\n"))
        return out

    @staticmethod
    def relay(mid, text):
        """The Bot Chat input turn for one say job run: Hermes's cron header, then the job's output."""
        return ("user", '[Cronjob "Proactive · say" output — scheduled job, not the user. Review it, act on anything '
                        'that needs action, and summarize for the chat.]\n\n' + say.job_output(text, mid))

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
        self.assertEqual(self.printed[0], say.job_output("Desk update", receipt["id"]) + "\n")
        self.assertEqual(self.rows(say.OUTBOX), [])
        self.assertEqual([r["status"] for r in self.rows(say.SAY_LOG)], ["handed"])
        self.assertEqual(say.deliver("Desk update", key="k", job="test")["status"], "duplicate")
        self.assertEqual(self.runs.call_count, 1)

    def test_job_output_is_a_relay_line_and_the_text_between_markers(self):
        text = "Morning Alex, 2 things:\n1. Reply to Sam\n2. Pay the bill \u2028 today"
        mid = say.msg_id("k", text)
        out = say.job_output(text, mid)
        head, body = out.split("\n", 1)
        self.assertEqual(body, f"{say.START}\n{text}\n{say.END}")
        self.assertIn(mid[:8], head)
        for words in ("not a task", "Do not review, act on, summarize", "use no tools", "word for word",
                      "between " + say.START + " and " + say.END):
            self.assertIn(words, head)
        self.assertIn("Alex", head)
        self.assertNotIn("DESK RELAY", out)
        self.assertNotIn("never shown", out)

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
    def test_confirm_matches_the_reply_to_each_relay_inside_the_markers(self):
        one, two = say.deliver("Line one", key="a")["id"], say.deliver("Line two", key="b")["id"]
        turns = [self.relay(one, "Line one"), ("assistant", "Line one"),
                 self.relay(two, "Line two"), ("assistant", "Here is a summary: line two.")]
        self.assertEqual(say.confirm(turns=turns), 1)
        self.assertEqual(say.delivered_ids(), {one})
        # The Bot kept the markers this time: the text between them still counts.
        turns += [("assistant", f"{say.START}\nLine two\n{say.END}")]
        turns[-2:] = [turns[-1]]
        self.assertEqual(say.confirm(turns=turns), 1)
        self.assertEqual(say.delivered_ids(), {one, two})
        self.assertEqual(say.confirm(turns=turns), 0)

    def test_one_reply_confirms_at_most_one_message(self):
        a = say.deliver("Same words", key="a")["id"]
        b = say.deliver("Same words", key="b")["id"]
        self.assertNotEqual(a, b)
        turns = [self.relay(a, "Same words"), self.relay(b, "Same words"), ("assistant", "Same words")]
        self.assertEqual(say.confirm(turns=turns), 1)
        self.assertEqual(say.delivered_ids(), {a})
        self.assertEqual(say.confirm(turns=turns + [("assistant", "Same words")]), 1)
        self.assertEqual(say.delivered_ids(), {a, b})

    def test_a_reply_to_another_relay_or_an_owner_turn_never_confirms(self):
        mine = say.deliver("Pay the bill", key="a")["id"]
        other = say.msg_id("z", "Pay the bill")
        turns = [self.relay(other, "Pay the bill"), ("assistant", "Pay the bill"),
                 ("user", "what's next?"), ("assistant", "Pay the bill")]
        self.assertEqual(say.confirm(turns=turns), 0)   # its own relay turn is not there and no loose reply fits
        self.assertEqual(say.delivered_ids(), set())
        self.assertEqual(say.status_of(mine), "handed")

    def test_without_relay_turns_in_the_chat_a_loose_reply_may_confirm_once(self):
        mid = say.deliver("Line one", key="a")["id"]
        self.assertEqual(say.confirm(turns=[("assistant", "Line one")]), 1)
        self.assertEqual(say.delivered_ids(), {mid})

    def test_confirm_counts_a_message_another_worker_confirmed_as_done(self):
        mid = say.deliver("Line one", key="a")["id"]
        turns = [self.relay(mid, "Line one"), ("assistant", "Line one")]
        self.assertEqual(say.confirm(turns=turns), 1)
        self.assertEqual(say.confirm(turns=turns), 0)   # nothing new, but it is delivered
        self.assertEqual(say.status_of(mid), "delivered")

    def test_a_handed_row_with_no_reply_becomes_unverified_once_after_24h_and_is_not_reread(self):
        mid = say.deliver("Token sk-proj-abc123456789", key="a")["id"]
        later = time.time() + say.UNVERIFIED_AFTER_S + 60
        reads = []
        with patch.object(say, "bot_chat_turns", side_effect=lambda: reads.append(1) or []):
            self.assertEqual(say.confirm(now_s=time.time()), 0)
            self.assertEqual(len(reads), 1)
            self.assertEqual(say.confirm(now_s=later), 0)
            self.assertEqual(say.confirm(now_s=later + 3600), 0)
        self.assertEqual(len(reads), 1)   # once unverified, Bot Chat is not read for it again
        statuses = [r["status"] for r in self.rows(say.SAY_LOG) if r["id"] == mid]
        self.assertEqual(statuses, ["handed", "unverified"])

    def test_confirm_never_raises_without_a_store(self):
        say.deliver("x", key="k")
        with patch.object(say, "bot_chat_turns", side_effect=RuntimeError("no store")):
            self.assertEqual(say.confirm(), 0)

    # --- the say job's route (fail closed) ------------------------------------------------
    def test_say_job_takes_nothing_unless_it_delivers_to_bot_chat_only(self):
        for route in (("local",), ("bot-chat", "telegram"), (), None):
            with self.subTest(route=route):
                self.route.return_value = route
                with patch.object(say, "kick", return_value={}):
                    say.deliver(f"Held {route}", key=f"k{route}")
                with patch.object(say, "confirm", return_value=0):
                    self.assertEqual(self.cli("next"), "")
                self.assertTrue(say.route_status()["route_problem"])
        self.assertEqual(len(self.rows(say.OUTBOX)), 4)
        self.assertEqual(self.rows(say.SAY_LOG), [])
        self.route.side_effect = RuntimeError("no cron")
        self.assertIn("unreadable", say.route_problem())
        self.route.side_effect, self.route.return_value = None, ("bot-chat",)
        say.flush()
        self.assertEqual(len(self.texts()), 4)
        self.assertIsNone(say.route_status()["route_problem"])

    # --- the kick -------------------------------------------------------------------------
    def test_a_kick_that_times_out_detaches_and_never_kills_the_run(self):
        class Proc:
            killed = False

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("hermes", timeout)

            def kill(self):
                Proc.killed = True
            terminate = kill
        started = {}

        def popen(argv, **kwargs):
            started.update(argv=argv, **kwargs)
            return Proc()
        with patch.dict(os.environ, {"DESK_SAY_DISABLE_DOOR": "0"}), patch.object(say.subprocess, "Popen", popen):
            self.assertIsNone(REAL_RUN(JID))
        self.assertEqual(started["argv"][1:], ["cron", "run", JID])
        self.assertTrue(started["start_new_session"])
        self.assertFalse(Proc.killed)
        self.runs.side_effect = lambda jid: None
        receipt = say.deliver("Slow", key="k")
        self.assertEqual(receipt["kick"], "still running (detached)")
        self.assertEqual(self.runs.call_count, 1)

    def test_an_urgency_upgrade_of_a_queued_message_is_kept(self):
        self.quiet.return_value = True
        with patch.object(say, "kick", return_value={}):
            say.deliver("Meeting soon", key="one")
            say.deliver("Meeting soon", key="one", urgent=True)
        self.assertEqual(say.pending_due(), 1)   # urgent now: due even in quiet hours
        self.assertTrue(say._pending(self.rows(say.OUTBOX))[say.msg_id("one", "Meeting soon")]["urgent"])
        say.flush()
        self.assertEqual(self.texts(), ["Meeting soon"])

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
        self.assertEqual(out.getvalue(), say.job_output("From the job", say.msg_id("k", "From the job")) + "\n")


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
