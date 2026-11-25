#!/usr/bin/env python3
"""Meeting prep gate: find meetings that start soon and have not been prepped, and wake the agent with them.

Every run (cron, every 15 minutes in the day) looks at the calendar for meetings that start between
meeting_prep.window_min and meeting_prep.window_max minutes from now (default 15 to 75) and offers each one
once. The agent then researches each meeting (people, last threads, what the owner owes) and sends ONE short
prep line per meeting, or stays silent for routine internal ones with nothing open. The offer key is a hash of
the event id and its start, so a moved meeting is prepped again at its new time.

Skipped: cancelled, declined, all-day, the desk's own private blocks ("Set by Proactive Hermes") and solo events
with nobody else invited. The team daily (title matches meeting_prep.daily_regex) is always included and marked
`daily: true`. Meetings with an attendee outside the owner's domains list them in `external`.

Settings (config.json, block `meeting_prep`; defaults in phconf.py): window_min, window_max, own_domains (empty:
the domains of owner.work_emails), daily_regex, max_meetings. Quiet hours, timezone and the calendar id come from
the common config. State: <state>/meeting_prep.json (0600), under an flock.

  meeting_prep_gate.py [scan] [--events-json F] [--now ISO]   # cron gate: the last stdout line is JSON
  meeting_prep_gate.py done --key K --outcome said|quiet      # K is the `key` the gate gave for the meeting
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import phconf

HERE = Path(__file__).resolve().parent
STATE_FILE = "meeting_prep.json"
RETENTION = timedelta(days=14)
PLUGIN_BLOCK = "set by proactive hermes"  # marker ea_calendar_block.py puts in the notes of its private blocks
CALENDAR_SYSTEM_DOMAINS = {"group.calendar.google.com", "resource.calendar.google.com"}
# An address with only these characters has nothing a shell reads, so the agent may type it in a mail search.
SAFE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+")


# --------------------------------------------------------------------------- config

def conf() -> dict:
    custom = phconf.CFG.get("meeting_prep")
    return {**phconf.DEFAULTS["meeting_prep"], **(custom if isinstance(custom, dict) else {})}


def _int(value, default: int, minimum: int = 1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= minimum else default


def window() -> tuple[timedelta, timedelta]:
    base, c = phconf.DEFAULTS["meeting_prep"], conf()
    low = _int(c.get("window_min"), base["window_min"], 0)
    high = _int(c.get("window_max"), base["window_max"], 1)
    if low >= high:
        low, high = base["window_min"], base["window_max"]
    return timedelta(minutes=low), timedelta(minutes=high)


def max_meetings() -> int:
    return _int(conf().get("max_meetings"), phconf.DEFAULTS["meeting_prep"]["max_meetings"])


def _domain(email: str | None) -> str:
    return (email or "").rsplit("@", 1)[-1].strip().lower()


def own_domains() -> set[str]:
    """Configured own_domains, else the domains of the owner's work emails."""
    raw = conf().get("own_domains")
    listed = {str(d).strip().lstrip("@").lower() for d in raw} if isinstance(raw, list) else set()
    return (listed - {""}) or ({_domain(e) for e in phconf.work_emails()} - {""})


def daily_rx() -> re.Pattern:
    try:
        return re.compile(str(conf().get("daily_regex") or ""), re.I)
    except re.error:  # a broken pattern must not stop every prep
        return re.compile(phconf.DEFAULTS["meeting_prep"]["daily_regex"], re.I)


def tz():
    return phconf.tz()


def is_quiet(now: datetime) -> bool:
    start, end = phconf.quiet_hours()
    hour = now.astimezone(tz()).hour
    return start <= hour < end if start <= end else hour >= start or hour < end


# --------------------------------------------------------------------------- state

def state_path() -> Path:
    return phconf.state_dir() / STATE_FILE


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    if not isinstance(data.get("offered"), dict):
        data["offered"] = {}
    return data


