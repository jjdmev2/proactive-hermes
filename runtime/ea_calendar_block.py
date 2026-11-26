#!/usr/bin/env python3
"""Private blocks on the configured calendar.

Blocks are private, have no attendees and are idempotent by key. Only blocks
created by Proactive Hermes may be changed or deleted.

Usage:
  ea_calendar_block.py free   --date YYYY-MM-DD [--minutes 30] [--from 09:00] [--to 18:00]
  ea_calendar_block.py create --key KEY --title T --start ISO --end ISO
                              [--notes TEXT] [--alert 10] [--allow-overlap]
  ea_calendar_block.py alert  --key ea:... --title T --at ISO [--notes TEXT]
                              (5 min private alert, popup at 0 min, may overlap, not in the daily cap)
  ea_calendar_block.py mine   [--days 14]
  ea_calendar_block.py delete --key KEY      (only Proactive Hermes blocks)

Authority: create, alert and delete are calendar writes made under `calendar_blocks`. Before any Google call they
need it in the automatic authority (phconf.authorize, phconf.authorize_cleanup); 'ask' and 'never' always refuse.
delete (cleanup of the plugin's own block or alert) also honours `delete_data`: listed in never or ask first, it
refuses. When the owner says yes in chat to an ask-first block or removal, the chat agent uses Hermes's own
calendar tools, never this script. A refusal prints {"status": "not_allowed", ...}, is logged, exits 5 and
changes nothing. free and mine only read.

`alert` is the time-critical path: a phone that syncs this calendar fires the popup on its own,
so it never waits for the Hermes app to wake (the mobile bridge can lag by minutes).

Times without an offset use the configured timezone. Output is one JSON object on stdout.
Exit codes: 0 ok, 2 bad input, 3 conflict (overlap), 4 Google error, 5 not allowed by the configured authority.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta

import phconf

TZ = phconf.tz()
CAL = phconf.CFG["calendar"]["id"]
TAG = "proactive_hermes_key"
CREATED_BY = "proactive-hermes"
LOG = phconf.state_dir() / "actions_log.jsonl"
DAILY_CAP = phconf.CFG["calendar"]["blocks_per_day"]  # alerts are not counted
ALERT_MINUTES = 5
ALERT_TITLE_CHARS = 60
ACTION = "calendar_blocks"   # the authority every calendar write here is made under


def refused(key: str) -> dict | None:
    """The logged refusal when the write may not go ahead, else None. Called before the Google service is built."""
    ok, basis = phconf.authorize(ACTION)
    if ok:
        return None
    log({"kind": "calendar_not_allowed", "key": key, "authority": basis})
    return {"status": "not_allowed", "action": ACTION, "authority": basis, "key": key}


def out(obj: dict, code: int = 0) -> None:
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(code)


def service():
    os.environ.setdefault("HERMES_HOME", str(phconf.hermes_home()))
    sys.path.insert(0, str(phconf.google_api().parent))
    import google_api as g
    return g.build_service("calendar", "v3")


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def busy(svc, start: datetime, end: datetime) -> list[dict]:
    items = svc.events().list(
        calendarId=CAL, timeMin=start.isoformat(), timeMax=end.isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=100,
    ).execute().get("items", [])
    rows = []
    for e in items:
        if e.get("status") == "cancelled" or e.get("transparency") == "transparent":
            continue
        if "dateTime" not in e.get("start", {}):
            continue  # all-day markers do not block time
        me = next((a for a in e.get("attendees", []) if a.get("self")), None)
        if me and me.get("responseStatus") == "declined":
            continue
        rows.append({
            "id": e["id"], "title": e.get("summary", ""),
            "start": parse_dt(e["start"]["dateTime"]).astimezone(TZ),
            "end": parse_dt(e["end"]["dateTime"]).astimezone(TZ),
        })
    return rows


def find_mine(svc, key: str) -> dict | None:
    items = svc.events().list(
        calendarId=CAL, privateExtendedProperty=f"{TAG}={key}",
        showDeleted=False, maxResults=5, singleEvents=True,
    ).execute().get("items", [])
    items = [e for e in items if e.get("status") != "cancelled"
             and e.get("extendedProperties", {}).get("private", {}).get("created_by") == CREATED_BY]
    return items[0] if items else None


def log(entry: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(json.dumps({"at": datetime.now(TZ).isoformat(timespec="seconds"), **entry},
                                ensure_ascii=False) + "\n")
    except Exception:
        pass


def cmd_free(a) -> None:
    svc = service()
    day = date.fromisoformat(a.date)
    lo = datetime.combine(day, time.fromisoformat(a.from_), TZ)
    hi = datetime.combine(day, time.fromisoformat(a.to), TZ)
    now = datetime.now(TZ) + timedelta(minutes=10)
    if now > lo:
        lo = now.replace(second=0, microsecond=0)
        lo += timedelta(minutes=(5 - lo.minute % 5) % 5)
    need = timedelta(minutes=a.minutes)
    slots, cursor = [], lo
    for ev in busy(svc, lo - timedelta(hours=12), hi):
        if ev["end"] <= cursor:
            continue
        if ev["start"] - cursor >= need:
            slots.append((cursor, ev["start"]))
        cursor = max(cursor, ev["end"])
    if hi - cursor >= need:
        slots.append((cursor, hi))
    out({"status": "ok", "date": a.date, "minutes": a.minutes,
         "free": [{"start": s.isoformat(), "end": e.isoformat()} for s, e in slots]})


def cmd_create(a) -> None:
    start, end = parse_dt(a.start), parse_dt(a.end)
    if end <= start:
        out({"status": "error", "error": "end must be after start"}, 2)
    if not a.key.startswith("ea:"):
        out({"status": "error", "error": "key must start with 'ea:'"}, 2)
    denied = refused(a.key)
    if denied:
        out(denied, 5)
    svc = service()
    existing = find_mine(svc, a.key)
    if not existing and not a.force:
        today = datetime.now(TZ).date().isoformat()
        made = 0
        try:
            for line in LOG.read_text().splitlines():
                row = json.loads(line)
                if row.get("kind") == "calendar_block" and row.get("status") == "created" \
                        and str(row.get("at", "")).startswith(today):
                    made += 1
        except Exception:
            pass
        if made >= DAILY_CAP:
            out({"status": "refused", "reason": f"daily cap {DAILY_CAP} new blocks reached"}, 3)
    clash = [b for b in busy(svc, start, end)
             if b["start"] < end and b["end"] > start and (not existing or b["id"] != existing["id"])]
    if clash and not a.allow_overlap:
        out({"status": "conflict", "key": a.key,
             "with": [{"title": c["title"], "start": c["start"].isoformat(), "end": c["end"].isoformat()}
                      for c in clash]}, 3)
    body = {
        "summary": a.title,
        "description": ((a.notes or "").strip() + "\n\nSet by Proactive Hermes.").strip(),
        "start": {"dateTime": start.astimezone(TZ).isoformat(), "timeZone": TZ.key},
        "end": {"dateTime": end.astimezone(TZ).isoformat(), "timeZone": TZ.key},
        "visibility": "private",
        "transparency": "opaque",
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": a.alert}]},
        "extendedProperties": {"private": {TAG: a.key, "created_by": CREATED_BY}},
    }
    if existing:
        ev = svc.events().patch(calendarId=CAL, eventId=existing["id"], body=body).execute()
        status = "updated"
    else:
        ev = svc.events().insert(calendarId=CAL, body=body).execute()
        status = "created"
    back = svc.events().get(calendarId=CAL, eventId=ev["id"]).execute()  # read-back, not trust
    ok = back.get("visibility") == "private" and back.get("extendedProperties", {}).get("private", {}).get(TAG) == a.key
    result = {"status": status if ok else "unverified", "key": a.key, "id": back["id"],
              "title": back.get("summary"), "start": back["start"].get("dateTime"),
              "end": back["end"].get("dateTime"), "visibility": back.get("visibility"),
              "calendar": back.get("organizer", {}).get("email")}
    log({"kind": "calendar_block", **result})
    out(result, 0 if ok else 4)


def alert_body(key: str, title: str, at: datetime, notes: str = "") -> dict:
    """A short private event whose only job is to make the phone buzz at `at` (popup at 0 min).
    Transparent, so it never shows as busy and never blocks a free slot; overlap is fine."""
    title = " ".join((title or "").split())
    if len(title) > ALERT_TITLE_CHARS:
        title = title[:ALERT_TITLE_CHARS - 1].rstrip() + "\u2026"
    at = at.astimezone(TZ)
    return {
        "summary": title,
        "description": ((notes or "").strip() + "\n\nAlert set by Proactive Hermes.").strip(),
        "start": {"dateTime": at.isoformat(), "timeZone": TZ.key},
        "end": {"dateTime": (at + timedelta(minutes=ALERT_MINUTES)).isoformat(), "timeZone": TZ.key},
        "visibility": "private",
        "transparency": "transparent",
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 0}]},
        "extendedProperties": {"private": {TAG: key, "created_by": CREATED_BY, "kind": "alert"}},
    }


def alert_ok(back: dict, key: str) -> bool:
    rem = back.get("reminders") or {}
    return (back.get("visibility") == "private"
            and back.get("extendedProperties", {}).get("private", {}).get(TAG) == key
            and rem.get("useDefault") is False
            and {"method": "popup", "minutes": 0} in (rem.get("overrides") or []))


def make_alert(svc, key: str, title: str, at: datetime, notes: str = "") -> tuple[dict, int]:
    """Create or update (idempotent by key) and read back. Returns (result, exit code). Refuses (exit 5) without
    calendar_blocks authority, before touching `svc`."""
    if not key.startswith("ea:"):
        return {"status": "error", "error": "key must start with 'ea:'"}, 2
    if not (title or "").strip():
        return {"status": "error", "error": "title is empty"}, 2
    denied = refused(key)
    if denied:
        return denied, 5
    body = alert_body(key, title, at, notes)
    existing = find_mine(svc, key)  # only events this plugin created
    if existing:
        ev = svc.events().patch(calendarId=CAL, eventId=existing["id"], body=body).execute()
        status = "updated"
    else:
        ev = svc.events().insert(calendarId=CAL, body=body).execute()
        status = "created"
    back = svc.events().get(calendarId=CAL, eventId=ev["id"]).execute()  # read-back, not trust
    ok = alert_ok(back, key)
    result = {"status": status if ok else "unverified", "key": key, "id": back["id"],
              "title": back.get("summary"), "start": back["start"].get("dateTime"),
              "end": back["end"].get("dateTime"), "visibility": back.get("visibility"),
              "reminders": back.get("reminders"), "calendar": back.get("organizer", {}).get("email")}
    log({"kind": "calendar_alert", **result})
    return result, (0 if ok else 4)


def cmd_alert(a) -> None:
    try:
        at = parse_dt(a.at)
    except ValueError:
        out({"status": "error", "error": "--at must be an ISO date and time"}, 2)
    if not a.key.startswith("ea:"):
        out({"status": "error", "error": "key must start with 'ea:'"}, 2)
    denied = refused(a.key)   # before service(): a refusal never builds a Google client
    if denied:
        out(denied, 5)
    result, code = make_alert(service(), a.key, a.title, at, a.notes or "")
    out(result, code)


def cmd_mine(a) -> None:
    svc = service()
    now = datetime.now(TZ)
    items = svc.events().list(
        calendarId=CAL, timeMin=(now - timedelta(days=1)).isoformat(),
        timeMax=(now + timedelta(days=a.days)).isoformat(), privateExtendedProperty=f"created_by={CREATED_BY}",
        singleEvents=True, orderBy="startTime", maxResults=100,
    ).execute().get("items", [])
    out({"status": "ok", "blocks": [{
        "key": e.get("extendedProperties", {}).get("private", {}).get(TAG), "id": e["id"],
        "title": e.get("summary"), "start": e["start"].get("dateTime"), "end": e["end"].get("dateTime"),
        "visibility": e.get("visibility")} for e in items]})


def cmd_delete(a) -> None:
    # Removing the plugin's own block or alert is a calendar write: automatic authority only, and delete_data.
    ok, action, basis = phconf.authorize_cleanup(ACTION, delete=True)
    if not ok:
        log({"kind": "calendar_not_allowed", "key": a.key, "action": action, "authority": basis})
        out({"status": "not_allowed", "action": action, "authority": basis, "key": a.key}, 5)
    svc = service()
    ev = find_mine(svc, a.key)
    if not ev:
        out({"status": "not_found", "key": a.key})
    if ev.get("extendedProperties", {}).get("private", {}).get("created_by") != CREATED_BY:
        out({"status": "refused", "error": "not a Proactive Hermes block"}, 2)
    svc.events().delete(calendarId=CAL, eventId=ev["id"]).execute()
    log({"kind": "calendar_block_deleted", "key": a.key, "id": ev["id"]})
    out({"status": "deleted", "key": a.key, "id": ev["id"]})


# Untrusted text (mail, Slack, calendar, web, people) never rides on a shell command line: each text option has
# a sibling --NAME-file that reads UTF-8 from a file ('-' = stdin, one trailing newline stripped). Prompts write
# that file with the write_file tool, then pass --NAME-file. The plain option stays for backwards compatibility.
_TEXT_OPTIONS: list[str] = []


def _text_option(target, name: str, *, required: bool = False, group: bool = True, **kw) -> None:
    dest = target.add_mutually_exclusive_group(required=required) if group else target
    dest.add_argument(f"--{name}", **kw)
    dest.add_argument(f"--{name}-file", metavar="PATH", help=f"read --{name} from a UTF-8 file ('-' = stdin)")
    if name not in _TEXT_OPTIONS:
        _TEXT_OPTIONS.append(name)


def _read_text_file(path: str) -> str:
    if path == "-":
        text = sys.stdin.buffer.read().decode("utf-8")
    else:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    return text[:-1] if text.endswith("\n") else text


def _load_text_files(args, parser) -> None:
    for name in _TEXT_OPTIONS:
        attr = name.replace("-", "_") + "_file"
        if not hasattr(args, attr):
            continue
        path = getattr(args, attr)
        delattr(args, attr)
        if path is None:
            continue
        try:
            setattr(args, name.replace("-", "_"), _read_text_file(path))
        except (OSError, UnicodeError) as exc:
            parser.error(f"--{name}-file {path}: {exc}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("free")
    f.add_argument("--date", required=True)
    f.add_argument("--minutes", type=int, default=30)
    f.add_argument("--from", dest="from_", default="09:00")
    f.add_argument("--to", default="18:00")
    c = sub.add_parser("create")
    c.add_argument("--key", required=True)
    _text_option(c, "title", required=True)
    c.add_argument("--start", required=True)
    c.add_argument("--end", required=True)
    _text_option(c, "notes", default="")
    c.add_argument("--alert", type=int, default=5)
    c.add_argument("--allow-overlap", action="store_true")
    c.add_argument("--force", action="store_true", help="ignore the daily cap")
    al = sub.add_parser("alert")
    al.add_argument("--key", required=True)
    _text_option(al, "title", required=True)
    al.add_argument("--at", required=True)
    _text_option(al, "notes", default="")
    m = sub.add_parser("mine")
    m.add_argument("--days", type=int, default=14)
    d = sub.add_parser("delete")
    d.add_argument("--key", required=True)
    a = p.parse_args()
    _load_text_files(a, p)
    if not phconf.CFG["calendar"]["enabled"]:
        out({"status": "disabled"})
    try:
        {"free": cmd_free, "create": cmd_create, "alert": cmd_alert, "mine": cmd_mine,
         "delete": cmd_delete}[a.cmd](a)
    except SystemExit:
        raise
    except Exception as exc:
        out({"status": "error", "error": f"{type(exc).__name__}: {exc}"[:400]}, 4)


if __name__ == "__main__":
    main()
