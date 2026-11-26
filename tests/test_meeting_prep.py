"""Meeting prep: the gate (which meetings get prepped, once, in the right window), the attendee-aware calendar
reader, the cron job and the evening "prep tonight" instruction. Offline: no calendar or network call."""
from __future__ import annotations

import io
import json
import os
import re
import stat
import subprocess
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from .test_desk import OfflineTestCase, phconf
from .test_setup import FakeCron
from .test_text_files import INLINE

import cli
import gcal_full
import jobs
import meeting_prep_gate as g

BER = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 25, 13, 0, tzinfo=BER)  # Wednesday 13:00 in the owner's timezone
OWNER = "alex@acme.example"
ME = {"email": OWNER, "self": True, "response": "accepted"}


def ev(eid, title, mins, attendees=(), **kw):
    start = NOW + timedelta(minutes=mins)
    row = {"id": eid, "summary": title, "start": start.isoformat(), "end": (start + timedelta(minutes=30)).isoformat(),
           "status": "confirmed", "all_day": False, "description": "", "attendees": list(attendees),
           "organizer": {"email": OWNER}, "recurring": False}
    row.update(kw)
    return row


class GateBase(OfflineTestCase):
    SETTINGS: dict = {}

    def setUp(self):
        super().setUp()
        self.configure(timezone="Europe/Berlin", owner={"name": "Alex", "work_emails": [OWNER]}, **self.SETTINGS)

    def run_gate(self, events, now=NOW):
        f = self.tmp / "ev.json"
        f.write_text(json.dumps(events))
        return g.scan(now, str(f))

    def offered(self):
        return json.loads((self.tmp / "meeting_prep.json").read_text())["offered"]


class GateTests(GateBase):
    def test_external_meeting_in_window_is_offered_once(self):
        evs = [ev("a", "Pilot review", 50, [ME, {"email": "sam@partner.example", "response": "accepted"}])]
        out = self.run_gate(evs)
        self.assertTrue(out["wakeAgent"])
        m = out["context"]["meetings"][0]
        self.assertEqual((m["title"], m["external"], m["daily"]), ("Pilot review", ["partner.example"], False))
        g.done(m["key"], "said")
        self.assertEqual(self.run_gate(evs, NOW + timedelta(minutes=15)), {"wakeAgent": False})

    def test_unanswered_offer_comes_back_once_after_10_minutes(self):
        evs = [ev("a", "Client call", 50, [ME, {"email": "sam@partner.example"}])]
        self.assertTrue(self.run_gate(evs)["wakeAgent"])
        self.assertEqual(self.run_gate(evs, NOW + timedelta(minutes=9)), {"wakeAgent": False})   # too soon
        (m,) = self.run_gate(evs, NOW + timedelta(minutes=15))["context"]["meetings"]
        self.assertTrue(m["reoffer"])
        self.assertEqual(self.run_gate(evs, NOW + timedelta(minutes=30)), {"wakeAgent": False})  # at most once more
        row = next(iter(json.loads((self.tmp / "meeting_prep.json").read_text())["offered"].values()))
        self.assertEqual((row["offers"], row["outcome"]), (2, None))

    def test_unanswered_offer_is_not_repeated_once_the_meeting_left_the_window(self):
        evs = [ev("a", "Client call", 20, [ME, {"email": "sam@partner.example"}])]
        self.assertTrue(self.run_gate(evs)["wakeAgent"])
        self.assertEqual(self.run_gate(evs, NOW + timedelta(minutes=11)), {"wakeAgent": False})  # starts in 9 min

    def test_quiet_outcome_is_never_reoffered(self):
        evs = [ev("a", "Client call", 50, [ME, {"email": "sam@partner.example"}])]
        g.done(self.run_gate(evs)["context"]["meetings"][0]["key"], "quiet")
        self.assertEqual(self.run_gate(evs, NOW + timedelta(minutes=15)), {"wakeAgent": False})

    def test_window_edges(self):
        evs = [ev("early", "Too soon", 10, [ME, {"email": "x@acme.com"}]),
               ev("late", "Too far", 90, [ME, {"email": "x@acme.com"}])]
        self.assertFalse(self.run_gate(evs)["wakeAgent"])

    def test_skips_declined_solo_all_day_desk_blocks_and_cancelled(self):
        evs = [ev("d", "Budget update", 40, [{**ME, "response": "declined"}, {"email": "g@partner.example"}]),
               ev("s", "Think time", 40),
               ev("cp", "Focus block", 40, [ME]),
               ev("blk", "Prep", 40, [ME, {"email": "x@acme.com"}], description="notes\n\nSet by Proactive Hermes."),
               ev("ad", "Holiday", 40, [ME, {"email": "x@acme.com"}], all_day=True),
               ev("c", "Gone", 40, [ME, {"email": "x@acme.com"}], status="cancelled")]
        self.assertFalse(self.run_gate(evs)["wakeAgent"])

    def test_daily_is_always_included_and_marked(self):
        evs = [ev("ps", "Team Daily", 60, [ME, {"email": "dana@acme.example"}], recurring=True)]
        m = self.run_gate(evs)["context"]["meetings"][0]
        self.assertTrue(m["daily"])
        self.assertEqual(m["external"], [])

    def test_moved_meeting_is_prepped_again(self):
        att = [ME, {"email": "x@acme.com"}]
        self.assertTrue(self.run_gate([ev("m", "Client", 50, att)])["wakeAgent"])
        self.assertTrue(self.run_gate([ev("m", "Client", 70, att)])["wakeAgent"])

    def test_quiet_hours_and_outage(self):
        self.assertEqual(self.run_gate([], datetime(2026, 11, 25, 23, 30, tzinfo=BER))["reason"], "quiet hours")
        out = g.scan(NOW, str(self.tmp / "missing.json"))
        self.assertFalse(out["wakeAgent"])
        self.assertIn("calendar unreadable", out["error"])

    def test_done_records_outcome_and_rejects_unknown(self):
        self.run_gate([ev("a", "Pilot review", 50, [ME, {"email": "g@partner.example"}])])
        key = next(iter(self.offered()))
        self.assertEqual(g.done(key, "said")["status"], "ok")
        self.assertEqual(self.offered()[key]["outcome"], "said")
        self.assertEqual(g.done("nope", "said")["status"], "unknown_key")


