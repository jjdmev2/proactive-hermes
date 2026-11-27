"""Offline CLI and queue regressions for the desk scripts."""

import io
import json
import os
import shutil
import subprocess
import sys
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))


def isolated_env(directory):
    """HOME and an isolated Hermes profile under DIRECTORY. The plugin's config and state are then found exactly as in
    production, at plugin_data(DIRECTORY)/config.json and .../state: no environment variable redirects them."""
    return {"HOME": str(directory), "HERMES_HOME": str(directory / "profiles" / "desk-test"),
            "DESK_SAY_DISABLE_DOOR": "1"}


def plugin_data(directory):
    return Path(directory) / "profiles" / "desk-test" / "plugin-data" / "proactive-hermes"


with TemporaryDirectory() as directory, patch.dict(os.environ, isolated_env(Path(directory))):
    import desk_say
    import ea_desk as desk
    try:
        import ea_monitor_bridge as monitor
    except ModuleNotFoundError as exc:
        if exc.name != "ea_monitor_bridge":
            raise
        monitor = None  # not packaged in every checkout
    import ea_reminders as reminders
    import ea_resolve as resolve
    import phconf
    import work_recap_gate as recap


SILENT = "[SILENT]\n"


class RenderMessageTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(phconf, "CFG", phconf.merge(phconf.DEFAULTS, {"owner": {"name": "Alex"}})))

    def test_single_item_has_no_number_or_default_header(self):
        self.assertEqual(desk.render_message(None, ["Reply to Sam."], []), "Reply to Sam.")

    def test_four_items_are_numbered_on_consecutive_lines(self):
        self.assertEqual(desk.render_message("", ["One", "Two", "Three", "Four"], []),
                         "Hey Alex, 4 things:\n1. One\n2. Two\n3. Three\n4. Four")

    def test_long_item_uses_last_sentence_end_at_or_before_140(self):
        first = "Reply to Sam before tomorrow morning so he can reserve the slot."
        second = " " + "x" * (140 - len(first) - 2) + "?"
        self.assertEqual(desk.render_message(None, [first + second + " More context."], []), first + second)

    def test_long_item_with_only_short_sentence_uses_word_boundary_and_ellipsis(self):
        text = "Hi! " + "context " * 30
        self.assertEqual(desk.render_message(None, [text], []), "Hi!" + " context" * 17 + "…")

    def test_strips_markdown_urls_and_dash_runs_from_sample(self):
        text = ("## **Project update**: Acme — release watch -\n"
                " - *** NEW DATE ANNOUNCED ***\t"
                "check the __release dates__ on `acme.example/releases` - "
                "https://acme.example/calendar")
        self.assertEqual(desk.render_message(None, [text], []),
                         "Project update: Acme, release watch NEW DATE ANNOUNCED check the release dates on")

    def test_worth_only_shows_cleaned_labels_and_remaining_count(self):
        worth = ["Sam sent the quarterly report for the partner review next week",
                 "**Project update**: *** NEW DATE ANNOUNCED ***",
                 "Release sync https://acme.example/release: registration update",
                 "Fourth: raw text", "Fifth: raw text"]
        self.assertEqual(desk.render_message("Tonight:", ["Reply."], worth),
                         "Tonight:\nReply.\n\nAlso saved: Sam sent the quarterly report, "
                         "Project update, Release sync and 2 more (ask if you want details)")

    def test_header_over_60_chars_is_dropped(self):
        self.assertEqual(desk.render_message("x" * 61, ["Reply"], []), "Reply")
        self.assertEqual(desk.render_message("x" * 60, ["Reply"], []), "x" * 60 + "\nReply")

    def test_multiline_header_is_replaced_by_default(self):
        self.assertEqual(desk.render_message("Tonight:\nLater:", ["One", "Two"], []),
                         "Hey Alex, 2 things:\n1. One\n2. Two")

    def test_message_cap_omits_whole_trailing_items_before_worth(self):
        header = "h" * 59
        items = [letter * 140 for letter in "abcde"]
        worth = ["Release: monitor details"]
        message, kept = desk.render_parts(header, items, worth)
        self.assertLessEqual(len(message), 700)
        self.assertEqual(kept, 3)
        item_lines = [f"{i}. {text}" for i, text in enumerate(items[:3], 1)]
        self.assertEqual(message, "\n".join([header, *item_lines, "+2 more lined up, coming next (or ask me)", "",
                                             "Also saved: Release (ask if you want details)"]))
        self.assertEqual(desk.render_message(header, items, worth), message)


class OfflineTestCase(unittest.TestCase):
    """self.root is HOME, self.root/profiles/desk-test the Hermes home, self.tmp the plugin's state dir in it."""

    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, isolated_env(self.root)))
        self.tmp = phconf.state_dir()
        self.assertEqual(self.tmp, plugin_data(self.root) / "state")
        self.configure(owner={"name": "Alex"})
        self.clock = datetime(2030, 4, 1, 10, tzinfo=phconf.tz())

    def configure(self, **config):
        phconf.config_path().write_text(json.dumps(config))
        self.enterContext(patch.object(phconf, "CFG", phconf.load()))

    def patch_paths(self, module, *names):
        self.enterContext(patch.object(module, "STATE", self.tmp))
        for name in names:
            self.enterContext(patch.object(module, name, self.tmp / getattr(module, name).name))

    def freeze_datetime(self, module):
        fake_datetime = self.enterContext(patch.object(module, "datetime", wraps=datetime))
        fake_datetime.now.side_effect = lambda tz: self.clock.astimezone(tz)

    def cli(self, module, *args, stdin="", code=0):
        output = io.StringIO()
        with patch.object(sys, "argv", [module.__file__, *args]), \
                patch.object(sys, "stdin", io.StringIO(stdin)), redirect_stdout(output):
            self.assertEqual(module.main(), code)
        return output.getvalue()

    @staticmethod
    def rows(path):
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    @staticmethod
    def read_json(path):
        return json.loads(path.read_text())


class DeskTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.patch_paths(desk, "SAID", "DIGEST", "HELD", "PING_LOG", "RUN_LOG", "LEDGER", "PENDING", "RESOLVE_WAIT")
        self.enterContext(patch.object(desk, "now", lambda: self.clock))
        # compose queues with desk_say.enqueue (then records SAID) and kicks the say job after its lock.
        self.deliver = self.enterContext(patch.object(desk_say, "enqueue", return_value={"status": "queued", "id": "m1"}))
        self.kick = self.enterContext(patch.object(desk_say, "kick", return_value={"kick": "ok"}))
        # "Reply N" binds to messages the owner received: here every logged message counts as delivered.
        self.enterContext(patch.object(resolve, "_say_statuses", lambda: {
            resolve._message_id(r): "delivered" for r in self.rows(desk.PING_LOG)}))

    @staticmethod
    def item(key, kind="action", **fields):
        return {"key": key, "kind": kind, "text": f"Work on {key}", "change": "open", **fields}

    def compose(self, items, *args):
        return self.cli(desk, "compose", "--job", "test", "--print-only", *args, stdin=json.dumps(items))

    def queue(self, path, item):
        row = {"at": self.clock.isoformat(), **item}
        with path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        return row

    def test_speaks_once_and_respeaks_on_material_change(self):
        original = self.item("offer", change="proposal 375 open")
        self.assertEqual(self.compose([original]), "Work on offer\n")
        self.assertEqual(self.compose([original]), SILENT)
        changed = dict(original, change="offer 400 open", text="Review the 400 offer")
        self.assertEqual(self.compose([changed]), "Review the 400 offer\n")
        self.assertEqual([r["keys"] for r in self.rows(desk.PING_LOG)], [["offer"], ["offer"]])

    def test_reworded_but_unchanged_change_stays_quiet(self):
        original = self.item("offer", change="the offer is open for 375")
        self.assertNotEqual(self.compose([original]), SILENT)
        reworded = dict(original, text="The offer needs your review", change="375, offer still open for")
        self.assertEqual(self.compose([reworded]), SILENT)
        self.assertEqual(self.read_json(desk.SAID)["offer"]["text"], original["text"])

    def test_non_actionable_item_is_dropped(self):
        self.assertEqual(self.compose([self.item("news", "noise")]), SILENT)
        self.assertEqual(self.rows(desk.DIGEST), [])
        self.assertEqual(self.rows(desk.HELD), [])
        self.assertEqual(self.rows(desk.PING_LOG), [])
        self.assertEqual(self.rows(desk.RUN_LOG)[0]["quiet"], [["news", "not actionable"]])

    def test_caps_at_three_with_decisions_first(self):
        items = [self.item("a1"), self.item("a2"), self.item("d1", "decision"),
                 self.item("d2", "decision"), self.item("a3")]
        self.assertEqual(self.compose(items), "Hey Alex, 3 things:\n1. Work on d1\n2. Work on d2\n3. Work on a1\n"
                                              "+2 more lined up, coming next (or ask me)\n")
        overflow = self.rows(desk.DIGEST)
        self.assertEqual([r["key"] for r in overflow], ["a2", "a3"])
        self.assertTrue(all(r["overflow"] is True for r in overflow))

    def test_ranks_by_priority_waiting_due_then_age(self):
        soon = (self.clock + timedelta(hours=3)).isoformat()
        later = (self.clock + timedelta(days=2)).isoformat()
        items = [self.item("p2-decision", "decision", priority="p2"),
                 self.item("p1-late", due=later), self.item("p1-soon", due=soon),
                 self.item("p1-waiting", waiting=True), self.item("p0", priority="P0"),
                 self.item("urgent", urgent=True)]
        self.assertEqual(self.compose(items),
                         "Hey Alex, 3 things:\n1. Work on urgent\n2. Work on p0\n3. Work on p1-waiting\n"
                         "+3 more lined up, coming next (or ask me)\n")
        self.clock += timedelta(minutes=30)
        self.assertEqual(self.compose([]), "Hey Alex, 3 things:\n1. Work on p1-soon\n2. Work on p1-late\n"
                                           "3. Work on p2-decision\n")
        self.assertEqual(self.rows(desk.DIGEST), [])

    def test_rolled_over_items_beat_new_items_of_equal_rank(self):
        self.compose([self.item(f"old-{i}") for i in range(4)])
        self.clock += timedelta(minutes=15)
        self.assertEqual(self.compose([self.item("new-1"), self.item("new-2"), self.item("new-3")]),
                         "Hey Alex, 3 things:\n1. Work on old-3\n2. Work on new-1\n3. Work on new-2\n"
                         "+1 more lined up, coming next (or ask me)\n")
        self.assertEqual([r["key"] for r in self.rows(desk.DIGEST)], ["new-3"])

    def test_no_daily_cap_for_normal_items(self):
        for _ in range(30):
            self.queue(desk.PING_LOG, {"kind": "desk_message"})
        self.assertEqual(self.compose([self.item("late-in-the-day")]), "Work on late-in-the-day\n")
        self.assertFalse(hasattr(desk, "DAILY_CEILING"))

    def test_items_cut_for_length_roll_over_and_are_not_marked_said(self):
        long = [self.item(k, text=k * 139 + ".") for k in "abcde"]
        output = self.compose(long, "--brief", "--header", "h" * 59)
        self.assertIn("+1 more lined up", output)
        self.assertEqual(set(self.read_json(desk.SAID)), set("abcd"))
        self.assertEqual([r["key"] for r in self.rows(desk.DIGEST)], ["e"])

    def test_week_old_rollover_moves_to_digest_instead_of_vanishing(self):
        old = self.item("stale", overflow=True, at=(self.clock - timedelta(hours=169)).isoformat())
        desk.DIGEST.write_text(json.dumps(old) + "\n")
        self.assertEqual(self.compose([]), SILENT)
        row, = self.rows(desk.DIGEST)
        self.assertEqual((row["key"], row["kind"], row["expired_from"]), ("stale", "digest", "action"))
        self.assertEqual(row["at"], self.clock.isoformat(timespec="seconds"))
        self.assertIn(["stale", "expired 7d, moved to digest"], self.rows(desk.RUN_LOG)[-1]["quiet"])

    def test_flush_waits_an_hour_after_last_message_and_respects_quiet_hours(self):
        self.compose([self.item(f"t{i}") for i in range(4)])
        self.assertEqual(self.cli(desk, "flush", "--print-only"), SILENT)
        self.clock += timedelta(minutes=59)
        self.assertEqual(self.cli(desk, "flush", "--print-only"), SILENT)
        self.clock = self.clock.replace(hour=23, minute=30)
        self.assertEqual(self.cli(desk, "flush", "--print-only"), SILENT)
        self.assertEqual([r["key"] for r in self.rows(desk.DIGEST)], ["t3"])
        self.clock = self.clock.replace(hour=15, minute=0)
        self.assertEqual(self.cli(desk, "flush", "--print-only"), "Work on t3\n")
        self.assertEqual(self.rows(desk.DIGEST), [])
        self.assertEqual(self.cli(desk, "flush", "--print-only"), SILENT)

    def test_brief_decisions_resurface_before_actions(self):
        self.clock = self.clock.replace(hour=11)
        decision = self.item("decision", "decision")
        action = self.item("action")
        self.assertNotEqual(self.compose([decision, action]), SILENT)
        self.clock += timedelta(hours=20)
        self.assertEqual(self.compose([decision, action]), SILENT)
        self.assertEqual(self.compose([decision, action], "--brief"), "Work on decision\n")
        self.clock += timedelta(hours=28)
        self.assertEqual(self.compose([action]), "Work on action\n")

    def test_quiet_hours_hold_nonurgent_items(self):
        self.clock = self.clock.replace(hour=23)
        self.assertEqual(self.compose([self.item("later")]), SILENT)
        self.assertEqual([r["key"] for r in self.rows(desk.HELD)], ["later"])
        self.assertFalse(desk.SAID.exists())

    def test_urgent_bypasses_quiet_hours_but_not_six_hour_guard(self):
        self.clock = self.clock.replace(hour=0, microsecond=700_000)
        for _ in range(8):
            self.queue(desk.PING_LOG, {"kind": "desk_message"})
        urgent = self.item("urgent", urgent=True)
        self.assertEqual(self.compose([urgent, urgent]), "Work on urgent\n")
        self.assertEqual(self.rows(desk.HELD), [])
        self.assertEqual(self.rows(desk.DIGEST), [])
        self.clock += timedelta(hours=6) - timedelta(milliseconds=500)
        self.assertEqual(self.compose([urgent]), SILENT)
        self.clock += timedelta(milliseconds=500)
        self.assertEqual(self.compose([urgent]), "Work on urgent\n")
        self.assertEqual(len(self.rows(desk.PING_LOG)), 8 + 2)

    def test_morning_brief_picks_up_held_and_overflow_with_zero_digest_attachment(self):
        actions = [self.item(f"task-{i}") for i in range(4)]
        self.assertNotEqual(self.compose(actions), SILENT)
        self.clock = self.clock.replace(hour=23)
        self.assertEqual(self.compose([self.item("held", "decision")]), SILENT)
        plain = self.queue(desk.DIGEST, self.item("fyi", "digest"))
        self.clock = (self.clock + timedelta(days=1)).replace(hour=7)
        self.assertEqual(self.compose([], "--brief", "--take-held", "--attach-digest", "0"),
                         "Hey Alex, 2 things:\n1. Work on held\n2. Work on task-3\n")
        self.assertEqual(self.rows(desk.DIGEST), [plain])
        self.assertEqual(self.rows(desk.HELD), [])

    def test_every_run_reoffers_rolled_over_items_with_or_without_flags(self):
        rolled = self.item("rolled", overflow=True, at=self.clock.isoformat())
        for i, args in enumerate(((), ("--brief",), ("--take-held",), ("--attach-digest", "0"), ("--take-overflow",))):
            with self.subTest(args=args):
                key = f"rolled-{i}"
                desk.DIGEST.write_text(json.dumps(dict(rolled, key=key, text=key)) + "\n")
                self.assertEqual(self.compose([], *args), key + "\n")
                self.assertEqual(self.rows(desk.DIGEST), [])

    def test_eod_attaches_only_the_requested_unsaid_digest_rows_when_speaking(self):
        self.clock = self.clock.replace(hour=17)
        self.assertNotEqual(self.compose([self.item("known")]), SILENT)
        known = self.queue(desk.DIGEST, self.item("known", "digest"))
        overlaps = self.queue(desk.DIGEST, self.item("overflow", "digest"))
        self.queue(desk.DIGEST, self.item("overflow", overflow=True))
        self.queue(desk.DIGEST, self.item("first", "digest"))
        remaining = self.queue(desk.DIGEST, self.item("second", "digest"))
        output = self.compose([], "--brief", "--attach-digest", "1")
        self.assertEqual(output, "Work on overflow\n\nAlso saved: Work on first (ask if you want details)\n")
        self.assertEqual(self.rows(desk.DIGEST), [known, overlaps, remaining])
        self.assertEqual(self.read_json(desk.SAID)["first"]["kind"], "digest")

    def test_digest_queue_is_preserved_when_nothing_speaks(self):
        for key in ("one", "two"):
            self.queue(desk.DIGEST, self.item(key, "digest"))
        before = desk.DIGEST.read_bytes()
        self.assertEqual(self.compose([], "--brief", "--attach-digest", "1"), SILENT)
        self.assertEqual(desk.DIGEST.read_bytes(), before)
        self.assertFalse(desk.SAID.exists())

    def test_take_drops_only_rows_with_a_valid_stale_timestamp(self):
        old = self.item("old", at=(self.clock - timedelta(hours=desk.SPEAKING_MAX_AGE_HOURS + 1)).isoformat())
        missing = self.item("missing")
        invalid = [self.item(str(i), at=value) for i, value in
                   enumerate(("not-a-date", None, 1234, "2030-04-01T01:00:00"))]
        fresh = self.item("fresh", at=(self.clock - timedelta(hours=desk.SPEAKING_MAX_AGE_HOURS)).isoformat())
        desk.HELD.write_text("".join(json.dumps(row) + "\n" for row in [old, missing, *invalid, fresh]))
        self.assertEqual(desk.take(desk.HELD), [missing, *invalid, fresh])

    def test_malformed_said_at_cannot_block_speaking_pruning_or_listing(self):
        item = self.item("new")
        stale = (self.clock - timedelta(days=31)).isoformat()
        recent = (self.clock - timedelta(days=1)).isoformat()
        desk.SAID.write_text(json.dumps({
            "bad": {"said_at": "not-a-date"}, "new": {"said_at": None, "fp": desk.fp(item)},
            "missing": {}, "naive": {"said_at": "2030-03-31T10:00:00"},
            "stale": {"said_at": stale}, "recent": {"said_at": recent},
        }))
        self.assertEqual(set(json.loads(self.cli(desk, "said"))), {"recent"})
        self.assertEqual(self.compose([item]), "Work on new\n")
        self.assertEqual(set(self.read_json(desk.SAID)), {"recent", "new"})

    def test_empty_stdin_and_file_are_silent(self):
        self.assertEqual(self.cli(desk, "compose", "--job", "empty-stdin", "--print-only"), SILENT)
        empty = self.tmp / "empty.json"
        empty.touch()
        self.assertEqual(self.cli(desk, "compose", "--job", "empty-file", "--print-only",
                                  "--file", str(empty)), SILENT)
        self.assertEqual(self.rows(desk.PING_LOG), [])
        self.assertTrue(all(row["candidates"] == 0 for row in self.rows(desk.RUN_LOG)))

    def test_bad_json_is_silent_and_logged(self):
        self.assertEqual(self.cli(desk, "compose", "--job", "bad", "--print-only", stdin="{broken"), SILENT)
        self.assertIn("bad items json:", self.rows(desk.RUN_LOG)[0]["error"])

    def test_claim_is_idempotent_and_capped_per_day_for_each_kind(self):
        def claim(key, kind):
            return json.loads(self.cli(desk, "ledger", "--key", key, "--claim", kind))

        for kind, cap in desk.CLAIM_CAPS.items():
            with self.subTest(kind=kind):
                key = f"{kind}-0"
                self.assertEqual(claim(key, kind), {
                    "allowed": True, "key": key, "kind": kind,
                    "idempotency_key": f"desk-test:{key}:{kind}",
                })
                self.assertIn("already exists", claim(key, kind)["reason"])
                for i in range(1, cap):
                    self.assertTrue(claim(f"{kind}-{i}", kind)["allowed"])
                denied = claim(f"{kind}-next", kind)
                self.assertFalse(denied["allowed"])
                self.assertEqual(denied["reason"], f"daily {kind} cap {cap} reached")
                ledger = json.loads(self.cli(desk, "ledger", "--list"))
                self.assertEqual(sum(f"claimed_{kind}" in row for row in ledger.values()), cap)
                self.clock += timedelta(days=1)
                self.assertTrue(claim(f"{kind}-next", kind)["allowed"])
                self.assertFalse(claim(key, kind)["allowed"])


