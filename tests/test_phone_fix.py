"""Time-critical reminders and alarms also become calendar alerts the phone fires by itself (offline fakes)."""

import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

try:  # `discover -s tests -t .` imports the package; plain `discover -s tests` does not.
    from .test_desk import FakeStore, OfflineTestCase, phconf, reminders
except ImportError:
    from test_desk import FakeStore, OfflineTestCase, phconf, reminders
import ea_close  # noqa: E402  (test_desk puts runtime/ on sys.path)

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"


class PhoneBase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.configure(reminders={"enabled": True}, calendar={"enabled": True},
                       authority={"auto": ["reminders", "alarms", "calendar_blocks"]})
        self.enterContext(patch.object(reminders, "PLUGIN", self.tmp))
        (self.tmp / "bridge.py").touch()
        self.freeze_datetime(reminders)
        self.store = FakeStore()
        self.enterContext(patch.object(reminders, "bridge", return_value=(None, self.store)))
        self.enterContext(patch.object(reminders, "wake"))
        self.calls = []

        def fake_cli(args, texts=None):
            self.calls.append((args, texts))
            if args[0] == "delete":
                return {"status": "deleted", "key": args[-1], "id": "ev1"}
            return {"status": "created", "key": args[2], "id": f"ev{len(self.calls)}"}
        self.enterContext(patch.object(reminders, "calendar_cli", side_effect=fake_cli))

    def at(self, **delta):
        return (self.clock + timedelta(**delta)).isoformat()

    def add(self, *extra, due=None, priority="medium", key="k1"):
        args = ["add", "--key", key, "--title", "Pay the invoice", "--priority", priority, *extra]
        if due:
            args += ["--due", due]
        return json.loads(self.cli(reminders, *args))

    def alerts(self):
        return [a for a, _ in self.calls if a[0] == "alert"]


class AddTests(PhoneBase):
    def test_time_critical_add_creates_alert_and_still_queues_reminder(self):
        out = self.add(due=self.at(hours=3))
        (args,) = self.alerts()
        self.assertEqual(args[:3], ["alert", "--key", "ea:alert:k1"])
        self.assertEqual(self.calls[0][1]["title"], "Pay the invoice")  # text goes through a file, never argv
        self.assertEqual(out["status"], "queued")
        self.assertEqual(out["calendar_alert"]["id"], "ev1")
        self.assertEqual(self.read_json(self.tmp / reminders.OUTBOX)["k1"]["calendar_alert"]["id"], "ev1")
        self.assertEqual(self.read_json(self.tmp / reminders.LEDGER)["k1"]["calendar_alert_id"], "ev1")
        self.assertEqual([r[0] for r in self.store.requests], ["reminders.create"])

    def test_high_priority_far_due_creates_alert(self):
        self.add(due=self.at(days=3), priority="high")
        self.assertEqual(len(self.alerts()), 1)

    def test_non_critical_gets_no_alert(self):
        out = self.add(due=self.at(days=2))
        self.assertEqual(self.calls, [])
        self.assertNotIn("calendar_alert", out)

    def test_opt_out_and_feature_off(self):
        self.add("--no-calendar-alert", due=self.at(hours=1), priority="high")
        self.configure(reminders={"enabled": True}, calendar={"enabled": True},
                       features={"calendar_alerts": False})
        self.add(due=self.at(hours=1), key="k2")
        self.configure(reminders={"enabled": True}, calendar={"enabled": False})
        self.add(due=self.at(hours=1), key="k3")
        self.assertEqual(self.calls, [])

    def test_past_due_gets_no_alert(self):
        self.add(due=self.at(minutes=-10), priority="high")
        self.assertEqual(self.calls, [])

    def test_expired_request_ensures_alert_once_when_due_is_far_enough(self):
        rid = self.add(due=self.at(days=2))["request_id"]
        self.store.results[rid] = {"status": "expired"}
        self.cli(reminders, "tick")
        self.assertEqual(len(self.alerts()), 1)
        self.assertEqual(len(self.store.requests), 2)  # the reminder itself is still retried
        self.store.results["request-2"] = {"status": "expired"}
        self.cli(reminders, "tick")
        self.assertEqual(len(self.alerts()), 1)

    def test_expired_request_too_close_to_due_makes_no_alert(self):
        rid = self.add("--no-calendar-alert", due=self.at(minutes=1))["request_id"]
        box = self.read_json(self.tmp / reminders.OUTBOX)
        box["k1"].pop("no_calendar_alert")
        (self.tmp / reminders.OUTBOX).write_text(json.dumps(box))
        self.store.results[rid] = {"status": "expired"}
        self.cli(reminders, "tick")
        self.assertEqual(self.calls, [])