class GateSettingsTests(GateBase):
    def test_own_domains_default_to_the_owner_work_email_domains(self):
        evs = [ev("a", "Sync", 50, [ME, {"email": "dana@acme.example"}, {"email": "sam@partner.example"}])]
        (m,) = self.run_gate(evs)["context"]["meetings"]
        self.assertEqual(m["external"], ["partner.example"])  # acme.example is the owner's own domain
        internal = [ev("b", "Sync", 55, [ME, {"email": "dana@ACME.example"}])]
        (m,) = self.run_gate(internal)["context"]["meetings"]
        self.assertEqual(m["external"], [])


class ConfiguredDomainsTests(GateBase):
    SETTINGS = {"meeting_prep": {"own_domains": ["@Partner.example"]}}

    def test_configured_own_domains_replace_the_default(self):
        evs = [ev("a", "Sync", 50, [ME, {"email": "dana@acme.example"}, {"email": "sam@partner.example"}])]
        (m,) = self.run_gate(evs)["context"]["meetings"]
        self.assertEqual(m["external"], ["acme.example"])


class CustomDailyTests(GateBase):
    SETTINGS = {"meeting_prep": {"daily_regex": r"\b(huddle)\b"}}

    def test_custom_daily_regex_replaces_the_default(self):
        solo = [ev("h", "Team Huddle", 50), ev("d", "Daily", 55)]
        (m,) = self.run_gate(solo)["context"]["meetings"]
        self.assertEqual((m["title"], m["daily"]), ("Team Huddle", True))  # "Daily" no longer counts

    def test_a_broken_regex_falls_back_to_the_default(self):
        with patch.object(phconf, "CFG", phconf.merge(phconf.CFG, {"meeting_prep": {"daily_regex": "("}})):
            (m,) = self.run_gate([ev("d", "Daily standup", 50)])["context"]["meetings"]
        self.assertTrue(m["daily"])