class ResolveGateTests(DeskTests):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(resolve, "now", lambda: self.clock))

    def put(self, key, act="send", **fields):
        return resolve.put({"key": key, "act": act, **fields})

    def test_missing_resolution_waits_without_saying_and_keeps_first_at(self):
        item = self.item("k", act="send")
        self.assertEqual(self.compose([item]), "[RESOLVE-FIRST] k\n" + SILENT)
        self.assertFalse(desk.SAID.exists())
        self.assertEqual(self.rows(desk.PING_LOG), [])
        row, = self.rows(desk.RESOLVE_WAIT)
        self.assertEqual((row["key"], row["at"]), ("k", self.clock.isoformat()))
        self.assertEqual(self.rows(desk.RUN_LOG)[-1]["resolve_wait"], ["k"])
        self.clock += timedelta(minutes=19)
        self.assertEqual(self.compose([item]), "[RESOLVE-FIRST] k\n" + SILENT)
        self.assertEqual(self.rows(desk.RESOLVE_WAIT)[0]["at"], row["at"])

    def test_put_releases_waiting_item_without_resubmission(self):
        item = self.item("k", act="send")
        self.assertEqual(self.compose([item]), "[RESOLVE-FIRST] k\n" + SILENT)
        self.assertEqual(self.put("k", draft="Hello, Sam.")["status"], "ready")
        self.assertEqual(self.compose([]), "Work on k. Draft ready, reply 1.\n")
        self.assertEqual(resolve.get("k")["item_fp"], desk.fp(item))
        self.assertEqual(self.rows(desk.RESOLVE_WAIT), [])
        self.assertEqual(self.rows(desk.PING_LOG)[-1]["drafts"], {"k": "ready"})
        self.assertEqual(resolve.key_for_number(1), "k")
        self.assertEqual(self.compose([item]), SILENT)
        self.deliver.assert_not_called()

    def test_none_and_fact_only_resolutions_speak_plain(self):
        self.assertEqual(self.put("none")["status"], "none")
        self.assertEqual(self.put("pay", act="pay", facts=[{"text": "Amount: 10.", "ref": "bill"}])["status"], "ready")
        self.assertEqual(self.compose([self.item("none", act="send"), self.item("pay", act="pay")]),
                         "Hey Alex, 2 things:\n1. Work on none\n2. Work on pay\n")
        self.assertEqual(self.rows(desk.PING_LOG)[-1]["drafts"], {})
        self.assertEqual(self.rows(desk.RUN_LOG)[-1]["unresolved"], [])

    def test_compose_fails_open_after_21_minutes(self):
        item = self.item("k", act="send")
        self.assertEqual(self.compose([item]), "[RESOLVE-FIRST] k\n" + SILENT)
        self.clock += timedelta(minutes=21)
        self.assertEqual(self.compose([]), "Work on k\n")
        self.assertEqual(self.rows(desk.RUN_LOG)[-1]["unresolved"], ["k"])
        self.assertEqual(self.rows(desk.RESOLVE_WAIT), [])
        self.assertEqual(self.compose([item]), SILENT)

    def test_urgent_and_explicit_skip_do_not_wait(self):
        self.clock = self.clock.replace(hour=23)
        self.assertEqual(self.compose([self.item("urgent", act="send", urgent=True)]), "Work on urgent\n")
        self.assertEqual(self.rows(desk.HELD), [])
        self.assertEqual(self.rows(desk.RUN_LOG)[-1]["unresolved"], ["urgent"])
        self.clock += timedelta(hours=9)
        self.assertEqual(self.compose([self.item("skip", act="reply", resolve_skipped=True)]), "Work on skip\n")
        self.assertEqual(self.rows(desk.RESOLVE_WAIT), [])
        self.assertEqual(self.rows(desk.RUN_LOG)[-1]["unresolved"], ["skip"])

    def test_no_act_message_output_is_byte_identical_even_with_a_stored_draft(self):
        self.put("a", draft="Hello, Sam.")
        items = [self.item("a", text="**Reply:** https://example.com now."), self.item("b", "decision")]
        output = self.compose(items)
        self.assertEqual(output, "Hey Alex, 2 things:\n1. Work on b\n2. Reply: now.\n")
        self.assertEqual(self.rows(desk.PING_LOG)[-1]["drafts"], {})
        self.assertEqual(self.rows(desk.RESOLVE_WAIT), [])
        self.assertNotIn("item_fp", resolve.get("a"))

    def test_long_numbered_second_item_keeps_partial_suffix_inside_140(self):
        self.assertEqual(self.put("draft", draft="Hello, Sam.", missing=["Date"])["status"], "partial")
        output = self.compose([self.item("draft", act="send", text="x" * 200), self.item("first", "decision")])
        heading, first, second = output.splitlines()
        self.assertEqual((heading, first), ("Hey Alex, 2 things:", "1. Work on first"))
        self.assertTrue(second.startswith("2. "))
        self.assertTrue(second.endswith("Draft ready with 1 open point, reply 2."))
        self.assertLessEqual(len(second), 140)
        self.assertEqual(self.rows(desk.PING_LOG)[-1]["drafts"], {"draft": "partial"})
        self.assertIsNone(resolve.key_for_number(1))
        self.assertEqual(resolve.key_for_number(2), "draft")

    def test_changed_fingerprint_waits_again_and_resets_the_deadline(self):
        item = self.item("k", act="send")
        self.put("k", draft="Hello, Sam.")
        self.assertEqual(self.compose([item]), "Work on k. Draft ready, reply 1.\n")
        self.assertEqual(self.compose([dict(item, change="deadline moved")]), "[RESOLVE-FIRST] k\n" + SILENT)
        self.clock += timedelta(minutes=21)
        changed = dict(item, change="deadline moved again")
        self.assertEqual(self.compose([changed]), "[RESOLVE-FIRST] k\n" + SILENT)
        self.assertEqual(self.rows(desk.RESOLVE_WAIT)[0]["at"], self.clock.isoformat())
        self.put("k", draft="Updated draft.")
        self.assertEqual(self.compose([changed]), "Work on k. Draft ready, reply 1.\n")

    def test_quiet_hours_hold_resolved_item_for_morning_take_held(self):
        self.clock = self.clock.replace(hour=23)
        self.put("k", draft="Hello, Sam.")
        self.assertEqual(self.compose([self.item("k", act="send")]), SILENT)
        self.assertEqual([r["key"] for r in self.rows(desk.HELD)], ["k"])
        self.assertEqual(self.rows(desk.RESOLVE_WAIT), [])
        self.clock += timedelta(hours=8)
        self.assertEqual(self.compose([], "--brief", "--take-held"), "Work on k. Draft ready, reply 1.\n")
        self.assertEqual(self.rows(desk.HELD), [])

    def test_flush_at_20_minutes_ignores_gap_for_timeout_only(self):
        self.compose([self.item(f"ordinary-{n}") for n in range(4)])
        self.assertEqual(self.compose([self.item("k", act="send")]), "[RESOLVE-FIRST] k\nWork on ordinary-3\n")
        self.queue(desk.DIGEST, self.item("overflow", overflow=True))
        self.clock += timedelta(minutes=19, seconds=59)
        self.assertEqual(self.cli(desk, "flush", "--print-only"), SILENT)
        self.clock += timedelta(seconds=1)
        self.assertEqual(self.cli(desk, "flush", "--print-only"), "Work on k\n")
        self.assertEqual(self.rows(desk.RUN_LOG)[-1]["unresolved"], ["k"])
        self.assertEqual([r["key"] for r in self.rows(desk.DIGEST)], ["overflow"])
        self.assertEqual(self.cli(desk, "flush", "--print-only"), SILENT)

    def test_flush_holds_timeouts_during_quiet_hours_and_drains_over_cap(self):
        self.clock = self.clock.replace(hour=23)
        self.assertEqual(self.compose([self.item(k, act="reply") for k in "abcd"]),
                         "[RESOLVE-FIRST] a,b,c,d\n" + SILENT)
        self.clock += timedelta(minutes=21)
        self.assertEqual(self.cli(desk, "flush", "--print-only"), SILENT)
        self.assertEqual(len(self.rows(desk.RESOLVE_WAIT)), 4)
        self.clock += timedelta(hours=8)
        self.assertIn("1. Work on a", self.cli(desk, "flush", "--print-only"))
        self.assertEqual(self.rows(desk.RUN_LOG)[-1]["unresolved"], list("abc"))
        self.clock += timedelta(minutes=10)
        self.assertEqual(self.cli(desk, "flush", "--print-only"), "Work on d\n")
        self.assertEqual(self.rows(desk.RUN_LOG)[-1]["unresolved"], ["d"])
        self.assertEqual(self.rows(desk.RESOLVE_WAIT), [])

    def test_flush_releases_ready_waiter_without_a_new_producer(self):
        self.assertEqual(self.compose([self.item("k", act="reply")]), "[RESOLVE-FIRST] k\n" + SILENT)
        self.put("k", act="reply", draft="Hello, Sam.")
        self.assertEqual(self.cli(desk, "flush", "--print-only"), "Work on k. Draft ready, reply 1.\n")
        self.assertEqual(self.rows(desk.RESOLVE_WAIT), [])
        self.assertEqual(self.rows(desk.RUN_LOG)[-1]["unresolved"], [])

    def test_corrupt_resolution_store_still_fails_open(self):
        (self.tmp / "resolutions.json").write_text("broken JSON")
        self.assertEqual(self.compose([self.item("k", act="send")]), "[RESOLVE-FIRST] k\n" + SILENT)
        self.clock += timedelta(minutes=20)
        self.assertEqual(self.cli(desk, "flush", "--print-only"), "Work on k\n")
        self.assertEqual(self.rows(desk.RUN_LOG)[-1]["unresolved"], ["k"])

    def test_timed_out_item_held_by_compose_does_not_restart_wait_next_morning(self):
        self.clock = self.clock.replace(hour=23)
        self.assertEqual(self.compose([self.item("k", act="reply")]), "[RESOLVE-FIRST] k\n" + SILENT)
        self.clock += timedelta(minutes=20)
        self.assertEqual(self.compose([]), SILENT)
        self.assertEqual([r["key"] for r in self.rows(desk.HELD)], ["k"])
        self.assertEqual(self.rows(desk.RUN_LOG)[-1]["unresolved"], [])
        self.clock = (self.clock + timedelta(days=1)).replace(hour=7, minute=0)
        self.assertEqual(self.compose([], "--take-held"), "Work on k\n")
        self.assertEqual(self.rows(desk.RUN_LOG)[-1]["unresolved"], ["k"])

    def test_compose_posts_only_the_visible_text_for_spoken_drafts(self):
        for key in ("second", "third", "rolled"):
            self.put(key, draft="Hello, Sam.")
        self.put("first", act="decide")
        items = [self.item("second", act="send"), self.item("third", act="send"),
                 self.item("rolled", act="send"), self.item("first", "decision", act="decide")]
        text = ("Hey Alex, 3 things:\n1. Work on first\n2. Work on second. Draft ready, reply 2.\n"
                "3. Work on third. Draft ready, reply 3.\n+1 more lined up, coming next (or ask me)")
        self.assertEqual(self.cli(desk, "compose", "--job", "test", stdin=json.dumps(items)), SILENT)
        # Nothing but the message: no private notes ride along (the delivery input is visible in Bot Chat).
        self.deliver.assert_called_once_with(text, key="test:first,second,third", job="test", urgent=False)
        self.assertEqual(self.rows(desk.PING_LOG)[-1]["drafts"], {"second": "ready", "third": "ready"})
        self.assertEqual(self.compose([]), "Work on rolled. Draft ready, reply 1.\n")
        self.deliver.reset_mock()
        self.put("plain")
        self.assertEqual(self.cli(desk, "compose", "--job", "test", stdin=json.dumps([self.item("plain", act="send")])), SILENT)
        self.deliver.assert_called_once_with("Work on plain", key="test:plain", job="test", urgent=False)

    def test_a_failed_enqueue_is_retried_not_marked_said(self):
        self.enterContext(patch.object(desk_say, "quiet_now", return_value=False))
        items = [self.item("bill", "action")]
        self.deliver.side_effect = OSError("disk full")
        self.assertEqual(self.cli(desk, "compose", "--job", "test", stdin=json.dumps(items), code=1), SILENT)
        self.assertFalse(desk.SAID.exists() and "bill" in self.read_json(desk.SAID))
        self.assertEqual(self.rows(desk.PING_LOG), [])
        self.assertEqual([r["key"] for r in self.rows(desk.DIGEST)], ["bill"])   # rolled over for the next run
        self.assertIn("enqueue failed", self.rows(desk.RUN_LOG)[-1]["error"])
        self.kick.assert_not_called()
        self.deliver.side_effect = None
        self.assertEqual(self.cli(desk, "compose", "--job", "test", stdin=json.dumps(items)), SILENT)
        self.assertEqual(self.deliver.call_count, 2)
        self.assertIn("bill", self.read_json(desk.SAID))
        self.assertEqual(self.rows(desk.PING_LOG)[-1]["say_id"], "m1")
        self.kick.assert_called_once()

    def test_cli_uses_the_profile_state_without_patch(self):
        item = self.item("cli", act="send", urgent=True)
        result = subprocess.run([sys.executable, desk.__file__, "compose", "--job", "test", "--print-only"],
                                input=json.dumps([item]), text=True, capture_output=True, check=True,
                                env=dict(os.environ))   # the isolated HERMES_HOME: its own plugin-data/state
        self.assertEqual(result.stdout, "Work on cli\n")
        self.assertEqual(self.rows(self.tmp / "run_log.jsonl")[-1]["unresolved"], ["cli"])
        self.assertEqual(self.read_json(self.tmp / "said.json")["cli"]["fp"], desk.fp(item))


