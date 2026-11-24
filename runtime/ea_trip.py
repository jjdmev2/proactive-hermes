#!/usr/bin/env python3
"""Plan a trip's private calendar block and leave-time reminders.

Times without an offset use the configured timezone. Optional config:
trips.buffer_minutes (10), trips.<kind>.block_before_minutes,
trips.<kind>.block_after_minutes, and trips.<kind>.check_in_minutes.
Use --arrival for a known train or bus arrival instead of the default end.
A booking key identifies one trip; completed effects are not repeated.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sqlite3
import sys
from contextlib import closing, redirect_stdout
from datetime import datetime, time, timedelta, timezone

import ea_reminders as outbox
import phconf

STATE_FILE = "trips.json"
# Calendar minutes before and after departure, followed by check-in minutes.
DEFAULTS = {"flight": (120, 90, 90), "train": (45, 60, 5),
            "bus": (45, 60, 5), "event": (30, 120, 10)}


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=phconf.tz())


def minutes(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("minutes must be a nonnegative integer")
    result = int(value)
    if result < 0:
        raise ValueError("minutes must be a nonnegative integer")
    return result


def make_plan(a) -> dict:
    tz = phconf.tz()
    depart = parse_dt(a.depart).astimezone(timezone.utc)
    if a.arrival and a.kind not in {"train", "bus"}:
        raise ValueError("--arrival is for train or bus trips")
    config = phconf.CFG.get("trips", {})
    kind = config.get(a.kind, {})
    before, after, check_in = (minutes(kind.get(field, default)) for field, default in zip(
        ("block_before_minutes", "block_after_minutes", "check_in_minutes"), DEFAULTS[a.kind]))
    buffer = a.buffer if a.buffer is not None else minutes(config.get("buffer_minutes", 10))
    start = depart - timedelta(minutes=before)
    end = depart + timedelta(minutes=after)
    if a.arrival:
        end = parse_dt(a.arrival).astimezone(timezone.utc)
        if end <= depart:
            raise ValueError("--arrival must be after departure")
    if end <= start:
        raise ValueError("calendar block end must be after start")
    leave = None if a.travel_minutes is None else depart - timedelta(minutes=check_in + a.travel_minutes + buffer)
    return {"block": {"start": start.astimezone(tz).isoformat(), "end": end.astimezone(tz).isoformat()},
            "leave_at": leave.astimezone(tz).isoformat() if leave else None}


def origin_hint() -> dict | None:
    # location.current requests a new fix. Read only completed, retained fixes
    # instead: the bridge currently has no public list/latest-result method.
    if not (outbox.PLUGIN / "bridge.py").is_file():
        return None
    path = outbox.PROFILE / "plugin-data" / outbox.PLUGIN.name / "bridge.db"
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            rows = db.execute("""
                SELECT requests.result, devices.grants FROM requests
                JOIN devices ON devices.id = requests.device
                WHERE operation = 'location.current' AND status = 'succeeded' AND created >= ?
                ORDER BY created DESC
            """, (datetime.now(timezone.utc).timestamp() - 86400,))
            for payload, grants in rows:
                if "location.read" not in json.loads(grants):
                    continue
                data = json.loads(payload).get("data") or {}
                lat, lon = data.get("latitude"), data.get("longitude")
                if type(lat) in (int, float) and type(lon) in (int, float) and -90 <= lat <= 90 and -180 <= lon <= 180:
                    return {k: data[k] for k in ("latitude", "longitude", "timestamp", "accuracy", "horizontal_accuracy") if k in data}
    except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError):
        pass  # Optional bridge, old schema, or no readable result.
    return None


def create_block(key: str, a, window: dict) -> dict:
    if not phconf.CFG["calendar"]["enabled"]:
        return {"status": "disabled"}
    import ea_calendar_block as calendar

    args = argparse.Namespace(key=key, title=a.title, start=window["start"], end=window["end"],
                              notes=f"{a.kind.title()} at {a.place}\nDeparture: {a.depart}",
                              alert=5, force=False, allow_overlap=False)
    output = io.StringIO()
    with redirect_stdout(output):
        try:
            calendar.cmd_create(args)
        except SystemExit:  # cmd_create emits one JSON object, then exits.
            pass
    return json.loads(output.getvalue())


def apply(a, plan: dict) -> dict:
    result = {"block": None, "leave_at": plan["leave_at"], "reminders": [], "skipped": []}

    def skip(action, reason):
        result["skipped"].append({"action": action, "reason": reason})

    if not outbox.auto_allowed("leave_time"):
        skip("leave_time", "not_allowed")
        return result

    tz = phconf.tz()
    now = datetime.now(timezone.utc)
    depart = parse_dt(a.depart).astimezone(tz)
    evening = datetime.combine(depart.date() - timedelta(days=1), time(20), tz)
    leave = parse_dt(plan["leave_at"])
    # Hash the input key so child keys stay bounded and cannot collide by suffix.
    prefix = "trip:" + hashlib.sha256(a.key.encode()).hexdigest()
    with outbox.state_lock(STATE_FILE):
        trips = outbox.load(STATE_FILE, {})
        record = trips.get(a.key, {"plan": plan, "title": a.title, "place": a.place})
        if (record["plan"], record["title"], record["place"]) != (plan, a.title, a.place):
            skip("leave_time", "key_already_used_for_different_plan")
            return result

        def remember(action, receipt):
            record[action] = receipt
            trips[a.key] = record
            outbox.save(STATE_FILE, trips)

        if not outbox.auto_allowed("calendar_blocks"):
            skip("calendar_blocks", "not_allowed")
        elif "block" in record:
            result["block"] = {**record["block"], "status": "exists"}
        else:
            try:
                block = create_block("ea:" + prefix, a, plan["block"])
            except Exception as exc:
                block = {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:300]}
            result["block"] = block
            if block["status"] in {"created", "updated"}:
                remember("block", block)
            else:
                skip("calendar_blocks", block["status"])

        if not outbox.auto_allowed("reminders"):
            skip("reminders", "not_allowed")
            return result
        desired = [("leave_reminder", leave, f"Leave now for {a.place} ({a.title})")]
        if depart.time() < time(10):
            desired.append(("evening_reminder", evening, f"Tomorrow: {a.title} at {depart:%H:%M} ({a.place})"))
        for action, due, title in desired:
            if action in record:
                result["reminders"].append({**record[action], "status": "exists"})
                continue
            if due.astimezone(timezone.utc) <= now:
                skip(action, "past_due")
                continue
            try:
                receipt = outbox.add(f"{prefix}:{action}", title, due=due.isoformat(), priority=None,
                                     required_authority=("leave_time", "reminders"))
            except Exception as exc:
                receipt = {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:300]}
            receipt = {**receipt, "action": action, "due": due.isoformat()}
            result["reminders"].append(receipt)
            if receipt["status"] == "queued" or receipt.get("outbox_status") in {"queued", "landed"}:
                remember(action, receipt)
            else:
                skip(action, receipt.get("outbox_status", receipt["status"]))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for cmd in ("plan", "apply"):
        p = sub.add_parser(cmd)
        p.add_argument("--key", required=True)
        p.add_argument("--title", required=True)
        p.add_argument("--depart", required=True)
        p.add_argument("--place", required=True)
        p.add_argument("--kind", choices=DEFAULTS, default="event")
        p.add_argument("--arrival")
        p.add_argument("--travel-minutes", type=minutes)
        p.add_argument("--buffer", type=minutes)
    a = parser.parse_args()
    try:
        plan = make_plan(a)
        if a.travel_minutes is None:
            result = {**plan, "needs": ["travel_minutes"]}
            if (a.cmd == "plan" or outbox.auto_allowed("leave_time")) and (origin := origin_hint()):
                result["origin_hint"] = origin
        else:
            result = apply(a, plan) if a.cmd == "apply" else plan
    except (ValueError, OSError, OverflowError, TypeError) as exc:
        result = {"status": "error", "error": str(exc)[:300]}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