class GateStateTests(GateBase):
    SETTINGS = {"meeting_prep": {"max_meetings": 2, "window_min": 20, "window_max": 40}}

    def test_state_file_is_private_and_atomic(self):
        self.run_gate([ev("a", "Pilot review", 30, [ME, {"email": "g@partner.example"}])])
        path = self.tmp / "meeting_prep.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.tmp.glob("*meeting_prep*")), ["meeting_prep.json", "meeting_prep.json.lock"])

    def test_configured_window_and_cap_apply_and_the_rest_wait_for_the_next_run(self):
        att = [ME, {"email": "x@partner.example"}]
        evs = [ev("a", "A", 25, att), ev("b", "B", 30, att), ev("c", "C", 35, att), ev("far", "Far", 50, att)]
        first = self.run_gate(evs)["context"]["meetings"]
        self.assertEqual([m["title"] for m in first], ["A", "B"])
        second = self.run_gate(evs)["context"]["meetings"]
        self.assertEqual([m["title"] for m in second], ["C"])

    def test_meetings_carry_a_fixed_token_key_and_a_checked_email_flag(self):
        att = [ME, {"email": "ok@partner.example"}, {"email": "x$(touch PWNED)@partner.example"}]
        (m,) = self.run_gate([ev("a;`b`", "Review $(touch PWNED)", 30, att)])["context"]["meetings"]
        self.assertRegex(m["key"], r"^[0-9a-f]{16}$")
        self.assertEqual([a["email_ok"] for a in m["attendees"]], [True, False])
        self.assertEqual(m["item_key"], "prep:a-b-:2026-11-25")

    def test_cli_scan_prints_json_last_and_done_roundtrips(self):
        f = self.tmp / "ev.json"
        f.write_text(json.dumps([ev("a", "Pilot review", 30, [ME, {"email": "g@partner.example"}])]))
        out = self.cli(g, "--events-json", str(f), "--now", NOW.isoformat())
        out = json.loads(out.strip().splitlines()[-1])
        key = out["context"]["meetings"][0]["key"]
        done = json.loads(self.cli(g, "done", "--key", key, "--outcome", "quiet"))
        self.assertEqual((done["status"], done["outcome"]), ("ok", "quiet"))


class CalendarReaderTests(OfflineTestCase):
    def fake_api(self, items):
        api = MagicMock()
        api._datetime_with_timezone.side_effect = lambda v: v
        api._gws_binary.return_value = None
        api.build_service.return_value.events.return_value.list.return_value.execute.return_value = {"items": items}
        return api

    def test_calendar_id_comes_from_config_and_defaults_to_primary(self):
        raw = [{"id": "e1", "summary": "Sync", "start": {"dateTime": "2026-11-25T14:00:00+01:00"},
                "end": {"dateTime": "2026-11-25T14:30:00+01:00"}, "recurringEventId": "r",
                "attendees": [{"email": OWNER, "self": True, "responseStatus": "accepted"},
                              {"email": "room@resource.calendar.google.com", "resource": True}]}]
        for config, expected in (({}, "primary"), ({"calendar": {"enabled": True, "id": OWNER}}, OWNER)):
            self.configure(**config)
            api = self.fake_api(raw)
            with patch.object(gcal_full, "_gapi", return_value=api):
                (row,) = gcal_full.events("2026-11-25T00:00:00", "2026-11-26T00:00:00")
            self.assertEqual(api.build_service.return_value.events.return_value.list.call_args.kwargs["calendarId"], expected)
            self.assertEqual((row["recurring"], row["attendees"][0]["self"], row["attendees"][1]["resource"]), (True, True, True))

    def test_missing_google_skill_is_one_clear_error(self):
        with patch.object(phconf, "google_api", return_value=self.tmp / "nope" / "google_api.py"):
            with self.assertRaisesRegex(RuntimeError, "google-workspace skill not found"):
                gcal_full.events("2026-11-25T00:00:00", "2026-11-26T00:00:00")
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(gcal_full.main(["--start", "2026-11-25T00:00:00", "--end", "2026-11-26T00:00:00"]), 1)
            self.assertIn("gcal_full: RuntimeError", err.getvalue())

    def test_runtime_has_no_write_calls(self):
        text = (Path(gcal_full.__file__)).read_text()
        self.assertNotRegex(text, r"\.(insert|update|delete|patch|quickAdd|move)\(")


