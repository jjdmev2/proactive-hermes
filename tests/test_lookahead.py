"""Offline tests for the look-ahead gate and ledger (ea_lookahead.py, lookahead_gate.py, the job). No network."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo

from .test_desk import OfflineTestCase, isolated_env, phconf

import cli
import ea_lookahead as la
import jobs
from tests.test_setup import FakeCron

REAL_RUN = subprocess.run  # Base patches subprocess.run for the in-process tests; scripts get the real one
BER = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 25, 10, 0, tzinfo=BER)  # Wednesday 10:00 in the owner's timezone
RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
SCRIPT = RUNTIME / "ea_lookahead.py"
GATE = RUNTIME / "lookahead_gate.py"
ACCOUNT = "alex@acme.example"


def ts(dt: datetime) -> str:
    return dt.isoformat()


def ev(eid, summary, start, end=None, location="", description="", attendees=None, **extra):
    row = {"id": eid, "summary": summary, "start": {"dateTime": ts(start)},
           "end": {"dateTime": ts(end or start + timedelta(hours=2))}, "location": location,
           "description": description, **extra}
    if attendees is not None:
        row["attendees"] = attendees
    return row


def mail(mid, subject, sent, sender="Rail <noreply@vr.fi>"):
    return {"id": mid, "threadId": mid, "from": sender, "subject": subject,
            "date": sent.strftime("%a, %d %b %Y %H:%M:%S %z")}


class Base(OfflineTestCase):
    SETTINGS: dict = {}

    def setUp(self):
        super().setUp()
        self.settings = {"timezone": "Europe/Berlin", "owner": {"name": "Alex", "work_emails": [ACCOUNT]},
                         "calendar": {"enabled": True, "id": ACCOUNT}, **self.SETTINGS}
        self.configure(**self.settings)
        # the live source must never run in tests
        self.live = self.enterContext(patch.object(la.subprocess, "run", side_effect=AssertionError("network call in test")))
        self.cal, self.mails = [], []

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = la.main([str(a) for a in argv])
        self.assertEqual(rc, 0)
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        return json.loads(lines[-1])

    def scan(self, now=NOW, cal=None, mails=None):
        cf, mf = self.tmp / "cal.json", self.tmp / "mail.json"
        cf.write_text(json.dumps(self.cal if cal is None else cal))
        mf.write_text(json.dumps(self.mails if mails is None else mails))
        return self.run_cli("scan", "--calendar-json", cf, "--mail-json", mf, "--now", ts(now))

    def state(self):
        return json.loads((self.tmp / "lookahead.json").read_text())

    def due_keys(self, out):
        return [d["key"] for d in out.get("context", {}).get("due", [])]


class TripSubjectTests(Base):
    SENT = datetime(2026, 11, 17, 12, 3, tzinfo=BER)

    def test_past_midnight_arrival_is_next_day(self):
        trip = la.parse_trip_subject("Your tickets 18.11. Berlin – Hamburg (23:10 - 1:10)", self.SENT)
        self.assertEqual((trip["from"], trip["to"]), ("Berlin", "Hamburg"))
        self.assertEqual(trip["depart"], datetime(2026, 11, 18, 23, 10, tzinfo=BER))
        self.assertEqual(trip["arrive"], datetime(2026, 11, 19, 1, 10, tzinfo=BER))

    def test_same_day_trip(self):
        trip = la.parse_trip_subject("Your tickets 17.11. Hamburg – Berlin (14:50 - 15:44)", self.SENT)
        self.assertEqual((trip["from"], trip["to"]), ("Hamburg", "Berlin"))
        self.assertEqual(trip["depart"], datetime(2026, 11, 17, 14, 50, tzinfo=BER))
        self.assertEqual(trip["arrive"], datetime(2026, 11, 17, 15, 44, tzinfo=BER))

    def test_hyphen_and_local_language_variant(self):
        trip = la.parse_trip_subject("Lippusi 18.11. Berlin - Hamburg (23:10 – 1:10)", self.SENT)
        self.assertEqual(trip["to"], "Hamburg")
        self.assertEqual(trip["arrive"].date().isoformat(), "2026-11-19")

    def test_year_rolls_when_date_is_far_before_mail(self):
        sent = datetime(2026, 12, 20, 9, 0, tzinfo=BER)
        trip = la.parse_trip_subject("Your tickets 05.01. Berlin – Leipzig (08:00 - 09:47)", sent)
        self.assertEqual(trip["depart"], datetime(2027, 1, 5, 8, 0, tzinfo=BER))
        recent = la.parse_trip_subject("Your tickets 30.11. Berlin – Leipzig (08:00 - 09:47)", sent)
        self.assertEqual(recent["depart"].year, 2026)  # 20 days back: same year, not rolled

    def test_change_notice_is_not_a_ticket(self):
        self.assertIsNone(la.parse_trip_subject("Your train trip Hamburg – Berlin has been changed", self.SENT))

    def test_times_use_the_configured_timezone(self):
        self.configure(**{**self.settings, "timezone": "Asia/Tokyo"})
        trip = la.parse_trip_subject("Your tickets 18.11. Berlin – Hamburg (23:10 - 1:10)", self.SENT)
        self.assertEqual(trip["depart"].utcoffset(), timedelta(hours=9))


class ClassifyTests(Base):
    def test_each_type(self):
        cases = {
            "flight": la.classify("Flight LH123 to London", "Berlin Airport"),
            "stay": la.classify("Hotel Adlon check-in", "Unter den Linden 77"),
            "train": la.classify("Train to Hamburg", ""),
            "bus": la.classify("Flixbus to Prague", ""),
            "ticket_sale": la.classify("Summer Festival tickets on sale", ""),
            "bill": la.classify("Phone bill due", ""),
            "event": la.classify("Board game night", "Market Street 5, Berlin"),
            "meeting": la.classify("1:1 with Sam", "https://meet.google.com/abc-defg-hij"),
        }
        for want, got in cases.items():
            self.assertEqual(got, want, want)

    def test_multi_day_all_day_is_stay(self):
        self.assertEqual(la.classify("Berlin", "", multi_day=True), "stay")

    def test_meeting_variants(self):
        self.assertEqual(la.classify("Product Sprint", ""), "meeting")
        self.assertEqual(la.classify("Board call", "Microsoft Teams Meeting"), "meeting")
        self.assertEqual(la.classify("Sync", "https://zoom.us/j/123"), "meeting")

    def test_false_friends(self):
        self.assertEqual(la.classify("Sales training", ""), "meeting")        # not "train"
        self.assertEqual(la.classify("Moved due to conflict", ""), "meeting")  # not "bill"
        self.assertEqual(la.classify("Presentation", ""), "meeting")          # not "station"
        self.assertEqual(la.classify("Client onboarding call", ""), "meeting")  # not "boarding"
        self.assertEqual(la.classify("Alex 121 Sam", "SLACK BABY!"), "meeting")  # chat app, not a place

    def test_airport_codes_are_configured_and_case_sensitive(self):
        self.assertEqual(la.classify("Fly BER to ARN", ""), "meeting")
        self.configure(**{**self.settings, "lookahead": {"airport_codes": ["BER"]}})
        self.assertEqual(la.classify("Fly BER to ARN", ""), "flight")
        self.assertEqual(la.classify("Coffee with Amber", ""), "meeting")

    def test_extra_keywords_extend_the_defaults(self):
        self.assertEqual(la.classify("ICE 123 to Munich", ""), "meeting")
        self.configure(**{**self.settings, "lookahead": {"keywords": {"train": [r"\bICE\b"]}}})
        self.assertEqual(la.classify("ICE 123 to Munich", ""), "train")

    def test_bill_words_in_description_do_not_make_a_bill(self):
        desc = "- [ ] Review live buyer replies, due promises and missing next actions."
        self.assertEqual(la.classify(f"Focus block  {desc}", "", title="Focus block "), "meeting")
        self.assertEqual(la.classify(f"Phone bill due  {desc}", "", title="Phone bill due "), "bill")

    def test_sender_types(self):
        self.assertEqual(la.sender_type("Finnair <noreply@finnair.com>"), "flight")
        self.assertEqual(la.sender_type("noreply@lippu.vr.fi"), "train")
        self.assertEqual(la.sender_type("Matkahuolto <info@matkahuolto.fi>"), "bus")
        self.assertEqual(la.sender_type("Airbnb <automated@airbnb.com>"), "stay")
        self.assertEqual(la.sender_type("Luma <calendar@lu.ma>"), "event")

    def test_configured_senders_extend_the_defaults(self):
        self.assertEqual(la.sender_type("Acme <info@acmetickets.example>", "Ticket"), "event")
        self.configure(**{**self.settings, "lookahead": {"senders": {"train": ["acmetickets.example"]}}})
        self.assertEqual(la.sender_type("Acme <info@acmetickets.example>", "Ticket"), "train")
        self.assertEqual(la.sender_type("Airbnb <automated@airbnb.com>"), "stay")


class ConfigTests(Base):
    def test_defaults_ship_in_phconf(self):
        d = phconf.DEFAULTS
        self.assertEqual(d["schedule"]["lookahead"], "*/30 7-22 * * *")
        self.assertIs(d["features"]["lookahead"], False)  # opt-in through setup
        self.assertEqual(d["lookahead"]["home_places"], [])
        self.assertEqual({"calendar_account", "mail_account"} - set(d["lookahead"]), set())

    def test_accounts_default_from_setup_and_can_be_overridden(self):
        self.assertEqual((la.calendar_account(), la.mail_account()), (ACCOUNT, ACCOUNT))
        self.configure(**{**self.settings, "lookahead": {"calendar_account": "team@acme.example", "mail_account": "me@home.example"}})
        self.assertEqual((la.calendar_account(), la.mail_account()), ("team@acme.example", "me@home.example"))
        self.configure(timezone="Europe/Berlin")
        self.assertEqual((la.calendar_account(), la.mail_account()), ("primary", "gmail"))

    def test_live_calendar_call_reads_the_configured_calendar(self):
        self.configure(**{**self.settings, "lookahead": {"calendar_account": "team@acme.example"}})
        with patch.object(la, "_google", return_value=[]) as google:
            la.fetch_calendar(NOW, None)
            la.fetch_mail(None)
        cal_args, mail_args = (c.args[0] for c in google.call_args_list)
        self.assertEqual(cal_args[:2], ["calendar", "list"])
        self.assertEqual(cal_args[cal_args.index("--calendar") + 1], "team@acme.example")
        self.assertEqual(cal_args[cal_args.index("--end") + 1], (NOW + timedelta(days=30)).isoformat())
        self.assertEqual(mail_args[:2], ["gmail", "search"])

    def test_mail_query_covers_senders_and_can_be_replaced(self):
        query = la.mail_query()
        for part in ("newer_than:60d", "from:ticketmaster", "from:airbnb", 'subject:"booking confirmation"', "subject:itinerary"):
            self.assertIn(part, query)
        self.configure(**{**self.settings, "lookahead": {"senders": {"train": ["acmerail.example"]}}})
        self.assertIn("from:acmerail.example", la.mail_query())
        self.configure(**{**self.settings, "lookahead": {"mail_query": "label:travel"}})
        self.assertEqual(la.mail_query(), "label:travel")

    def test_quiet_hours_come_from_the_config(self):
        self.assertTrue(la.is_quiet(NOW.replace(hour=23)))
        self.assertFalse(la.is_quiet(NOW.replace(hour=22, minute=59)))
        self.assertTrue(la.is_quiet(NOW.replace(hour=6, minute=59)))
        self.assertFalse(la.is_quiet(NOW.replace(hour=7)))
        self.configure(**{**self.settings, "quiet_hours": {"start": 21, "end": 9}})
        self.assertFalse(la.is_quiet(NOW.replace(hour=20, minute=59)))
        self.assertTrue(la.is_quiet(NOW.replace(hour=21)))
        self.assertTrue(la.is_quiet(NOW.replace(hour=8, minute=59)))
        self.assertFalse(la.is_quiet(NOW.replace(hour=9)))
        self.configure(**{**self.settings, "quiet_hours": {"start": 1, "end": 5}})  # a daytime-style window
        self.assertTrue(la.is_quiet(NOW.replace(hour=3)))
        self.assertFalse(la.is_quiet(NOW.replace(hour=23)))

    def test_checkpoint_wall_clock_uses_the_configured_timezone(self):
        tokyo = ZoneInfo("Asia/Tokyo")
        self.configure(**{**self.settings, "timezone": "Asia/Tokyo"})
        cps = la.schedule("train", datetime(2026, 12, 1, 9, 30, tzinfo=tokyo))
        self.assertEqual(cps["t1d"], datetime(2026, 11, 30, 19, 0, tzinfo=tokyo))
        self.assertEqual(cps["morning"], datetime(2026, 12, 1, 7, 30, tzinfo=tokyo))

    def test_watched_remotely_needs_a_watch_word_and_a_non_home_place(self):
        self.assertFalse(la.watched_remotely("Team dinner", "Bar, Anytown"))
        # no home_places: nothing can match, so a watch-word title is always watched, never attended
        self.assertTrue(la.watched_remotely("Season finale livestream", "Bar, Berlin"))
        self.configure(**{**self.settings, "lookahead": {"home_places": ["Berlin", "Germany"]}})
        self.assertFalse(la.watched_remotely("Season finale watch party", "Bar, Berlin"))
        self.assertTrue(la.watched_remotely("Season finale watch party", "Arena, Tokyo"))
        self.assertFalse(la.watched_remotely("Grand Prix", "Circuit, Tokyo"))  # not a default watch word
        self.configure(**{**self.settings, "lookahead": {"home_places": ["Berlin"], "watch_words": ["grand prix"]}})
        self.assertTrue(la.watched_remotely("Grand Prix: Qualifying", "Circuit, Tokyo"))
        self.assertFalse(la.watched_remotely("Grand Prix: Qualifying", "Circuit, Berlin"))


class ScanFilterTests(Base):
    def test_meeting_declined_and_closed_are_skipped(self):
        start = NOW + timedelta(days=5)
        self.cal = [
            ev("meet1", "1:1 with Sam", start, location="https://meet.google.com/x"),
            ev("dec1", "Dinner at the Grill", start, location="Market Street 14",
               attendees=[{"email": ACCOUNT, "self": True, "responseStatus": "declined"}]),
            ev("closed1", "Board game league", start, location="Game Shop 3"),
            ev("keep1", "Card night", start, location="Game Shop 3"),
            ev("cp3", "Daily focus block", start, description="Review due promises."),
        ]
        (self.tmp / "actions.json").write_text(json.dumps({"la:cal:closed1": {"status": "dropped"}}))
        out = self.scan()
        self.assertEqual(self.due_keys(out), ["la:cal:keep1"])
        self.assertEqual(set(self.state()["items"]), {"la:cal:keep1"})

    def test_flattened_google_api_shape_is_read(self):
        start = NOW + timedelta(days=3)
        self.cal = [{"id": "s1", "summary": "Card night", "start": ts(start), "end": ts(start + timedelta(hours=3)),
                     "location": "Game Shop 3", "description": "", "status": "confirmed"}]
        out = self.scan(cal={"items": self.cal})
        self.assertEqual(out["context"]["due"][0]["checkpoint"], "t7d")

    def test_watch_party_is_skipped_unless_at_home(self):
        start = NOW + timedelta(days=5)
        self.configure(**{**self.settings, "lookahead": {"home_places": ["Berlin"]}})
        self.cal = [ev("a", "Season finale livestream", start, location="Arena, Tokyo"),
                    ev("b", "Season finale watch party", start, location="Sports Bar, Berlin")]
        self.assertEqual(self.due_keys(self.scan()), ["la:cal:b"])


class CheckpointTests(Base):
    def flight(self, days=5, hour=12):
        start = (NOW + timedelta(days=days)).replace(hour=hour, minute=0)
        self.cal = [ev("fl1", "Flight LH1331 to Stockholm", start, location="Berlin Airport")]
        return start

    def test_latest_due_only_and_earlier_skipped(self):
        self.flight(days=5)
        out = self.scan()
        due = out["context"]["due"]
        self.assertEqual(len(due), 1)
        self.assertEqual((due[0]["checkpoint"], due[0]["tier"], due[0]["type"]), ("t7d", "A", "flight"))
        cps = self.state()["items"]["la:cal:fl1"]["checkpoints"]
        self.assertEqual(cps["t30d"]["state"], "skipped")
        self.assertEqual(cps["t14d"]["state"], "skipped")
        self.assertEqual(cps["checkin"]["state"], "pending")

    def test_quiet_hours_never_wake(self):
        self.flight(days=5)
        late = NOW.replace(hour=23, minute=30)
        self.assertEqual(self.scan(now=late), {"wakeAgent": False})
        self.assertEqual(self.scan(now=NOW.replace(hour=6, minute=59)), {"wakeAgent": False})
        self.assertTrue(self.scan(now=NOW.replace(hour=7, minute=0))["wakeAgent"])

    def test_configured_quiet_hours_gate_the_scan(self):
        self.flight(days=5)
        self.configure(**{**self.settings, "quiet_hours": {"start": 9, "end": 12}})
        self.assertEqual(self.scan(), {"wakeAgent": False})  # 10:00 is quiet now
        self.assertTrue(self.scan(now=NOW.replace(hour=12))["wakeAgent"])

    def test_fired_is_not_repeated(self):
        self.flight(days=5)
        self.assertEqual(self.due_keys(self.scan()), ["la:cal:fl1"])
        self.assertEqual(self.scan(now=NOW + timedelta(minutes=10)), {"wakeAgent": False})  # offered, not acked yet
        res = self.run_cli("fired", "--key", "la:cal:fl1", "--checkpoint", "t7d", "--outcome", "quiet")
        self.assertEqual(res["status"], "fired")
        self.assertEqual(self.scan(now=NOW + timedelta(minutes=20)), {"wakeAgent": False})
        # next checkpoint (check-in, 48h before) still fires once
        start = NOW + timedelta(days=5)
        at_checkin = start.replace(hour=12) - timedelta(hours=48) + timedelta(minutes=5)
        self.assertEqual(self.scan(now=at_checkin)["context"]["due"][0]["checkpoint"], "checkin")

    def test_start_moved_is_changed(self):
        start = self.flight(days=5)
        self.scan()
        self.run_cli("fired", "--key", "la:cal:fl1", "--checkpoint", "t7d", "--outcome", "said")
        moved = start + timedelta(hours=3)
        self.cal = [ev("fl1", "Flight LH1331 to Stockholm", moved, location="Berlin Airport")]
        out = self.scan(now=NOW + timedelta(hours=1))
        d = out["context"]["due"][0]
        self.assertTrue(d["changed"])
        self.assertEqual(d["old_start"], la.iso(start))
        self.assertEqual(d["start"], la.iso(moved))
        self.assertIsNone(d["checkpoint"])  # no checkpoint due, still a reason to speak
        cps = self.state()["items"]["la:cal:fl1"]["checkpoints"]
        self.assertEqual(cps["checkin"]["at"], la.iso(moved - timedelta(hours=48)))
        self.assertEqual(cps["t7d"]["state"], "fired")
        self.run_cli("fired", "--key", "la:cal:fl1", "--checkpoint", "changed", "--outcome", "said")
        self.assertEqual(self.scan(now=NOW + timedelta(hours=2)), {"wakeAgent": False})

    def test_train_morning_rule_and_flight_early_morning(self):
        early = datetime(2026, 12, 1, 8, 0, tzinfo=BER)
        self.assertNotIn("morning", la.schedule("train", early))
        self.assertEqual(la.schedule("train", early + timedelta(hours=1))["morning"],
                         datetime(2026, 12, 1, 7, 30, tzinfo=BER))
        f = la.schedule("flight", datetime(2026, 12, 1, 11, 0, tzinfo=BER))
        self.assertEqual(f["morning"], datetime(2026, 12, 1, 7, 30, tzinfo=BER))
        self.assertEqual(f["t1d"], datetime(2026, 11, 30, 19, 0, tzinfo=BER))
        self.assertEqual(f["leave"], datetime(2026, 12, 1, 7, 0, tzinfo=BER))
        self.assertEqual(la.schedule("flight", datetime(2026, 12, 1, 10, 0, tzinfo=BER))["morning"],
                         datetime(2026, 12, 1, 7, 0, tzinfo=BER))  # before 10:30: three hours before
        self.assertEqual(set(la.schedule("ticket_sale", early)), {"t7d", "t1d", "open30"})
        self.assertEqual(set(la.schedule("bill", early)), {"t14d", "t3d", "t1d"})

    def test_max_six_sorted_by_start(self):
        self.cal = [ev(f"e{i}", f"Gig {i}", NOW + timedelta(days=6 - i * 0.5), location="Club Mitte, Berlin")
                    for i in range(8)]
        due = self.scan()["context"]["due"]
        self.assertEqual(len(due), 6)
        self.assertEqual([d["start"] for d in due], sorted(d["start"] for d in due))


class AnswerTests(Base):
    def setUp(self):
        super().setUp()
        self.start = (NOW + timedelta(days=3)).replace(hour=11)
        self.cal = [ev("tcg", "Card night", self.start, location="Game Shop 3, Berlin")]

    def test_not_mine_closes_for_good(self):
        self.assertTrue(self.scan()["wakeAgent"])
        res = self.run_cli("answer", "--key", "la:cal:tcg", "--mode", "not-mine")
        self.assertEqual(res["item_status"], "dropped")
        for later in (timedelta(hours=1), timedelta(days=1, hours=9.5)):  # t2d time included
            self.assertEqual(self.scan(now=NOW + later), {"wakeAgent": False})
        self.assertEqual(self.run_cli("list", "--days", "30", "--now", ts(NOW))["count"], 0)

    def test_later_suppresses_until_next_checkpoint(self):
        self.assertEqual(self.scan()["context"]["due"][0]["checkpoint"], "t7d")
        res = self.run_cli("answer", "--key", "la:cal:tcg", "--mode", "later", "--now", ts(NOW))
        t2d = self.start - timedelta(days=2)
        self.assertEqual(res["later_until"], la.iso(t2d))
        self.assertEqual(self.scan(now=NOW + timedelta(hours=2)), {"wakeAgent": False})
        self.assertEqual(self.scan(now=t2d - timedelta(minutes=1)), {"wakeAgent": False})
        self.assertEqual(self.scan(now=t2d + timedelta(minutes=1))["context"]["due"][0]["checkpoint"], "t2d")

    def test_mode_and_booked_are_per_item(self):
        self.scan()
        self.run_cli("answer", "--key", "la:cal:tcg", "--mode", "scooter", "--note", "if dry")
        self.run_cli("answer", "--key", "la:cal:tcg", "--mode", "booked")
        out = self.scan(now=self.start - timedelta(days=2) + timedelta(minutes=1))
        d = out["context"]["due"][0]
        self.assertEqual((d["mode"], d["ownership"]), ("scooter", "confirmed"))

    def test_unknown_key_is_json_error(self):
        self.assertIn("error", self.run_cli("answer", "--key", "nope", "--mode", "walk"))
        self.assertIn("error", self.run_cli("fired", "--key", "nope", "--checkpoint", "t7d", "--outcome", "said"))


class MailTests(Base):
    def test_mail_trip_merges_into_calendar_event(self):
        event_start = datetime(2026, 11, 28, 11, 0, tzinfo=BER)
        self.cal = [ev("ws", "Sam's workshop", event_start, location="Market Street 32, 20095 Hamburg")]
        self.mails = [mail("m1", "Your tickets 28.11. Berlin – Hamburg (09:50 - 10:44)", NOW - timedelta(days=2)),
                      mail("m2", "Your tickets 18.12. Hamburg – Berlin (23:10 - 1:10)", NOW - timedelta(days=1))]
        due = self.scan()["context"]["due"]
        keys = [d["key"] for d in due]
        self.assertIn("la:cal:ws", keys)
        self.assertNotIn("la:mail:m1", keys)
        merged = due[keys.index("la:cal:ws")]
        self.assertEqual(merged["ownership"], "confirmed")
        self.assertIn(f"gmail:{ACCOUNT}:m1", merged["sources"])
        self.assertEqual(merged["legs"][0]["arrive"], "2026-11-28T10:44+01:00")
        self.assertEqual(self.state()["items"]["la:cal:ws"]["mail_ids"], ["m1"])
        self.assertNotIn("la:mail:m1", self.state()["items"])
        # the unmatched trip stays its own confirmed train item
        trip = self.state()["items"]["la:mail:m2"]
        self.assertEqual((trip["type"], trip["ownership"], trip["end"]), ("train", "confirmed", "2026-12-19T01:10+01:00"))

    def test_no_merge_without_shared_city(self):
        self.cal = [ev("x", "Dinner", datetime(2026, 11, 28, 11, 0, tzinfo=BER), location="Market Street 1, Munich")]
        self.mails = [mail("m1", "Your tickets 28.11. Berlin – Hamburg (09:50 - 10:44)", NOW - timedelta(days=2))]
        self.assertEqual(set(self.due_keys(self.scan())), {"la:cal:x", "la:mail:m1"})

    def test_other_sender_needs_detail_only_if_recent(self):
        self.mails = [mail("f1", "Your flight booking confirmation", NOW - timedelta(days=3),
                           sender="Finnair <noreply@finnair.com>"),
                      mail("f2", "Your flight booking confirmation", NOW - timedelta(days=20),
                           sender="Finnair <noreply@finnair.com>")]
        due = self.scan()["context"]["due"]
        self.assertEqual([d["key"] for d in due], ["la:mail:f1"])
        d = due[0]
        self.assertEqual((d["type"], d["needs_detail"], d["ownership"], d["start"], d["checkpoint"]),
                         ("flight", True, "unknown", None, "detail"))
        self.run_cli("fired", "--key", "la:mail:f1", "--checkpoint", "detail", "--outcome", "said")
        self.assertEqual(self.scan(now=NOW + timedelta(hours=1)), {"wakeAgent": False})

    def test_failing_source_recorded_and_not_fatal(self):
        self.mails = [mail("m2", "Your tickets 27.11. Berlin – Hamburg (23:10 - 1:10)", NOW - timedelta(days=1))]
        mf = self.tmp / "mail.json"
        mf.write_text(json.dumps(self.mails))
        out = self.run_cli("scan", "--calendar-json", self.tmp / "missing.json", "--mail-json", mf, "--now", ts(NOW))
        self.assertTrue(out["wakeAgent"])
        unread = out["context"]["sources_unread"]
        self.assertTrue(any(u["source"].startswith("calendar") and "FileNotFoundError" in u["error"] for u in unread))
        self.assertEqual(self.due_keys(out), ["la:mail:m2"])
        self.assertEqual(self.state()["last_scan"]["sources_unread"], unread)

    def test_live_source_failure_is_recorded_with_the_configured_accounts(self):
        self.live.side_effect = None
        self.live.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="HttpError 401: invalid_grant")
        out = self.run_cli("scan", "--now", ts(NOW))
        self.assertEqual(out, {"wakeAgent": False})
        unread = self.state()["last_scan"]["sources_unread"]
        self.assertEqual([u["source"] for u in unread][:2], [f"calendar:{ACCOUNT}", f"mail:{ACCOUNT}"])
        self.assertIn("invalid_grant", unread[0]["error"])

    def test_outage_keeps_stored_items_alive(self):
        start = (NOW + timedelta(days=3)).replace(hour=11)
        self.cal = [ev("tcg", "Card night", start, location="Game Shop 3")]
        self.scan()
        self.run_cli("fired", "--key", "la:cal:tcg", "--checkpoint", "t7d", "--outcome", "quiet")
        later = start - timedelta(days=2) + timedelta(minutes=5)
        out = self.run_cli("scan", "--calendar-json", self.tmp / "missing.json", "--mail-json", self.tmp / "mail.json",
                           "--now", ts(later))
        self.assertEqual(out["context"]["due"][0]["checkpoint"], "t2d")


class CliTests(Base):
    def test_list_shows_next_checkpoint(self):
        start = (NOW + timedelta(days=3)).replace(hour=11)
        self.cal = [ev("tcg", "Card night", start, location="Game Shop 3"),
                    ev("far", "Flight LH5 to Tokyo", NOW + timedelta(days=25), location="Berlin Airport")]
        self.scan()
        res = self.run_cli("list", "--days", "7", "--now", ts(NOW))
        self.assertEqual([r["key"] for r in res["items"]], ["la:cal:tcg"])
        self.assertEqual(res["items"][0]["next_checkpoint"], {"name": "t2d", "at": la.iso(start - timedelta(days=2))})
        self.assertEqual(self.run_cli("list", "--days", "30", "--now", ts(NOW))["count"], 2)

    def run_script(self, script, *args):
        return REAL_RUN([sys.executable, str(script), *args], capture_output=True, text=True,
                        env={**os.environ, **isolated_env(self.root)}, timeout=60)

    def test_subprocess_last_line_and_exit_zero_on_error(self):
        bad = self.run_script(SCRIPT, "scan", "--now", "not-a-date")
        self.assertEqual(bad.returncode, 0)
        lines = bad.stdout.strip().splitlines()
        self.assertIn("error", json.loads(lines[0]))
        self.assertEqual(json.loads(lines[-1]), {"wakeAgent": False})
        (self.tmp / "c.json").write_text("[]")
        (self.tmp / "m.json").write_text("[]")
        ok = self.run_script(SCRIPT, "scan", "--calendar-json", str(self.tmp / "c.json"),
                             "--mail-json", str(self.tmp / "m.json"), "--now", ts(NOW))
        self.assertEqual((ok.returncode, json.loads(ok.stdout.strip().splitlines()[-1])), (0, {"wakeAgent": False}))
        unknown = self.run_script(SCRIPT, "answer", "--key", "k", "--mode", "fly")
        self.assertEqual(unknown.returncode, 0)
        self.assertIn("error", json.loads(unknown.stdout.strip().splitlines()[-1]))

    def test_gate_script_never_wakes_when_sources_are_unreadable(self):
        self.configure(**{**self.settings, "quiet_hours": {"start": 0, "end": 0}})  # never quiet, whatever the clock
        gate = self.run_script(GATE)  # HOME and HERMES_HOME are temp dirs: no google_api.py, nothing live to call
        self.assertEqual(gate.returncode, 0, gate.stderr)
        self.assertEqual(json.loads(gate.stdout.strip().splitlines()[-1]), {"wakeAgent": False})
        unread = self.state()["last_scan"]["sources_unread"]
        self.assertEqual([u["source"] for u in unread][:2], [f"calendar:{ACCOUNT}", f"mail:{ACCOUNT}"])


class NoiseRules(Base):
    def _ev(self, i, title, loc, day):
        return {"id": f"e{i}", "summary": title, "location": loc,
                "start": {"dateTime": f"2026-12-{day:02d}T17:00:00+01:00"},
                "end": {"dateTime": f"2026-12-{day:02d}T20:00:00+01:00"}}

    def test_watched_abroad_is_not_attended(self):
        self.configure(**{**self.settings, "lookahead": {"home_places": ["Berlin"], "watch_words": [r"grand prix"]}})
        evs = [self._ev(1, "Grand Prix: Qualifying", "Losail International Circuit, Qatar", 5),
               self._ev(2, "Grand Prix watch party", "Bar Central, Berlin", 6)]
        self.assertEqual([i["title"] for i in la.calendar_items(evs, NOW)], ["Grand Prix watch party"])

    def test_weekly_event_is_tier_c_with_one_checkpoint(self):
        evs = [self._ev(i, "Board game league", "Game Shop, Hamburg", d) for i, d in enumerate((1, 8, 15))]
        evs.append(self._ev(9, "Workshop: Sam's", "Market Street 32, 20095 Berlin", 12))
        items = {i["title"]: i for i in la.calendar_items(evs, NOW)}
        self.assertTrue(items["Board game league"]["recurring"])
        self.assertNotIn("recurring", items["Workshop: Sam's"])
        start = items["Board game league"]["start"]
        self.assertEqual(list(la.schedule("event", start, True)), ["t1d"])


class PersonalBase(Base):
    """Look-ahead reads every mailbox, work and personal: himalaya rows come in next to the Gmail API mail."""

    def scan_p(self, rows, unread=None, now=NOW):
        cf, mf, pf = self.tmp / "cal.json", self.tmp / "mail.json", self.tmp / "personal.json"
        cf.write_text(json.dumps(self.cal))
        mf.write_text(json.dumps(self.mails))
        pf.write_text(json.dumps({"rows": rows, "unread": unread or []}))
        return self.run_cli("scan", "--calendar-json", cf, "--mail-json", mf, "--personal-json", pf, "--now", ts(now))

    @staticmethod
    def row(acc, mid, subject, sent, sender, folder="[Gmail]/All Mail"):
        return {"id": f"{acc}:{mid}", "date": sent.strftime("%Y-%m-%d %H:%M+00:00"), "subject": subject,
                "from": sender, "account": acc, "folder": folder}


class PersonalMailTests(PersonalBase):
    def test_ticket_in_personal_mailbox_becomes_confirmed_train(self):
        sent = NOW - timedelta(days=1)
        self.scan_p([self.row("home", "77", "Your tickets 29.11. Berlin – Hamburg (09:50 - 10:44)", sent, "tickets@vr.fi")])
        row = self.state()["items"]["la:mail:home:77"]
        self.assertEqual((row["type"], row["ownership"]), ("train", "confirmed"))
        self.assertEqual(row["sources"], ["himalaya:home:[Gmail]/All Mail:77"])

    def test_marketing_and_login_alerts_are_ignored(self):
        sent = NOW - timedelta(days=1)
        self.scan_p([self.row("a", "1", "Your flight search to Oslo is waiting", sent, "Offers@email.finnair.com"),
                     self.row("b", "2", "We detected a new login", sent, "noreply@finnair.com"),
                     self.row("b", "3", "Promotional Award Tickets", sent, "milesandsmiles@miles.example")])
        self.assertEqual(self.state()["items"], {})

    def test_registration_mail_confirms_calendar_event_and_is_not_its_own_item(self):
        self.cal = [ev("hh", "Showdown: Spielwiese Hamburg", NOW + timedelta(days=3), location="Spielwiese Hamburg")]
        self.scan_p([self.row("home", "6229", "You Are Registered For Spielwiese Hamburg Night City Showdown",
                              NOW - timedelta(days=4), "noreply@melee.gg")])
        items = self.state()["items"]
        self.assertEqual(list(items), ["la:cal:hh"])
        self.assertEqual(items["la:cal:hh"]["ownership"], "confirmed")

    def test_generic_words_do_not_confirm_an_unrelated_event(self):
        self.cal = [ev("x", "Showdown: Corner Hobby Shop (Berlin)", NOW + timedelta(days=3), location="Market Street 32, Berlin")]
        self.scan_p([self.row("home", "6155", "You Are Registered For Night City Showdown", NOW - timedelta(days=4),
                              "noreply@melee.gg")])
        self.assertEqual(self.state()["items"]["la:cal:x"]["ownership"], "likely")

    def test_configured_generic_words_do_not_confirm_either(self):
        self.configure(**{**self.settings, "lookahead": {"generic_words": ["Spielwiese"]}})
        self.cal = [ev("hh", "Showdown: Spielwiese Hamburg", NOW + timedelta(days=3), location="Hamburg")]
        self.scan_p([self.row("home", "1", "You Are Registered For Spielwiese Munich", NOW - timedelta(days=4),
                              "noreply@melee.gg")])
        self.assertEqual(self.state()["items"]["la:cal:hh"]["ownership"], "likely")

    def test_failed_mailbox_is_reported_not_hidden(self):
        failed = {"source": "mail:work2", "error": "himalaya exit 1"}
        self.scan_p([], unread=[failed])
        self.assertIn(failed, self.state()["last_scan"]["sources_unread"])

    def test_gmail_created_calendar_event_counts_as_booked(self):
        self.cal = [ev("tr", "Train to Hamburg", NOW + timedelta(days=3), location="Berlin",
                       description="This event was created from an email you received in Gmail. https://mail.google.com")]
        self.scan_p([])
        self.assertEqual(self.state()["items"]["la:cal:tr"]["ownership"], "confirmed")

    def himalaya(self, accounts, folders=None, failing=(), envelopes=None):
        """A fake `himalaya`: records every call and answers account, folder and envelope lists."""
        calls = []
        envelopes = envelopes or [{"id": "9", "date": "2026-11-24 10:00+00:00", "from": {"addr": "tickets@vr.fi"},
                                   "subject": "Your tickets 29.11. Berlin – Hamburg (09:50 - 10:44)"}]

        def fake(args, **kw):
            calls.append(args[1:])
            acc = args[args.index("-a") + 1] if "-a" in args else None
            if acc in failing:
                return subprocess.CompletedProcess(args, 1, "", "boom")
            if args[1:3] == ["account", "list"]:
                out = [{"name": a} for a in accounts]
            elif args[1:3] == ["folder", "list"]:
                out = [{"name": f} for f in (folders or {}).get(acc, ["INBOX"])]
            else:
                out = envelopes
            return subprocess.CompletedProcess(args, 0, json.dumps(out), "")
        self.live.side_effect = fake
        return calls

    def test_live_fetch_skips_configured_account_and_survives_one_failure(self):
        self.configure(**{**self.settings, "mail": {"himalaya_accounts": ["work", "home", "other"]},
                          "lookahead": {"skip_accounts": ["work"]}})
        calls = self.himalaya(["work", "home", "other"], failing=["other"])
        rows, unread = la.fetch_personal(NOW, None)
        self.assertEqual([r["id"] for r in rows], ["home:9"])
        self.assertEqual([u["source"] for u in unread], ["mail:other"])
        self.assertFalse([c for c in calls if "work" in c])
        query = next(c for c in calls if c[:2] == ["envelope", "list"] and "home" in c)[-1]
        self.assertTrue(query.startswith("after 2026-09-26 and (from "), query)
        self.assertIn("from vr.fi", query)
        self.assertIn("subject registered", query)
        self.assertIn("25", next(c for c in calls if c[:2] == ["envelope", "list"]))  # newest 25 per account

    def test_skip_accounts_applies_to_configured_accounts_without_listing_them(self):
        self.configure(**{**self.settings, "mail": {"himalaya_accounts": ["a", {"account": "b", "folders": ["INBOX"]}, "c"]},
                          "lookahead": {"skip_accounts": ["b"]}})
        calls = self.himalaya(["never-listed"])
        rows, unread = la.fetch_personal(NOW, None)
        self.assertEqual(sorted(r["account"] for r in rows), ["a", "c"])
        self.assertEqual(unread, [])
        self.assertNotIn(["account", "list", "-o", "json"], calls)
        self.assertFalse([c for c in calls if "b" in c])

    def test_folder_is_detected_per_account_unless_configured(self):
        self.configure(**{**self.settings, "mail": {"himalaya_accounts": ["g", "plain", "pinned"]},
                          "lookahead": {"folders": {"pinned": "Travel"}}})
        calls = self.himalaya([], folders={"g": ["INBOX", "[Gmail]/All Mail"], "plain": ["INBOX", "Sent"],
                                          "pinned": ["INBOX", "[Gmail]/All Mail"]})
        rows, _ = la.fetch_personal(NOW, None)
        self.assertEqual({r["account"]: r["folder"] for r in rows}, {"g": "[Gmail]/All Mail", "plain": "INBOX", "pinned": "Travel"})
        listed = [c[c.index("-a") + 1] for c in calls if c[:2] == ["folder", "list"]]
        self.assertEqual(sorted(listed), ["g", "plain"])  # once per account, never for a configured folder
        self.assertEqual(len(listed), len(set(listed)))
        sources = {r["account"]: la.mail_items([r], NOW)[0]["sources"][0] for r in rows}
        self.assertEqual(sources["g"], "himalaya:g:[Gmail]/All Mail:9")
        self.assertEqual(sources["pinned"], "himalaya:pinned:Travel:9")

    def test_no_himalaya_means_no_personal_mailboxes_and_no_noise(self):
        self.live.side_effect = FileNotFoundError("himalaya")
        self.assertEqual(la.fetch_personal(NOW, None), ([], []))

    def test_no_configured_accounts_reads_no_mailbox(self):
        calls = self.himalaya(["work", "home"])  # himalaya knows accounts, but none were given to the desk
        self.assertEqual(la.fetch_personal(NOW, None), ([], []))
        self.assertEqual(calls, [])

    def test_personal_outage_keeps_stored_items_alive(self):
        sent = NOW - timedelta(days=1)
        depart = (NOW + timedelta(days=2)).replace(hour=9, minute=50)
        self.scan_p([self.row("home", "5", f"Your tickets {depart:%d.%m.} Berlin – Hamburg (09:50 - 10:44)", sent, "tickets@vr.fi")])
        self.run_cli("fired", "--key", "la:mail:home:5", "--checkpoint", "t2d", "--outcome", "quiet")
        later = NOW.replace(day=26, hour=19, minute=5)  # t1d, 19:00 the evening before
        out = self.scan_p([], unread=[{"source": "mail:home", "error": "himalaya exit 1"}], now=later)
        self.assertEqual(self.due_keys(out), ["la:mail:home:5"])
        self.assertEqual(self.due_keys(self.scan_p([], now=later)), [])  # read fine and gone: not kept alive


class RegistrationVenueTests(PersonalBase):
    def test_registration_at_same_shop_does_not_confirm_other_events_there(self):
        self.cal = [ev("pl", "Weekly League Cup", NOW + timedelta(days=6), location="Spielwiese, Market Street 15, Hamburg")]
        self.scan_p([self.row("home", "6205", "You Are Registered For Night City Brawl - Spielwiese Hamburg",
                              NOW - timedelta(days=5), "noreply@melee.gg")])
        self.assertEqual(self.state()["items"]["la:cal:pl"]["ownership"], "likely")


class JobTests(unittest.TestCase):
    def setUp(self):
        home = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"HERMES_HOME": home}, clear=True))
        self.home = Path(home)
        self.run = FakeCron()
        self.enterContext(patch.object(jobs, "_plugin_ensure", self.run.ensure))
        self.enterContext(patch.object(jobs, "api_problem", return_value=None))
        self.cfg = cli.base_config("custom")
        self.cfg["owner"]["name"] = "Alex"
        self.cfg["timezone"] = "Europe/Berlin"
        self.cfg["mail"]["gmail_api"] = True
        self.cfg["features"].update({key: key == "lookahead" for key in self.cfg["features"]})

    def created(self):
        return [argv for argv in self.run.calls if argv[2] == "create"]

    def test_default_config_registers_one_agent_job_with_the_gate_script(self):
        rows = jobs.apply(self.cfg, run=self.run)
        self.assertEqual([r["action"] for r in rows if r["job"] != "Proactive · say"].count("create"), 1)
        jid = self.run.job("Proactive · look-ahead")
        argv = self.run.live[jid]
        opt = lambda flag: argv[argv.index(flag) + 1]  # noqa: E731
        # Reads untrusted mail and operator pages: web for timetables, no browser (it never books).
        self.assertEqual(self.run.toolsets[jid], ["terminal", "file", "skills", "web"])
        self.assertEqual(opt("--script"), "proactive-hermes/lookahead_gate.py")
        self.assertEqual((opt("--deliver"), opt("--failure-deliver"), opt("--timezone")), ("local", "local", "Europe/Berlin"))
        self.assertEqual(opt("--name"), "Proactive · look-ahead")
        # browser-bookings is jobs.py's call (review item 1: not in a scheduled job); the desk skill always loads
        self.assertIn("proactive-hermes:proactive-desk", [argv[i + 1] for i, a in enumerate(argv) if a == "--skill"])
        self.assertNotIn("--no-agent", argv)
        self.assertEqual(self.run.schedule_of(jid), "*/30 7-22 * * *")
        prompt = self.run.prompt_of(jid)
        self.assertIn("ea_lookahead.py fired", prompt)
        self.assertIn("compose --job lookahead", prompt)
        self.assertIn(str(self.home / "scripts" / "proactive-hermes"), prompt)
        self.assertNotIn("{{", prompt)
        self.assertTrue((self.home / "scripts" / "proactive-hermes" / "lookahead_gate.py").is_file())
        self.assertTrue((self.home / "scripts" / "proactive-hermes" / "ea_lookahead.py").is_file())

    def test_reapply_is_a_no_op_and_installed_contract_has_the_sections(self):
        jobs.apply(self.cfg, run=self.run)
        self.run.calls.clear()
        jobs.apply(self.cfg, run=self.run)
        self.assertFalse([argv for argv in self.run.calls if argv[2] != "list"])
        contract = (self.home / "scripts" / "proactive-hermes" / "desk-contract.md").read_text()
        for heading in ("### Closing items", "### Look ahead like Dots"):
            self.assertIn(heading, contract)
        self.assertNotIn("{{", contract)

    def test_schedule_override_edits_in_place(self):
        jobs.apply(self.cfg, run=self.run)
        self.cfg["schedule"]["lookahead"] = "*/15 8-20 * * *"
        self.run.calls.clear()
        jobs.apply(self.cfg, run=self.run)
        (edit,) = [argv for argv in self.run.calls if argv[2] == "edit"]
        self.assertEqual(edit[edit.index("--schedule") + 1], "*/15 8-20 * * *")
        self.assertIn("proactive-hermes:proactive-desk", [edit[i + 1] for i, a in enumerate(edit) if a == "--skill"])

    def test_not_created_without_google_or_when_the_feature_is_off(self):
        self.cfg["mail"]["gmail_api"] = False
        self.cfg["calendar"]["enabled"] = False
        rows = {row["job"]: row["action"] for row in jobs.apply(self.cfg, run=self.run)}
        self.assertEqual(rows["Proactive · look-ahead"], "skip")
        self.assertFalse(self.run.live)
        self.cfg["mail"]["gmail_api"] = True
        self.cfg["features"]["lookahead"] = False
        jobs.apply(self.cfg, run=self.run)
        self.assertFalse(self.run.live)

    def test_other_jobs_have_no_skill_flags(self):
        self.cfg["features"].update(lookahead=False, health=True)
        jobs.apply(self.cfg, run=self.run)
        self.assertTrue(self.created())
        self.assertFalse([argv for argv in self.created() if "--skill" in argv])


# ----------------------------------------------------------------------------- review fixes (robustness)

class ConcurrencyTests(Base):
    """Item 2: scan fetches without the lock, then load-change-save under it; nothing written meanwhile is lost."""

    def setUp(self):
        super().setUp()
        self.cal = [ev("tcg", "Card night", (NOW + timedelta(days=3)).replace(hour=11), location="Game Shop 3, Berlin"),
                    ev("gig", "Gig", (NOW + timedelta(days=4)).replace(hour=20), location="Club Mitte, Berlin")]

    def test_answer_written_during_a_slow_scan_survives(self):
        self.scan()
        real = la.fetch_calendar

        def slow_fetch(now, fixture):  # the owner answers while the scan is still reading its sources
            self.run_cli("answer", "--key", "la:cal:tcg", "--mode", "not-mine", "--now", ts(now))
            return real(now, fixture)

        with patch.object(la, "fetch_calendar", side_effect=slow_fetch):
            self.scan(now=NOW + timedelta(hours=3))
        row = self.state()["items"]["la:cal:tcg"]
        self.assertEqual(row["status"], "dropped")
        self.assertEqual(row["answers"][-1]["mode"], "not-mine")

    def test_parallel_processes_never_lose_a_write(self):
        self.scan()
        env = {**os.environ, **isolated_env(self.root)}
        procs = [subprocess.Popen([sys.executable, str(SCRIPT), "answer", "--key", key, "--mode", "scooter",
                                   "--note", f"n{i}"], env=env, stdout=subprocess.PIPE, text=True)
                 for i in range(6) for key in ("la:cal:tcg", "la:cal:gig")]
        for p in procs:
            p.communicate(timeout=60)
        items = self.state()["items"]
        self.assertEqual(len(items["la:cal:tcg"]["answers"]) + len(items["la:cal:gig"]["answers"]), 12)
        self.assertEqual(sorted(p.name for p in self.tmp.glob("*.tmp")), [])  # no stray temp files


class RewakeTests(Base):
    """Item 3: a checkpoint that woke the agent is "offered"; without `fired` it re-offers after 2h, twice, then missed."""

    def test_offered_then_reoffered_twice_then_missed(self):
        self.cal = [ev("fl1", "Flight LH1331 to Stockholm", (NOW + timedelta(days=5)).replace(hour=12),
                       location="Berlin Airport")]
        self.assertEqual(self.due_keys(self.scan()), ["la:cal:fl1"])
        cp = lambda: self.state()["items"]["la:cal:fl1"]["checkpoints"]["t7d"]  # noqa: E731
        self.assertEqual((cp()["state"], cp()["offers"]), ("offered", 1))
        self.assertEqual(self.scan(now=NOW + timedelta(minutes=20)), {"wakeAgent": False})
        self.assertEqual(self.scan(now=NOW + timedelta(hours=1, minutes=59)), {"wakeAgent": False})
        self.assertEqual(self.due_keys(self.scan(now=NOW + timedelta(hours=2))), ["la:cal:fl1"])
        self.assertEqual(self.due_keys(self.scan(now=NOW + timedelta(hours=4))), ["la:cal:fl1"])
        self.assertEqual(cp()["offers"], 3)
        self.assertEqual(self.scan(now=NOW + timedelta(hours=6)), {"wakeAgent": False})
        self.assertEqual(cp()["state"], "missed")
        self.assertEqual(self.scan(now=NOW + timedelta(hours=9)), {"wakeAgent": False})

    def test_leave_checkpoint_does_not_wake_every_scan(self):
        start = (NOW + timedelta(days=1)).replace(hour=12)
        self.cal = [ev("tr", "Train to Hamburg", start, location="Berlin Hbf")]
        leave = start - timedelta(minutes=90)
        self.assertEqual(self.scan(now=leave + timedelta(minutes=1))["context"]["due"][0]["checkpoint"], "leave")
        self.assertEqual(self.scan(now=leave + timedelta(minutes=21)), {"wakeAgent": False})

    def test_pending_change_wakes_once_and_again_when_moved_again(self):
        start = (NOW + timedelta(days=5)).replace(hour=12)
        self.cal = [ev("fl1", "Flight LH1331 to Stockholm", start, location="Berlin Airport")]
        self.scan()
        self.run_cli("fired", "--key", "la:cal:fl1", "--checkpoint", "t7d", "--outcome", "said")
        self.cal = [ev("fl1", "Flight LH1331 to Stockholm", start + timedelta(hours=3), location="Berlin Airport")]
        self.assertTrue(self.scan(now=NOW + timedelta(hours=1))["context"]["due"][0]["changed"])
        self.assertEqual(self.scan(now=NOW + timedelta(hours=1, minutes=30)), {"wakeAgent": False})
        self.cal = [ev("fl1", "Flight LH1331 to Stockholm", start + timedelta(hours=5), location="Berlin Airport")]
        d = self.scan(now=NOW + timedelta(hours=1, minutes=40))["context"]["due"][0]
        self.assertEqual((d["changed"], d["old_start"]), (True, la.iso(start)))

    def test_only_shown_items_count_as_offered(self):
        self.cal = [ev(f"e{i}", f"Gig {i}", NOW + timedelta(days=6 - i * 0.5), location="Club Mitte, Berlin")
                    for i in range(8)]
        shown = set(self.due_keys(self.scan()))
        self.assertEqual(len(shown), 6)
        rest = set(self.due_keys(self.scan(now=NOW + timedelta(minutes=30))))
        self.assertEqual(len(rest), 2)
        self.assertFalse(shown & rest)


class QuietCheckpointTests(Base):
    """Item 4: a checkpoint inside quiet hours moves to the last scan before they begin (22:30 for 23-07)."""

    def test_early_train_leave_time_comes_the_evening_before(self):
        start = datetime(2026, 12, 1, 7, 30, tzinfo=BER)
        cps = la.schedule("train", start)
        self.assertEqual(cps["leave"], datetime(2026, 11, 30, 22, 30, tzinfo=BER))
        self.assertEqual(cps["t1d"], datetime(2026, 11, 30, 19, 0, tzinfo=BER))
        self.cal = [ev("tr", "Train to Hamburg", start, location="Berlin Hbf")]
        self.scan(now=datetime(2026, 11, 30, 19, 5, tzinfo=BER))
        out = self.scan(now=datetime(2026, 11, 30, 22, 30, tzinfo=BER))  # the last scan before quiet hours
        self.assertEqual(out["context"]["due"][0]["checkpoint"], "leave")

    def test_early_flight_morning_and_leave_wake_once_before_quiet(self):
        f = la.schedule("flight", datetime(2026, 12, 1, 6, 0, tzinfo=BER))  # leave 02:00, morning 03:00
        self.assertEqual(f["leave"], datetime(2026, 11, 30, 22, 30, tzinfo=BER))
        self.assertNotIn("morning", f)  # same slot: one wake, leave wins
        self.assertEqual(f["t1d"], datetime(2026, 11, 30, 19, 0, tzinfo=BER))
        self.assertTrue(all(not la.is_quiet(at) for at in f.values()))

    def test_configured_quiet_hours_move_checkpoints(self):
        self.configure(**{**self.settings, "quiet_hours": {"start": 21, "end": 9}})
        cps = la.schedule("event", datetime(2026, 12, 1, 9, 30, tzinfo=BER))
        self.assertEqual(cps["leave"], datetime(2026, 11, 30, 20, 30, tzinfo=BER))
        self.assertNotIn("morning", cps)  # 07:30 also moved onto 20:30: leave wins
        self.assertTrue(all(not la.is_quiet(at) for at in cps.values()))


class CrossSourceDedupeTests(PersonalBase):
    """Item 6: one booking mail is one item, whichever source or folder saw it first."""

    SUBJECT = "Your booking confirmation for Hotel Alster"

    def gmail_and_himalaya(self, sent):
        self.mails = [mail("g1", self.SUBJECT, sent, sender="Booking.com <noreply@booking.com>")]
        return self.row("home", "41", self.SUBJECT, sent.astimezone(ZoneInfo("UTC")), "noreply@booking.com")

    def test_same_mail_from_gmail_api_and_himalaya_is_one_item(self):
        sent = (NOW - timedelta(days=1)).replace(second=37)
        out = self.scan_p([self.gmail_and_himalaya(sent)])
        self.assertEqual(self.due_keys(out), ["la:mail:g1"])
        row = self.state()["items"]["la:mail:g1"]
        self.assertEqual(row["sources"], [f"gmail:{ACCOUNT}:g1", "himalaya:home:[Gmail]/All Mail:41"])
        self.assertNotIn("la:mail:home:41", self.state()["items"])

    def test_folder_fallback_keeps_the_first_key(self):
        sent = NOW - timedelta(days=1)
        first = self.row("home", "41", self.SUBJECT, sent, "noreply@booking.com")
        self.scan_p([first])
        self.run_cli("answer", "--key", "la:mail:home:41", "--mode", "not-mine")
        inbox = self.row("home", "7", self.SUBJECT, sent, "Booking <noreply@booking.com>", folder="INBOX")
        self.assertEqual(self.scan_p([inbox], now=NOW + timedelta(hours=3)), {"wakeAgent": False})
        self.assertEqual(set(self.state()["items"]), {"la:mail:home:41"})

    def test_message_id_matches_when_the_source_gives_one(self):
        sent = NOW - timedelta(days=1)
        a = {**self.row("home", "1", self.SUBJECT, sent, "noreply@booking.com"), "message_id": "<ABC@booking.com>"}
        b = {**self.row("work", "2", self.SUBJECT + " (fwd)", sent, "me@acme.example"), "message_id": "abc@booking.com"}
        self.scan_p([a, b])
        self.assertEqual([k for k in self.state()["items"]], ["la:mail:home:1"])

    def test_different_mails_stay_apart(self):
        sent = NOW - timedelta(days=1)
        self.scan_p([self.row("home", "1", self.SUBJECT, sent, "noreply@booking.com"),
                     self.row("home", "2", self.SUBJECT, sent + timedelta(minutes=5), "noreply@booking.com")])
        self.assertEqual(len(self.state()["items"]), 2)


class ListClosedTests(Base):
    """Item 10: `list` never shows an item closed through ea_close."""

    def test_list_hides_keys_closed_in_the_ledger(self):
        self.cal = [ev("tcg", "Card night", (NOW + timedelta(days=3)).replace(hour=11), location="Game Shop 3"),
                    ev("gig", "Gig", (NOW + timedelta(days=4)).replace(hour=20), location="Club Mitte, Berlin")]
        self.scan()
        (self.tmp / "actions.json").write_text(json.dumps({"la:cal:tcg": {"status": "done"}}))
        res = self.run_cli("list", "--days", "30", "--now", ts(NOW))
        self.assertEqual([r["key"] for r in res["items"]], ["la:cal:gig"])


class NoteFileTests(Base):
    """Untrusted text never rides argv: --note-file reads it literally, nothing in it runs."""

    EVIL = "Ref $(touch PWNED) and `touch PWNED2` ; rm -rf / \"quoted\" 'single' ünïcode"

    def setUp(self):
        super().setUp()
        self.cal = [ev("tcg", "Card night", (NOW + timedelta(days=3)).replace(hour=11), location="Game Shop 3")]
        self.scan()

    def test_note_file_round_trips_literally_and_runs_nothing(self):
        work = self.tmp / "cwd"
        work.mkdir()
        note = self.tmp / "note.txt"
        note.write_text(self.EVIL + "\n", encoding="utf-8")
        p = REAL_RUN([sys.executable, str(SCRIPT), "answer", "--key", "la:cal:tcg", "--mode", "booked",
                      "--note-file", str(note)], capture_output=True, text=True, cwd=work,
                     env={**os.environ, **isolated_env(self.root)}, timeout=60)
        self.assertEqual(json.loads(p.stdout.splitlines()[-1])["status"], "recorded", p.stdout + p.stderr)
        row = self.state()["items"]["la:cal:tcg"]
        self.assertEqual((row["note"], row["answers"][-1]["note"]), (self.EVIL, self.EVIL))
        for d in (work, self.tmp, RUNTIME):
            self.assertFalse((d / "PWNED").exists() or (d / "PWNED2").exists())

    def test_note_from_stdin_and_flags_are_exclusive(self):
        with patch.object(sys, "stdin", io.StringIO(self.EVIL + "\n")):
            self.run_cli("answer", "--key", "la:cal:tcg", "--mode", "scooter", "--note-file", "-")
        self.assertEqual(self.state()["items"]["la:cal:tcg"]["note"], self.EVIL)
        out = self.run_cli("answer", "--key", "la:cal:tcg", "--mode", "walk", "--note", "x", "--note-file", "-")
        self.assertIn("not allowed with", out["error"])


class StateDirTests(unittest.TestCase):
    """Security 4: the state dir (senders, subjects, ledgers) is owner-only, also when it already existed open."""

    def test_state_dir_is_0700(self):
        with TemporaryDirectory() as tmp:
            homes = Path(tmp) / "new", Path(tmp) / "loose"
            loose = homes[1] / "plugin-data" / "proactive-hermes" / "state"
            loose.mkdir(mode=0o755, parents=True)
            loose.chmod(0o755)
            for home in homes:
                d = home / "plugin-data" / "proactive-hermes" / "state"
                with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
                    self.assertEqual(phconf.state_dir(), d)
                self.assertEqual(d.stat().st_mode & 0o777, 0o700)