class ConfigurationTests(OfflineTestCase):
    def test_desk_uses_configured_item_caps(self):
        self.configure(quiet_hours={"start": 0, "end": 0},
                       caps={"max_items": 1, "max_brief_items": 2, "line_chars": 12})
        text = "a" * 20
        self.assertEqual(desk.render_message(None, [text], []), "a" * 11 + "…")
        items = json.dumps([{"key": key, "kind": "action", "text": key} for key in ("one", "two", "three")])
        for flags, expected in (([], "one\n+2 more"), (["--brief"], "1. one\n2. two\n+1 more")):
            home = self.root / ("brief" if flags else "normal")   # a fresh profile each, with the same config
            (home / "plugin-data" / "proactive-hermes").mkdir(parents=True)
            shutil.copy(phconf.config_path(), home / "plugin-data" / "proactive-hermes" / "config.json")
            env = {**os.environ, "HERMES_HOME": str(home)}
            output = subprocess.check_output([sys.executable, desk.__file__, "compose", "--job", "test",
                                              "--print-only", *flags], input=items, text=True, env=env)
            self.assertIn(expected, output)

    def test_desk_uses_configured_message_length_and_quiet_hours(self):
        self.configure(timezone="Asia/Tokyo", quiet_hours={"start": 13, "end": 15},
                       caps={"line_chars": 100, "message_chars": 200})
        self.assertTrue(desk.in_quiet(datetime(2030, 4, 1, 4, tzinfo=timezone.utc)))
        self.assertFalse(desk.in_quiet(datetime(2030, 4, 1, 6, tzinfo=timezone.utc)))
        message, kept = desk.render_parts(None, ["a" * 100, "b" * 100], [])
        self.assertLessEqual(len(message), 200)
        self.assertEqual(kept, 1)
        self.assertIn("+1 more lined up", message)

    def test_recap_uses_call_timezone_window_and_workdays_with_optional_holidays(self):
        self.configure(timezone="Asia/Tokyo", workdays=[0], holidays=["GB"],
                       daily_call={"timezone": "America/New_York", "poll_start": "12:15", "poll_end": "13:45"})
        with patch.dict(sys.modules, {"holidays": None}):
            monday = datetime(2030, 4, 1, 16, 15, tzinfo=timezone.utc)
            self.assertFalse(recap.gate("am", monday - timedelta(minutes=1))["wakeAgent"])
            am = recap.gate("am", monday)
            self.assertEqual(am["context"], {"date": "2030-04-01", "daily_time": "12:15", "attempt": 1,
                                              "final_attempt": False})
            end = monday + timedelta(minutes=90, seconds=59)
            self.assertTrue(recap.gate("am", end)["context"]["final_attempt"])
            self.assertFalse(recap.gate("am", end + timedelta(seconds=1))["wakeAgent"])
            self.assertFalse(recap.gate("pm", monday)["wakeAgent"])  # already Tuesday in the owner's timezone
            self.assertTrue(recap.gate("pm", monday - timedelta(hours=7))["wakeAgent"])

        calendar = types.SimpleNamespace(country_holidays=lambda cc, years: {monday.date(): "Public holiday"}
                                         if cc == "GB" else {})
        with patch.dict(sys.modules, {"holidays": calendar}):
            self.assertEqual(recap.gate("pm", monday - timedelta(hours=7)),
                             {"wakeAgent": False, "reason": "GB holiday: Public holiday"})