class JobTests(unittest.TestCase):
    def setUp(self):
        home = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"HERMES_HOME": home}, clear=True))
        self.home = Path(home)
        self.run = FakeCron()
        self.enterContext(patch.object(jobs, "_plugin_ensure", self.run.ensure))
        self.cfg = cli.base_config("custom")
        self.cfg["owner"]["name"] = "Alex"
        self.cfg["timezone"] = "Europe/Berlin"
        self.cfg["features"].update({key: key == "meeting_prep" for key in self.cfg["features"]})

    def created(self):
        return [argv for argv in self.run.calls if argv[2] == "create"]

    def test_standard_feature_is_gated_on_a_calendar(self):
        self.assertTrue(phconf.DEFAULTS["features"]["meeting_prep"])
        job = next(j for j in jobs.JOBS if j["key"] == "meeting_prep")
        self.assertFalse(jobs.enabled(self.cfg, job))  # no calendar
        self.cfg["calendar"]["enabled"] = True
        self.assertTrue(jobs.enabled(self.cfg, job))
        self.cfg["calendar"]["enabled"], self.cfg["mail"]["gmail_api"] = False, True
        self.assertTrue(jobs.enabled(self.cfg, job))
        self.cfg["features"]["meeting_prep"] = False
        self.assertFalse(jobs.enabled(self.cfg, job))

    def test_job_runs_the_gate_every_15_minutes_and_installs_both_scripts(self):
        self.cfg["calendar"]["enabled"] = True
        jobs.apply(self.cfg, run=self.run)
        jid = self.run.job("Proactive · meeting prep")
        argv = self.run.live[jid]
        opt = lambda flag: argv[argv.index(flag) + 1]  # noqa: E731
        self.assertEqual(opt("--script"), "proactive-hermes/meeting_prep_gate.py")
        self.assertEqual(opt("--name"), "Proactive · meeting prep")
        self.assertEqual(self.run.toolsets[jid], ["terminal", "file", "skills"])
        self.assertEqual(self.run.schedule_of(jid), "*/15 7-21 * * *")
        self.assertIn("proactive-hermes:proactive-desk", [argv[i + 1] for i, a in enumerate(argv) if a == "--skill"])
        self.assertNotIn("--no-agent", argv)
        prompt = self.run.prompt_of(jid)
        for text in ("compose --job meeting-prep", "meeting_prep_gate.py done --key <key> --outcome said|quiet",
                     "Never email, message, RSVP", "Final answer = [SILENT]", "380 characters", "`item_key`",
                     str(self.home / "scripts" / "proactive-hermes")):
            self.assertIn(text, prompt)
        self.assertNotIn("{{", prompt)
        self.assertEqual(INLINE.findall(prompt), [])
        for name in ("meeting_prep_gate.py", "gcal_full.py", "phconf.py"):
            self.assertTrue((self.home / "scripts" / "proactive-hermes" / name).is_file(), name)

    def test_evening_brief_adds_tomorrows_meetings_only_with_the_feature_and_a_calendar(self):
        job = next(j for j in jobs.JOBS if j["key"] == "evening_brief")
        without = jobs.prompt_for(job, self.cfg)
        self.assertNotIn("Tomorrow's meetings", without)
        self.cfg["calendar"]["enabled"] = True
        with_calendar = jobs.prompt_for(job, self.cfg)
        for text in ("Tomorrow's meetings", "gcal_full.py --start", "prep tonight: <what>", "At most 2 such items",
                     "ONE action item", "tonight:<event_id>:<YYYY-MM-DD>"):
            self.assertIn(text, with_calendar)
        self.assertNotIn("{{", with_calendar)
        self.assertEqual(INLINE.findall(with_calendar), [])
        self.cfg["features"]["meeting_prep"] = False
        self.assertEqual(jobs.prompt_for(job, self.cfg), without)

    def test_juan_preset_adds_product_sprint_to_the_daily_regex(self):
        preset = json.loads((Path(jobs.__file__).parent / "presets" / "juan.json").read_text())
        self.assertTrue(preset["features"]["meeting_prep"])
        rx = re.compile(preset["meeting_prep"]["daily_regex"], re.I)
        self.assertTrue(rx.search("Product Sprint") and rx.search("Daily") and rx.search("stand-up"))
        self.assertFalse(re.compile(phconf.DEFAULTS["meeting_prep"]["daily_regex"], re.I).search("Product Sprint"))


if __name__ == "__main__":
    unittest.main()
