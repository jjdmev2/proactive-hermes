"""Hermetic trip CLI tests using the real outbox and calendar-create logic."""

from contextlib import closing
from datetime import datetime
import json
import sqlite3
from unittest.mock import patch

from .test_desk import FakeStore, OfflineTestCase, phconf, reminders

import ea_trip as trip


class FakeCalendar:
    def __init__(self):
        self.fake_events = {}
        self.inserts = []
        self.readbacks = []
        self.patches = []

    def events(self):
        return self

    def execute(self):
        return self.response

    def list(self, **query):
        values = list(self.fake_events.values())
        if tagged := query.get("privateExtendedProperty"):
            field, _, key = tagged.partition("=")
            values = [e for e in values if e["extendedProperties"]["private"].get(field) == key]
        self.response = {"items": values}
        return self

    def insert(self, **request):
        self.inserts.append(request)
        event_id = f"event-{len(self.inserts)}"
        self.response = self.fake_events[event_id] = {"id": event_id, **request["body"]}
        return self

    def patch(self, **request):
        self.patches.append(request)
        raise AssertionError("A repeated trip should not patch its existing block")

    def get(self, **request):
        self.readbacks.append(request)
        self.response = self.fake_events[request["eventId"]]
        return self


class TripTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.clock = datetime(2030, 4, 1, 11, tzinfo=phconf.tz())
        self.settings = {"timezone": "Europe/Berlin", "owner": {"name": "Alex"},
                         "calendar": {"enabled": True, "id": "alex@acme.example"},
                         "reminders": {"enabled": True},
                         "authority": {"auto": ["leave_time", "calendar_blocks", "reminders"]}}
        self.configure(**self.settings)
        import ea_calendar_block as calendar
        self.calendar = FakeCalendar()
        self.calendar_service = self.enterContext(patch.object(calendar, "service", return_value=self.calendar))
        self.enterContext(patch.object(calendar, "TZ", phconf.tz()))
        self.enterContext(patch.object(calendar, "CAL", "alex@acme.example"))
        self.enterContext(patch.object(calendar, "LOG", self.tmp / "actions_log.jsonl"))
        self.freeze_datetime(trip)
        self.freeze_datetime(reminders)
        self.store = FakeStore()
        real_bridge = reminders.bridge   # read-only reads hit the fake bridge.db below; writes go to the fake store
        self.enterContext(patch.object(reminders, "bridge", side_effect=lambda read_only=False:
                                       real_bridge(read_only=True) if read_only else (None, self.store)))
        self.enterContext(patch.object(reminders, "PLUGIN", self.tmp / "plugins" / "hermes-mobile-device-access"))
        reminders.PLUGIN.mkdir(parents=True)
        (reminders.PLUGIN / "bridge.py").touch()
        self.enterContext(patch.object(reminders, "PROFILE", self.tmp))

    def invoke(self, cmd, kind="train", depart="2030-04-02T07:12:00+02:00", *extra, key="booking-1"):
        return json.loads(self.cli(trip, cmd, "--key", key, "--title", "Alex to Berlin",
                                   "--depart", depart, "--place", "Berlin Hbf", "--kind", kind, *extra))

    def test_plan_windows_leave_time_and_buffer(self):
        cases = {
            "flight": ("10:00", "13:30", "09:55"),
            "train": ("11:15", "13:00", "11:20"),
            "bus": ("11:15", "13:00", "11:20"),
            "event": ("11:30", "14:00", "11:15"),
        }
        for kind, (start, end, leave) in cases.items():
            with self.subTest(kind=kind):
                result = self.invoke("plan", kind, "2030-04-02T12:00:00+02:00", "--travel-minutes", "25")
                stamp = lambda value: f"2030-04-02T{value}:00+02:00"
                self.assertEqual(result["block"], {"start": stamp(start), "end": stamp(end)})
                self.assertEqual(result["leave_at"], stamp(leave))
        self.calendar_service.assert_not_called()
        self.assertEqual(self.store.requests, [])

    def test_train_arrival_and_config_overrides(self):
        self.configure(**self.settings, trips={"train": {"block_before_minutes": 60, "check_in_minutes": 15}})
        result = self.invoke("plan", "train", "2030-04-02T12:00:00+02:00", "--travel-minutes", "25",
                             "--buffer", "20", "--arrival", "2030-04-02T16:00:00+02:00")
        self.assertEqual(result["block"], {"start": "2030-04-02T11:00:00+02:00", "end": "2030-04-02T16:00:00+02:00"})
        self.assertEqual(result["leave_at"], "2030-04-02T11:00:00+02:00")

    def test_missing_travel_never_requests_live_location(self):
        self.assertEqual(self.invoke("plan")["needs"], ["travel_minutes"])
        self.assertNotIn("origin_hint", self.invoke("plan"))
        self.assertEqual(self.invoke("apply")["needs"], ["travel_minutes"])
        self.calendar_service.assert_not_called()
        self.assertEqual(self.store.requests, [])

    def test_latest_authorized_cached_location_is_an_optional_origin_hint(self):
        db_path = self.tmp / "plugin-data" / reminders.PLUGIN.name / "bridge.db"
        db_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("CREATE TABLE devices (id TEXT, grants TEXT)")
            db.execute("CREATE TABLE requests (device TEXT, operation TEXT, status TEXT, created REAL, result TEXT)")
            db.executemany("INSERT INTO devices VALUES (?, ?)", [("phone", '["location.read"]'), ("revoked", "[]")])
            for device, created, latitude in [("phone", 10, 52.5), ("phone", 20, 52.52), ("revoked", 30, 1.0)]:
                data = {"latitude": latitude, "longitude": 13.4, "timestamp": "2030-04-01T11:00:00+02:00"}
                db.execute("INSERT INTO requests VALUES (?, 'location.current', 'succeeded', ?, ?)",
                           (device, self.clock.timestamp() - 100 + created,
                            json.dumps({"status": "succeeded", "data": data})))
        before = db_path.read_bytes()
        hint = self.invoke("plan")["origin_hint"]
        self.assertEqual((hint["latitude"], hint["longitude"]), (52.52, 13.4))
        self.assertEqual(db_path.read_bytes(), before)
        self.assertEqual(self.store.requests, [])

    def test_apply_is_idempotent_and_morning_trip_gets_two_reminders(self):
        result = self.invoke("apply", "train", "2030-04-02T07:12:00+02:00", "--travel-minutes", "25")
        self.assertEqual(result["block"]["status"], "created")
        self.assertEqual(result["leave_at"], "2030-04-02T06:32:00+02:00")
        self.assertEqual(result["skipped"], [])
        self.assertEqual([r["status"] for r in result["reminders"]], ["queued", "queued"])
        [insert] = self.calendar.inserts
        self.assertEqual(insert["calendarId"], "alex@acme.example")
        self.assertEqual(insert["body"]["visibility"], "private")
        self.assertNotIn("attendees", insert["body"])
        self.assertEqual(self.calendar.readbacks, [{"calendarId": "alex@acme.example", "eventId": "event-1"}])
        leave, evening = [request[1] for request in self.store.requests]
        self.assertEqual(leave["due"], result["leave_at"])
        self.assertEqual(leave["title"], "Leave now for Berlin Hbf (Alex to Berlin)")
        self.assertEqual(evening["due"], "2030-04-01T20:00:00+02:00")
        again = self.invoke("apply", "train", "2030-04-02T07:12:00+02:00", "--travel-minutes", "25")
        self.assertEqual(again["block"]["status"], "exists")
        self.assertEqual([r["status"] for r in again["reminders"]], ["exists", "exists"])
        self.calendar_service.assert_called_once()
        self.assertEqual(len(self.store.requests), 2)

    def test_authority_checks_leave_time_and_each_side_effect_separately(self):
        for auto in ([], ["calendar_blocks", "reminders"]):
            self.configure(**{**self.settings, "authority": {"auto": auto}})
            result = self.invoke("apply", "train", "2030-04-02T07:12:00+02:00", "--travel-minutes", "25")
            self.assertEqual(result["skipped"], [{"action": "leave_time", "reason": "not_allowed"}])
        self.calendar_service.assert_not_called()
        self.assertEqual(self.store.requests, [])
        self.configure(**{**self.settings, "authority": {"auto": ["leave_time", "calendar_blocks"]}})
        result = self.invoke("apply", "train", "2030-04-02T07:12:00+02:00", "--travel-minutes", "25")
        self.assertEqual(result["skipped"], [{"action": "reminders", "reason": "not_allowed"}])
        self.assertEqual(len(self.calendar.inserts), 1)
        self.assertEqual(self.store.requests, [])
        self.configure(**{**self.settings, "authority": {"auto": ["leave_time", "reminders"]}})
        result = self.invoke("apply", "train", "2030-04-02T07:12:00+02:00", "--travel-minutes", "25")
        self.assertEqual(result["skipped"], [{"action": "calendar_blocks", "reason": "not_allowed"}])
        self.assertEqual(len(self.store.requests), 2)
        self.calendar_service.assert_called_once()

    def test_outbox_does_not_retry_trip_reminders_after_authority_is_revoked(self):
        self.invoke("apply", "train", "2030-04-02T12:00:00+02:00", "--travel-minutes", "25")
        self.assertEqual(len(self.store.requests), 1)
        self.store.results["request-1"] = {"status": "expired"}
        self.configure(**{**self.settings, "authority": {"auto": ["reminders"]}})
        self.cli(reminders, "tick")
        self.assertEqual(len(self.store.requests), 1)

    def test_evening_rule_uses_local_clock_and_omits_past_reminders(self):
        # 08:00 UTC is exactly 10:00 in Berlin, so no previous-evening reminder.
        self.invoke("apply", "train", "2030-04-02T08:00:00+00:00", "--travel-minutes", "25")
        self.assertEqual(len(self.store.requests), 1)
        self.clock = datetime.fromisoformat("2030-04-02T21:00:00+02:00")
        result = self.invoke("apply", "train", "2030-04-03T07:12:00+02:00", "--travel-minutes", "25",
                             key="booking-2")
        self.assertEqual(result["skipped"], [{"action": "evening_reminder", "reason": "past_due"}])
        self.assertEqual(len(self.store.requests), 2)