class FakeStore:
    def __init__(self):
        self.requests = []
        self.results = {}
        self.lookups = []

    def request(self, op, args, *, idempotency_key, ttl):
        self.requests.append((op, args, idempotency_key, ttl))
        request_id = f"request-{len(self.requests)}"
        self.results[request_id] = {"status": "pending"}
        return {"id": request_id, "device_id": "fake-phone", "status": "pending"}

    def result(self, request_id):
        self.lookups.append(request_id)
        return self.results[request_id]


class ReminderTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.configure(reminders={"enabled": True, "per_day": 2}, authority={"auto": ["reminders"]})
        self.enterContext(patch.object(reminders, "PLUGIN", self.tmp))
        (self.tmp / "bridge.py").touch()  # Presence check only; the bridge itself is mocked below.
        self.freeze_datetime(reminders)
        self.store = FakeStore()
        self.enterContext(patch.object(reminders, "bridge", return_value=(None, self.store)))

    def add(self, key="task"):
        return json.loads(self.cli(reminders, "add", "--key", key, "--title", "Do the task"))

    def tick(self):
        self.assertEqual(self.cli(reminders, "tick"), "")

    def test_tick_lands_succeeded_request_and_records_reminder_id(self):
        request_id = self.add()["request_id"]
        self.store.results[request_id] = {"status": "succeeded", "result": {"data": {"id": "reminder-1"}}}
        self.tick()
        row = self.read_json(self.tmp / reminders.OUTBOX)["task"]
        self.assertEqual((row["status"], row["reminder_id"]), ("landed", "reminder-1"))
        ledger = self.read_json(self.tmp / reminders.LEDGER)["task"]
        self.assertEqual((ledger["reminder_status"], ledger["reminder_id"]), ("landed", "reminder-1"))
        self.assertEqual(self.store.lookups, [request_id])
        self.assertEqual(len(self.store.requests), 1)

    def test_expired_request_gets_new_key_and_incremented_attempt(self):
        request_id = self.add()["request_id"]
        self.store.results[request_id] = {"status": "expired"}
        self.tick()
        self.assertEqual(self.store.requests, [
            ("reminders.create", {"title": "Do the task", "priority": "medium"},
             f"proactive-hermes:task:rem:{i}", reminders.TTL)
            for i in (1, 2)
        ])
        row = self.read_json(self.tmp / reminders.OUTBOX)["task"]
        self.assertEqual((row["attempts"], row["status"], row["request_id"]), (2, "queued", "request-2"))
        self.assertFalse(hasattr(reminders, "wake"))

    def test_pending_and_running_requests_are_never_resubmitted(self):
        request_id = self.add()["request_id"]
        before = (self.tmp / reminders.OUTBOX).read_bytes()
        for status in ("pending", "running", "pending"):
            self.store.results[request_id] = {"status": status}
            self.tick()
            self.assertEqual((self.tmp / reminders.OUTBOX).read_bytes(), before)
        self.assertEqual(self.store.lookups, [request_id] * 3)
        self.assertEqual(len(self.store.requests), 1)
        self.assertFalse(hasattr(reminders, "wake"))  # the desk never wakes the phone itself

    def test_expiry_at_max_attempts_fails_without_resubmitting(self):
        request_id = self.add()["request_id"]
        box = self.read_json(self.tmp / reminders.OUTBOX)
        box["task"]["attempts"] = reminders.MAX_ATTEMPTS
        (self.tmp / reminders.OUTBOX).write_text(json.dumps(box))
        self.store.results[request_id] = {"status": "expired"}
        self.tick()
        self.assertEqual(self.read_json(self.tmp / reminders.OUTBOX)["task"]["status"], "failed")
        self.assertEqual(self.read_json(self.tmp / reminders.LEDGER)["task"]["reminder_status"], "failed")
        self.assertEqual(len(self.store.requests), 1)
        self.assertFalse(hasattr(reminders, "wake"))  # the desk never wakes the phone itself

    def test_add_daily_cap_refuses_before_submitting(self):
        for i in range(2):
            self.assertEqual(self.add(f"task-{i}")["status"], "queued")
        self.assertEqual(self.add("next"), {
            "status": "refused", "reason": "daily cap 2 reminders reached",
        })
        self.assertNotIn("next", self.read_json(self.tmp / reminders.OUTBOX))
        self.assertEqual(len(self.store.requests), 2)
        self.clock += timedelta(days=1)
        self.assertEqual(self.add("next")["status"], "queued")