class AcceptedFirstTests(PhoneBase):
    """The calendar alert exists only for a request the outbox accepted, and no calendar call holds its lock."""

    def test_refused_reminder_makes_no_alert(self):
        self.configure(reminders={"enabled": True, "per_day": 1}, calendar={"enabled": True},
                       authority={"auto": ["reminders", "alarms", "calendar_blocks"]})
        self.assertEqual(self.add(due=self.at(hours=2), key="k1")["status"], "queued")
        out = self.add(due=self.at(hours=2), key="k2")
        self.assertEqual(out["status"], "refused")
        self.assertNotIn("calendar_alert", out)
        self.assertEqual([a[2] for a in self.alerts()], ["ea:alert:k1"])
        self.assertEqual(self.add(due=self.at(hours=2), key="k1")["status"], "exists")   # duplicate key: no 2nd
        self.assertEqual(len(self.alerts()), 1)
        self.assertNotIn("k2", self.read_json(self.tmp / reminders.OUTBOX))

    def test_alarm_on_a_reminder_key_is_refused_without_alert(self):
        self.add(due=self.at(days=3), key="k1")                       # not critical: no alert
        out = json.loads(self.cli(reminders, "alarm", "--key", "k1", "--at", self.at(minutes=40), "--label", "Go"))
        self.assertEqual(out["status"], "refused")
        self.assertEqual(self.alerts(), [])

    def test_tick_makes_alerts_outside_the_outbox_lock_then_records_them(self):
        import fcntl
        rid = self.add(due=self.at(days=2))["request_id"]
        self.store.results[rid] = {"status": "expired"}
        free = []
        inner = reminders.calendar_cli.side_effect

        def probe(args, texts=None):
            with (phconf.state_dir() / (reminders.OUTBOX + ".lock")).open("a") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    free.append(True)
                    fcntl.flock(fh, fcntl.LOCK_UN)
                except BlockingIOError:
                    free.append(False)
            return inner(args, texts)

        with patch.object(reminders, "calendar_cli", side_effect=probe):
            self.cli(reminders, "tick")
        self.assertEqual(free, [True])
        box = self.read_json(self.tmp / reminders.OUTBOX)
        self.assertEqual(box["k1"]["calendar_alert"]["why"], "bridge expired")
        self.assertEqual(box["k1"]["attempts"], 2)                     # the retry was recorded too


class CalendarAlertsOnTests(OfflineTestCase):
    def test_needs_calendar_blocks_auto(self):
        base = {"calendar": {"enabled": True}, "features": {"calendar_alerts": True}}
        auto = {**base, "authority": {"auto": ["calendar_blocks"]}}
        ask = {**base, "authority": {"auto": [], "ask_first": ["calendar_blocks"]}}
        never = {**base, "authority": {"auto": ["calendar_blocks"], "never": ["calendar_blocks"]}}
        self.assertTrue(phconf.calendar_alerts_on(cfg=auto))
        self.assertFalse(phconf.calendar_alerts_on(cfg=ask))
        self.assertFalse(phconf.calendar_alerts_on(cfg=base))         # unlisted = ask
        self.assertFalse(phconf.calendar_alerts_on(cfg=never))
        self.assertFalse(phconf.calendar_alerts_on(cfg={**auto, "features": {"calendar_alerts": False}}))
        self.assertFalse(phconf.calendar_alerts_on(cfg={**auto, "calendar": {"enabled": False}}))