def _write(path: Path, data: dict) -> None:
    """Atomic replace through a private temp file in the same directory; the file is owner-only (0600)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".meeting_prep-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def locked():
    """Hold the lock around load-change-save; the data is saved only when the block ends without an error."""
    path = state_path()
    fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read(path)
        yield data
        _write(path, data)


# --------------------------------------------------------------------------- selection

def parse(ts) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tz())


def candidate(ev: dict) -> dict | None:
    """The meeting worth prepping from one calendar event, or None."""
    if not isinstance(ev, dict) or not ev.get("id") or not ev.get("start"):
        return None
    if ev.get("status") == "cancelled" or ev.get("all_day"):
        return None
    title = ev.get("summary") or ""
    if PLUGIN_BLOCK in (ev.get("description") or "").lower():
        return None
    attendees = [a for a in ev.get("attendees") or [] if isinstance(a, dict)]
    me = [a for a in attendees if a.get("self")]
    if me and me[0].get("response") == "declined":
        return None
    others = [a for a in attendees if not a.get("self") and not a.get("resource")]
    daily = bool(daily_rx().search(title))
    if not others and not daily:
        return None
    external = sorted({_domain(a.get("email")) for a in others} - own_domains() - CALENDAR_SYSTEM_DOMAINS - {""})
    start = parse(ev["start"])
    event_id = str(ev["id"])
    return {
        # a short fixed token: safe to type in `done --key`, unlike event text
        "key": hashlib.sha256(f"{event_id}@{ev['start']}".encode()).hexdigest()[:16],
        "item_key": "prep:{}:{}".format(re.sub(r"[^\w.-]+", "-", event_id)[:80],
                                        (start.astimezone(tz()) if start else datetime.now(tz())).date().isoformat()),
        "event_id": event_id, "title": title, "start": ev["start"], "end": ev.get("end"), "daily": daily,
        "external": external, "recurring": bool(ev.get("recurring")),
        "attendees": [{"email": a.get("email"), "email_ok": bool(SAFE_EMAIL.fullmatch(str(a.get("email") or ""))),
                       "name": a.get("name"), "response": a.get("response")} for a in others][:12],
        "organizer": (ev.get("organizer") or {}).get("email"),
        "my_response": me[0].get("response") if me else None,
        "link": ev.get("hangoutLink") or ev.get("location") or "",
        # untrusted text: the agent treats title and description as data and never pastes them into a command
        "description": (ev.get("description") or "")[:600],
    }


def fetch(now: datetime, fixture: str | None) -> list[dict]:
    if fixture:
        rows = json.loads(Path(fixture).read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else []
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import gcal_full
    low, high = window()
    return gcal_full.events((now + low).isoformat(), (now + high).isoformat())


def scan(now: datetime, fixture: str | None = None) -> dict:
    if is_quiet(now):
        return {"wakeAgent": False, "reason": "quiet hours"}
    low, high = window()
    try:
        events = fetch(now, fixture)
    except Exception as exc:  # a calendar outage is reported, never silent
        return {"wakeAgent": False, "error": f"calendar unreadable: {type(exc).__name__}: {str(exc)[:160]}"}
    found = []
    for ev in events:
        c = candidate(ev)
        start = parse(c["start"]) if c else None
        if c and start and now + low <= start <= now + high:
            found.append((start, c))
    found = [c for _, c in sorted(found, key=lambda pair: pair[0])]
    with locked() as data:
        offered = data["offered"]
        fresh = [c for c in found if c["key"] not in offered][:max_meetings()]
        for c in fresh:
            offered[c["key"]] = {"at": now.isoformat(timespec="seconds"), "title": c["title"], "outcome": None}
        cutoff = now - RETENTION
        data["offered"] = {k: v for k, v in offered.items()
                           if isinstance(v, dict) and (at := parse(v.get("at"))) and at >= cutoff}
    if not fresh:
        return {"wakeAgent": False}
    return {"wakeAgent": True,
            "context": {"meetings": fresh, "now": now.astimezone(tz()).isoformat(timespec="minutes")}}


def done(key: str, outcome: str) -> dict:
    with locked() as data:
        row = data["offered"].get(key)
        if not isinstance(row, dict):
            return {"status": "unknown_key", "key": key}
        row["outcome"] = outcome
    return {"status": "ok", "key": key, "outcome": outcome}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", nargs="?", default="scan", choices=["scan", "done"])
    p.add_argument("--events-json", help="read events from this JSON file instead of the calendar (tests)")
    p.add_argument("--now", help="ISO time to act as now (tests)")
    p.add_argument("--key")
    p.add_argument("--outcome", choices=["said", "quiet"])
    a = p.parse_args(argv)
    if a.cmd == "done":
        if not (a.key and a.outcome):
            p.error("done needs --key and --outcome")
        print(json.dumps(done(a.key, a.outcome)))
        return 0
    now = parse(a.now) if a.now else datetime.now(tz())
    if now is None:
        p.error("--now must be an ISO time")
    print(json.dumps(scan(now, a.events_json), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