@unittest.skipUnless(monitor is not None, "optional monitor bridge not packaged")
class MonitorTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.patch_paths(monitor, "DIGEST", "RUN_LOG")
        self.enterContext(patch.object(monitor, "HERE", self.tmp))
        self.freeze_datetime(monitor)

    def run_fake(self, script):
        (self.tmp / "fake_monitor.py").write_text(script)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(monitor.run("fake_monitor.py", "fake", "Fake monitor"), 0)
        self.assertEqual(output.getvalue(), "")

    def test_stdout_creates_one_digest_row_but_run_prints_nothing(self):
        self.run_fake("print('First line')\nprint('Second line')\n")
        row, = self.rows(monitor.DIGEST)
        self.assertEqual(row["kind"], "digest")
        self.assertEqual(row["text"], "Fake monitor: First line Second line")
        self.assertEqual(row["ref"], "cron monitor fake_monitor.py")
        self.assertEqual(self.rows(monitor.RUN_LOG)[0]["digest"], 1)

    def test_empty_stdout_does_not_create_a_digest_row(self):
        self.run_fake("pass\n")
        self.assertEqual(self.rows(monitor.DIGEST), [])
        self.assertEqual(self.rows(monitor.RUN_LOG)[0]["candidates"], 0)

    def test_nonzero_exit_logs_error_and_does_not_queue_stdout(self):
        self.run_fake("import sys\nprint('not news')\nprint('broken monitor', file=sys.stderr)\nsys.exit(7)\n")
        self.assertEqual(self.rows(monitor.DIGEST), [])
        row, = self.rows(monitor.RUN_LOG)
        self.assertEqual(row["job"], "monitor:fake")
        self.assertEqual(row["error"], "exit 7: broken monitor")


class CalendarHelperTests(OfflineTestCase):
    def test_parse_dt_localizes_naive_input_and_preserves_explicit_offset(self):
        # Import mutates sys.path; the offline base also isolates HERMES_HOME.
        with patch.object(sys, "path", sys.path.copy()):
            import ea_calendar_block as calendar
        self.assertEqual(calendar.parse_dt("2030-04-01T09:00:00").tzinfo, phconf.tz())
        self.assertEqual(calendar.parse_dt("2030-04-01T09:00:00+00:00").tzinfo, timezone.utc)