class AlarmTests(PhoneBase):
    def alarm(self, at, key="flight"):
        return json.loads(self.cli(reminders, "alarm", "--key", key, "--at", at, "--label", "Leave for the airport"))

    def test_alarm_queues_bridge_alarm_and_calendar_alert(self):
        out = self.alarm(self.at(minutes=40))
        self.assertEqual(out["status"], "queued")
        self.assertEqual([r[0] for r in self.store.requests], ["alarms.create"])
        self.assertEqual(self.alerts()[0][2], "ea:alert:flight")
        self.assertEqual(self.calls[0][1]["title"], "Leave for the airport")

    def test_already_passed_is_logged_and_never_retried(self):
        rid = self.alarm(self.at(minutes=40))["request_id"]
        self.store.results[rid] = {"status": "failed", "error": "The alarm time has already passed"}
        self.cli(reminders, "tick")
        self.cli(reminders, "tick")
        self.assertEqual(len(self.store.requests), 1)
        self.assertEqual(len(self.alerts()), 1)  # made at queue time, not again
        self.assertEqual(self.read_json(self.tmp / reminders.OUTBOX)["flight"]["status"], "failed")

    def test_alarm_in_the_past_is_refused(self):
        self.assertEqual(self.alarm(self.at(minutes=-1))["status"], "refused")
        self.assertEqual((self.store.requests, self.calls), ([], []))


class CloseTests(PhoneBase):
    def setUp(self):
        super().setUp()
        self.freeze_datetime(ea_close)
        (self.tmp / "promises.json").write_text(json.dumps({"p1": {"status": "open", "what": "Pay"}}))

    def test_close_deletes_recorded_alert(self):
        self.add(due=self.at(hours=2), key="p1")
        out = ea_close.close("p1", "owner: paid")
        self.assertIn("calendar alert", out["closed_in"])
        self.assertEqual(self.calls[-1][0], ["delete", "--key", "ea:alert:p1"])

    def test_close_without_alert_makes_no_calendar_call_and_unknown_key_is_untouched(self):
        out = ea_close.close("p1", "owner: paid")
        self.assertNotIn("calendar_alert", out)
        self.assertEqual(ea_close.close("typo", "x"), {"status": "unknown_key"})
        self.assertEqual(self.calls, [])

    def test_alert_seen_only_in_the_calendar_log_is_deleted_once(self):
        log = self.tmp / "actions_log.jsonl"
        log.write_text(json.dumps({"kind": "calendar_alert", "key": "ea:alert:p1", "id": "e1"}) + "\n")
        self.assertTrue(reminders.alert_recorded("p1"))
        with log.open("a") as fh:
            fh.write(json.dumps({"kind": "calendar_block_deleted", "key": "ea:alert:p1", "id": "e1"}) + "\n")
        self.assertFalse(reminders.alert_recorded("p1"))


class FakeEvents:
    def __init__(self):
        self.db, self.n = {}, 0

    def list(self, **kw):
        want = kw["privateExtendedProperty"].split("=", 1)[1]
        items = [e for e in self.db.values() if want in e["extendedProperties"]["private"].values()]
        return Mock(execute=lambda: {"items": items})

    def insert(self, calendarId, body):
        self.n += 1
        ev = {**body, "id": f"e{self.n}", "organizer": {"email": "me"}}
        self.db[ev["id"]] = ev
        return Mock(execute=lambda: ev)

    def patch(self, calendarId, eventId, body):
        self.db[eventId].update(body)
        return Mock(execute=lambda: self.db[eventId])

    def get(self, calendarId, eventId):
        return Mock(execute=lambda: self.db[eventId])


class CalendarAlertTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.configure(calendar={"enabled": True}, timezone="Europe/Berlin")
        import ea_calendar_block as calendar
        self.cal = calendar
        self.enterContext(patch.object(calendar, "LOG", self.tmp / "actions_log.jsonl"))
        self.enterContext(patch.object(calendar, "TZ", phconf.tz()))
        self.ev = FakeEvents()
        self.svc = Mock(events=lambda: self.ev)

    def test_alert_is_private_popup_at_zero_short_idempotent_and_uncapped(self):
        at = datetime(2030, 1, 2, 15, 40, tzinfo=phconf.tz())
        res, code = self.cal.make_alert(self.svc, "ea:alert:x", "A" * 200, at, "note")
        self.assertEqual((res["status"], code), ("created", 0))
        e = self.ev.db[res["id"]]
        self.assertEqual(e["reminders"], {"useDefault": False, "overrides": [{"method": "popup", "minutes": 0}]})
        self.assertEqual((e["visibility"], e["transparency"]), ("private", "transparent"))
        self.assertLessEqual(len(e["summary"]), self.cal.ALERT_TITLE_CHARS)
        self.assertEqual(datetime.fromisoformat(e["end"]["dateTime"]) - at, timedelta(minutes=5))
        res2, _ = self.cal.make_alert(self.svc, "ea:alert:x", "Short", at)
        self.assertEqual((res2["status"], res2["id"], len(self.ev.db)), ("updated", res["id"], 1))
        kinds = {json.loads(x)["kind"] for x in self.cal.LOG.read_text().splitlines()}
        self.assertEqual(kinds, {"calendar_alert"})  # never counted as a calendar_block

    def test_key_must_start_with_ea(self):
        self.assertEqual(self.cal.make_alert(self.svc, "x", "T", datetime(2030, 1, 1, tzinfo=timezone.utc))[1], 2)


class PhoneHealthTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.configure(reminders={"enabled": True}, timezone="Europe/Helsinki")
        spec = importlib.util.spec_from_file_location("test_ea_health_phone", RUNTIME / "ea_health.py")
        self.health = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.health)
        self.db = self.tmp / "bridge.db"
        self.enterContext(patch.multiple(self.health, STATE_DIR=self.tmp, BRIDGE_DB=self.db, faults={}))

    def clock_at(self, hour):
        clock = datetime(2030, 4, 1, hour, tzinfo=phconf.tz()).astimezone(timezone.utc)
        return patch.object(self.health, "now", return_value=clock), clock

    def seen(self, ts):
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE devices (id TEXT, seen REAL)")
        con.execute("INSERT INTO devices VALUES ('d', ?)", (ts.timestamp(),))
        con.commit()
        con.close()

    def test_stale_phone_in_daytime_raises_one_owner_item(self):
        p, clock = self.clock_at(15)
        self.seen(clock - timedelta(minutes=95))
        with p:
            self.health.check_phone_sync()
            self.health.check_phone_sync()
        (cls, text), = self.health.faults.values()
        self.assertEqual((cls, text), ("owner", "Phone has not synced for 95 min: open Hermes once; "
                                                "check Background App Refresh"))

    def test_fresh_phone_or_night_is_quiet(self):
        p, clock = self.clock_at(15)
        self.seen(clock - timedelta(minutes=30))
        with p:
            self.health.check_phone_sync()
        p, _ = self.clock_at(23)
        with p:
            self.health.check_phone_sync()
        self.assertEqual(self.health.faults, {})

    def test_outbox_fallback_when_no_bridge_db(self):
        p, clock = self.clock_at(10)
        (self.tmp / "reminder_outbox.json").write_text(json.dumps(
            {"k": {"status": "landed", "landed": (clock - timedelta(minutes=70)).isoformat()}}))
        with p:
            self.health.check_phone_sync()
        self.assertEqual(len(self.health.faults), 1)
