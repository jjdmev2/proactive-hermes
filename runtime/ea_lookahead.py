#!/usr/bin/env python3
"""Look-ahead gate and ledger: the desk looks at upcoming plans before the owner has to ask.

Higher stakes = earlier first look and more checkpoints. A checkpoint wakes the agent; the agent
speaks only when it finds a gap, a change or a decision, then records the outcome with `fired`.
Read-only on calendar and mail; private state in <state>/lookahead.json.

  ea_lookahead.py scan [--calendar-json F] [--mail-json F] [--now ISO]
  ea_lookahead.py fired --key K --checkpoint C --outcome said|quiet
  ea_lookahead.py answer --key K --mode walk|scooter|uber|taxi|bus|train|drive|not-mine|later|booked [--note TEXT]
  ea_lookahead.py list [--days N]

Every command exits 0 and prints JSON. `scan` prints {"wakeAgent": ...} as the LAST stdout line.

Config (all optional, under "lookahead" in the Proactive Hermes config; nothing personal lives in code):
  calendar_account  calendar id to read (default: calendar.id, then "primary")
  mail_account      label of the mailbox the booking mails come from (default: owner.work_emails[0], then "gmail").
                    The google-workspace skill reads whichever Google account it is authorised for.
  home_places       regex words for where the owner lives (e.g. ["Germany", "Berlin", "Munich"]).
  watch_words       extra regex words for events watched remotely, on top of "watch party", "livestream".
  airport_codes     case-sensitive airport codes that mark a flight in a title (e.g. ["BER"]).
  senders           {"train": ["bahn.de"], ...} extra sender fragments per kind (flight, stay, train, bus, event).
  keywords          {"train": ["bahn"], ...} extra regex words per kind (flight, stay, train, bus, ticket_sale, bill).
  mail_query        replace the whole Gmail booking-mail query.
Quiet hours come from the top-level quiet_hours; wall-clock logic uses the top-level timezone.

A "watched remotely" event (title has a watch word, e.g. a race livestream or a watch party) is skipped
when its location does not match home_places. With home_places empty no location can match, so every
event whose title has a watch word is skipped; set home_places to keep local watch parties.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, time, timedelta, tzinfo
from email.utils import parsedate_to_datetime
from pathlib import Path

import phconf

WINDOW_DAYS = 30
DETAIL_MAIL_DAYS = 14
MAX_DUE = 6
MERGE_WINDOW = timedelta(hours=3)
CLOSED = {"done", "closed", "dropped", "resolved", "cancelled"}
MODES = ["walk", "scooter", "uber", "taxi", "bus", "train", "drive", "not-mine", "later", "booked"]
TIERS = {"flight": "A", "stay": "A", "train": "B", "bus": "B", "event": "B", "ticket_sale": "B", "bill": "B"}
TRAVEL = {"flight", "stay", "train", "bus"}
KINDS = ("flight", "stay", "train", "bus", "ticket_sale", "bill")

# Words that classify a calendar entry or mail. Extended per user with lookahead.keywords.
KEYWORDS = {
    "flight": r"flight|airport|\bboarding\b|ryanair|easyjet|lufthansa|finnair|norwegian air|\bsas\b|airbaltic"
              r"|wizz ?air|\bklm\b|airlines?\b|airways",
    "stay": r"hotel|hostel|airbnb|booking\.com",
    "train": r"\btrains?\b|\brailway\b|\brail\b|\bstation\b|trainline|eurostar|amtrak|\bvr\.fi",
    "bus": r"\bbus\b|flixbus|megabus|greyhound|onnibus|matkahuolto",
    "ticket_sale": r"on sale|ticket sale|registration opens|pre-sale|presale",
    "bill": r"\bdue\b(?!\s+to\b)|invoice|renewal|renews",
}
# Sender fragments per kind (mail from, lowercase). Extended per user with lookahead.senders.
SENDERS = {
    "train": ["vr.fi", "trainline", "eurostar", "amtrak", "bahn.de", "sncf"],
    "flight": ["finnair", "norwegian", "ryanair", "easyjet", "lufthansa", "flysas", "airbaltic", "wizzair"],
    "bus": ["matkahuolto", "onnibus", "flixbus"],
    "stay": ["booking.com", "airbnb"],
    "event": ["tiketti", "lippu.fi", "ticketmaster", "eventbrite", "lu.ma", "melee.gg", "dice.fm"],
}
SUBJECT_WORDS = ["booking confirmation", "itinerary", "e-ticket", "your tickets"]
VIRTUAL = re.compile(r"meet\.google|zoom|teams|slack|huddle|discord|webex|whereby|online|virtual", re.I)
WATCH_WORDS = r"watch party|livestream|live stream|viewing party|watch along"


# --------------------------------------------------------------------------- config

def conf() -> dict:
    c = phconf.CFG.get("lookahead")
    return c if isinstance(c, dict) else {}


def _words(value) -> list[str]:
    return [str(w) for w in value if str(w).strip()] if isinstance(value, list) else []


def _per_kind(name: str, base: dict) -> dict:
    extra = conf().get(name)
    extra = extra if isinstance(extra, dict) else {}
    return {k: list(base.get(k, [])) + _words(extra.get(k)) for k in set(base) | set(extra)}


def tz() -> tzinfo:
    return phconf.tz()


def calendar_account() -> str:
    cal = phconf.CFG.get("calendar")
    return (str(conf().get("calendar_account") or "").strip()
            or str((cal or {}).get("id") or "").strip() or "primary")


def mail_account() -> str:
    emails = (phconf.CFG.get("owner") or {}).get("work_emails") or []
    return str(conf().get("mail_account") or "").strip() or (str(emails[0]) if emails else "gmail")


def kind_rx() -> dict[str, re.Pattern]:
    extra = conf().get("keywords")
    extra = extra if isinstance(extra, dict) else {}
    out = {}
    for kind in KINDS:
        parts = [KEYWORDS[kind], *_words(extra.get(kind))]
        out[kind] = re.compile("|".join(f"(?:{p})" for p in parts), re.I)
    return out


def code_rx() -> re.Pattern | None:
    codes = _words(conf().get("airport_codes"))
    return re.compile(r"\b(?:" + "|".join(re.escape(c) for c in codes) + r")\b") if codes else None


def sender_types() -> list[tuple[tuple[str, ...], str]]:
    per_kind = _per_kind("senders", SENDERS)
    return [(tuple(n.lower() for n in needles), kind) for kind, needles in per_kind.items() if needles]


def mail_query() -> str:
    custom = str(conf().get("mail_query") or "").strip()
    if custom:
        return custom
    needles = sorted({n for ns, _ in sender_types() for n in ns})
    terms = [f"from:{n}" for n in needles] + [f'subject:"{w}"' if " " in w else f"subject:{w}" for w in SUBJECT_WORDS]
    return f"newer_than:60d ({' OR '.join(terms)})"


# --------------------------------------------------------------------------- state

def _load(path: Path, default):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, type(default)) else default
    except (FileNotFoundError, ValueError):
        return default


def load() -> dict:
    data = _load(phconf.state_dir() / "lookahead.json", {})
    data.setdefault("items", {})
    return data


def save(data: dict) -> None:
    path = phconf.state_dir() / "lookahead.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))
    tmp.replace(path)


def closed_keys() -> set[str]:
    led = _load(phconf.state_dir() / "actions.json", {})
    return {k for k, v in led.items() if isinstance(v, dict) and str(v.get("status", "")).lower() in CLOSED}


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(tz()).isoformat(timespec="minutes") if dt else None


def parse_dt(ts) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tz())


def is_quiet(now: datetime) -> bool:
    start, end = phconf.quiet_hours()
    hour = now.astimezone(tz()).hour
    return start <= hour < end if start <= end else hour >= start or hour < end


# --------------------------------------------------------------------------- classification

def watched_remotely(title: str, location: str) -> bool:
    """A watch-word title whose location is not one of home_places: watched (stream, TV), not attended."""
    words = [WATCH_WORDS, *_words(conf().get("watch_words"))]
    if not re.search("|".join(f"(?:{w})" for w in words), title or "", re.I):
        return False
    home = _words(conf().get("home_places"))
    return not (home and re.search("|".join(f"(?:{w})" for w in home), location or "", re.I))


def physical(location: str) -> bool:
    loc = (location or "").strip()
    return bool(loc) and not re.fullmatch(r"https?://\S+", loc) and not VIRTUAL.search(loc)


def classify(text: str, location: str = "", multi_day: bool = False, title: str | None = None) -> str:
    """First match wins: flight, stay, train, bus, ticket_sale, bill, event, meeting.

    Bill words ("due", invoice, renewal) are matched on `title` (summary + location) when given: in
    descriptions they are prose ("review due promises", "this due date is the next review")."""
    rx = kind_rx()
    codes = code_rx()
    if rx["flight"].search(text) or (codes and codes.search(text)):
        return "flight"
    if multi_day or rx["stay"].search(text):
        return "stay"
    for kind in ("train", "bus", "ticket_sale", "bill"):
        if rx[kind].search(title if kind == "bill" and title is not None else text):
            return kind
    return "event" if physical(location) else "meeting"


def sender_type(sender: str, subject: str = "") -> str:
    s = (sender or "").lower()
    for needles, kind in sender_types():
        if any(n in s for n in needles):
            return kind
    kind = classify(f"{subject} {sender}")
    return kind if kind != "meeting" else "event"


# --------------------------------------------------------------------------- trip subjects

# "Your tickets 18.11. Berlin – Hamburg (23:10 - 1:10)": the day.month. form some rail operators put in the subject.
TRIP_RX = re.compile(
    r"(?:your tickets|lippusi)\s+(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.\s*(?P<orig>.+?)\s+[–—-]\s+(?P<dest>.+?)\s*"
    r"\(\s*(?P<h1>\d{1,2})[:.](?P<n1>\d{2})\s*[–—-]\s*(?P<h2>\d{1,2})[:.](?P<n2>\d{2})\s*\)", re.I)


def parse_trip_subject(subject: str, mail_date: datetime) -> dict | None:
    """'Your tickets 18.11. Berlin – Hamburg (23:10 - 1:10)' -> trip with tz-aware depart/arrive."""
    m = TRIP_RX.search(subject or "")
    if not m:
        return None
    zone = tz()
    md = mail_date.astimezone(zone)
    try:
        day = date(md.year, int(m["m"]), int(m["d"]))
        if day < md.date() - timedelta(days=60):
            day = date(md.year + 1, int(m["m"]), int(m["d"]))
        dep = datetime.combine(day, time(int(m["h1"]), int(m["n1"])), zone)
        arr = datetime.combine(day, time(int(m["h2"]), int(m["n2"])), zone)
    except ValueError:
        return None
    if arr <= dep:
        arr = datetime.combine(day + timedelta(days=1), arr.time(), zone)
    return {"from": m["orig"].strip(), "to": m["dest"].strip(), "depart": dep, "arrive": arr}


def mail_datetime(raw) -> datetime | None:
    if not raw:
        return None
    dt = parse_dt(raw)
    if dt:
        return dt
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tz())


# --------------------------------------------------------------------------- sources

class SourceError(Exception):
    pass


def _rows(data, *keys) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return data[k]
    raise SourceError(f"unexpected JSON shape: {type(data).__name__}")


def _read_fixture(path: str):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise SourceError(f"{type(e).__name__}: {e}") from None


def _google(args: list[str]):
    cmd = [sys.executable, str(phconf.google_api()), *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SourceError(f"{type(e).__name__}: {e}") from None
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-1:] or ["no output"]
        raise SourceError(f"exit {p.returncode}: {tail[0][:200]}")
    try:
        return json.loads(p.stdout)
    except ValueError as e:
        raise SourceError(f"bad JSON from google_api.py: {e}") from None


def fetch_calendar(now: datetime, fixture: str | None) -> list:
    if fixture:
        return _rows(_read_fixture(fixture), "items", "events")
    end = now + timedelta(days=WINDOW_DAYS)
    return _rows(_google(["calendar", "list", "--start", now.isoformat(), "--end", end.isoformat(), "--max", "250",
                          "--calendar", calendar_account()]), "items", "events")


def fetch_mail(fixture: str | None) -> list:
    if fixture:
        return _rows(_read_fixture(fixture), "messages", "items")
    return _rows(_google(["gmail", "search", mail_query(), "--max", "25"]), "messages", "items")


# --------------------------------------------------------------------------- building items

def _when(v) -> tuple[datetime | None, bool]:
    """Calendar start/end: dict {dateTime|date} or the flattened string google_api.py prints."""
    if isinstance(v, dict):
        v = v.get("dateTime") or v.get("date")
    if not isinstance(v, str) or not v:
        return None, False
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return datetime.combine(date.fromisoformat(v), time(0, 0), tz()), True
    return parse_dt(v), False


def _self_attendee(ev: dict) -> dict | None:
    for a in ev.get("attendees") or []:
        if isinstance(a, dict) and a.get("self"):
            return a
    return None


def calendar_items(events: list, now: datetime) -> list[dict]:
    out = []
    for ev in events:
        if not isinstance(ev, dict) or not ev.get("id") or ev.get("status") == "cancelled":
            continue
        me = _self_attendee(ev)
        if me and me.get("responseStatus") == "declined":
            continue
        start, all_day = _when(ev.get("start"))
        end, _ = _when(ev.get("end"))
        if not start or start <= now:
            continue
        multi_day = bool(all_day and end and (end.date() - start.date()).days >= 2)
        summary, location = ev.get("summary") or "(no title)", ev.get("location") or ""
        text = f"{summary} {location} {(ev.get('description') or '')[:500]}"
        kind = classify(text, location, multi_day, title=f"{summary} {location}")
        if kind == "meeting" or (kind == "event" and watched_remotely(summary, location)):
            continue
        organizer_self = bool((ev.get("organizer") or {}).get("self"))
        accepted = bool(me and me.get("responseStatus") == "accepted") or (not ev.get("attendees") and organizer_self)
        out.append({
            "key": f"la:cal:{ev['id']}", "type": kind, "title": summary, "start": start, "end": end,
            "all_day": all_day, "location": location,
            "ownership": "confirmed" if kind in TRAVEL and accepted and organizer_self else "likely",
            "sources": [f"gcal:{ev['id']}"], "needs_detail": False, "mail_ids": [],
        })
    # A routine (same title and place at least twice in the window, e.g. a weekly league night) is known
    # to the owner: tier C, one evening-before look that speaks only for a gap or a change.
    seen: dict[tuple, int] = {}
    for it in out:
        k = (it["title"].strip().lower(), it["location"].strip().lower())
        seen[k] = seen.get(k, 0) + 1
    for it in out:
        if it["type"] == "event" and seen[(it["title"].strip().lower(), it["location"].strip().lower())] >= 2:
            it["recurring"] = True
    return out


def mail_items(mails: list, now: datetime) -> list[dict]:
    out = []
    account = mail_account()
    for m in mails:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        sent = mail_datetime(m.get("date"))
        subject, sender = m.get("subject") or "", m.get("from") or ""
        base = {"key": f"la:mail:{m['id']}", "sources": [f"gmail:{account}:{m['id']}"], "mail_ids": [m["id"]],
                "all_day": False}
        trip = parse_trip_subject(subject, sent) if sent else None
        if trip:
            if trip["depart"] <= now:
                continue
            out.append({**base, "type": "train", "title": f"Train {trip['from']} → {trip['to']}",
                        "start": trip["depart"], "end": trip["arrive"], "location": f"{trip['from']} → {trip['to']}",
                        "ownership": "confirmed", "needs_detail": False,
                        "legs": [{"from": trip["from"], "to": trip["to"], "depart": iso(trip["depart"]),
                                  "arrive": iso(trip["arrive"]), "mail_id": m["id"]}]})
            continue
        if not sent or sent < now - timedelta(days=DETAIL_MAIL_DAYS) or sent > now + timedelta(hours=1):
            continue
        out.append({**base, "type": sender_type(sender, subject), "title": subject or "(no subject)",
                    "start": None, "end": None, "location": "", "ownership": "unknown", "needs_detail": True,
                    "from": sender, "mail_date": iso(sent)})
    return out


def _words_of(*texts: str) -> set[str]:
    return {w for t in texts for w in re.findall(r"[^\W\d_]{3,}", (t or "").lower())}


def merge(cal: list[dict], mail: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Fold parsed mail trips into an overlapping calendar item sharing a city word."""
    merged_into, rest = {}, []
    for mi in mail:
        best = None
        if mi.get("legs") and mi["start"]:
            cities = _words_of(mi["legs"][0]["from"], mi["legs"][0]["to"])
            for ci in cal:
                gap = abs(ci["start"] - mi["start"])
                if gap <= MERGE_WINDOW and cities & _words_of(ci["title"], ci["location"]):
                    if best is None or gap < abs(best["start"] - mi["start"]):
                        best = ci
        if best is None:
            rest.append(mi)
            continue
        best["ownership"] = "confirmed"
        best["mail_ids"] = sorted(set(best["mail_ids"]) | set(mi["mail_ids"]))
        best["sources"] = best["sources"] + [s for s in mi["sources"] if s not in best["sources"]]
        best.setdefault("legs", []).extend(mi.get("legs", []))
        merged_into[mi["key"]] = best["key"]
    return cal + rest, merged_into


# --------------------------------------------------------------------------- checkpoints

def schedule(kind: str, start: datetime, recurring: bool = False) -> dict[str, datetime]:
    zone = tz()
    local = start.astimezone(zone)

    def wall(days_before: int, hh: int, mm: int) -> datetime:
        return datetime.combine(local.date() - timedelta(days=days_before), time(hh, mm), zone)

    off = lambda **kw: start - timedelta(**kw)  # noqa: E731
    t1d, morning = wall(1, 19, 0), wall(0, 7, 30)
    if kind == "flight":
        cps = {"t30d": off(days=30), "t14d": off(days=14), "t7d": off(days=7), "checkin": off(hours=48),
               "t1d": t1d, "morning": off(hours=3) if local.time() < time(10, 30) else morning,
               "leave": off(hours=4)}
    elif kind == "stay":
        cps = {"t30d": off(days=30), "t14d": off(days=14), "t7d": off(days=7), "t2d": off(days=2),
               "t1d": t1d, "morning": morning}
    elif kind in ("train", "bus"):
        cps = {"t7d": off(days=7), "t2d": off(days=2), "t1d": t1d, "leave": off(minutes=90)}
        if local.time() >= time(8, 30):
            cps["morning"] = morning
    elif kind == "event" and recurring:
        cps = {"t1d": t1d}
    elif kind == "event":
        cps = {"t7d": off(days=7), "t2d": off(days=2), "t1d": t1d, "morning": morning, "leave": off(minutes=90)}
    elif kind == "ticket_sale":
        cps = {"t7d": off(days=7), "t1d": t1d, "open30": off(minutes=30)}
    elif kind == "bill":
        cps = {"t14d": off(days=14), "t3d": off(days=3), "t1d": t1d}
    else:
        cps = {}
    return {k: v for k, v in cps.items() if v < start}  # a checkpoint at or after start can never be due


def reschedule(row: dict, now: datetime) -> None:
    """(Re)compute unfired checkpoints from the row's current start."""
    cps = row.setdefault("checkpoints", {})
    start = parse_dt(row.get("start"))
    if start is None:  # needs_detail mail: one look, as soon as it is detected
        cps.setdefault("detail", {"at": row.get("first_seen") or iso(now), "state": "pending"})
        return
    fresh = schedule(row["type"], start, bool(row.get("recurring")))
    for name in list(cps):
        if cps[name].get("state") != "fired" and name not in fresh:
            del cps[name]
    for name, at in fresh.items():
        if cps.get(name, {}).get("state") == "fired":
            continue
        cps[name] = {"at": iso(at), "state": "pending"}


def pick_due(row: dict, now: datetime) -> str | None:
    """Latest due checkpoint (time <= now < start, not fired); earlier pending ones are skipped silently."""
    start = parse_dt(row.get("start"))
    if start is not None and now >= start:
        return None
    later = parse_dt(row.get("later_until"))
    past = sorted(((parse_dt(c["at"]), n) for n, c in row.get("checkpoints", {}).items()
                   if parse_dt(c.get("at")) and parse_dt(c["at"]) <= now), key=lambda x: x[0])
    if not past:
        return None
    latest_at, latest = past[-1]
    for at, name in past[:-1]:
        if row["checkpoints"][name]["state"] == "pending":
            row["checkpoints"][name]["state"] = "skipped"
    cp = row["checkpoints"][latest]
    if cp["state"] != "pending":
        return None
    if later and latest_at < later:  # "later": quiet until the next checkpoint
        cp["state"] = "skipped"
        return None
    return latest


def next_checkpoint(row: dict, now: datetime) -> dict | None:
    pend = sorted((c["at"], n) for n, c in row.get("checkpoints", {}).items()
                  if c.get("state") == "pending" and parse_dt(c["at"]) and parse_dt(c["at"]) > now)
    return {"name": pend[0][1], "at": pend[0][0]} if pend else None


# --------------------------------------------------------------------------- commands

PERSIST = ("checkpoints", "mode", "note", "later_until", "status", "first_seen", "pending_change", "answers",
           "ownership_override", "closed_at")


def _to_row(item: dict, prev: dict | None, now: datetime) -> dict:
    row = {k: v for k, v in item.items() if k not in ("start", "end")}
    row.update(start=iso(item["start"]), end=iso(item["end"]),
               tier="C" if item.get("recurring") else TIERS.get(item["type"], "B"), last_seen=iso(now))
    for k in PERSIST:
        if prev and k in prev:
            row[k] = prev[k]
    row.setdefault("first_seen", iso(now))
    row.setdefault("status", "open")
    if row.get("ownership_override"):
        row["ownership"] = row["ownership_override"]
    return row


def _entry(row: dict, checkpoint: str | None) -> dict:
    change = row.get("pending_change") or {}
    return {"key": row["key"], "type": row["type"], "tier": row["tier"], "title": row.get("title"),
            "start": row.get("start"), "end": row.get("end"), "location": row.get("location") or "",
            "ownership": row.get("ownership"), "checkpoint": checkpoint, "needs_detail": bool(row.get("needs_detail")),
            "changed": bool(change), "old_start": change.get("old_start"), "mode": row.get("mode"),
            "sources": row.get("sources", []), **({"legs": row["legs"]} if row.get("legs") else {})}


def _prune(items: dict, now: datetime) -> None:
    for key, row in list(items.items()):
        start = parse_dt(row.get("start"))
        seen = parse_dt(row.get("first_seen")) or now
        if row.get("status") in CLOSED or row.get("status") == "merged":
            if (parse_dt(row.get("closed_at")) or seen) < now - timedelta(days=90):
                del items[key]
        elif start and start < now - timedelta(days=2):
            del items[key]
        elif not start and seen < now - timedelta(days=DETAIL_MAIL_DAYS + 2):
            del items[key]


def cmd_scan(a, now: datetime) -> dict:
    if is_quiet(now):
        return {"wakeAgent": False}
    data = load()
    items = data["items"]
    unread, read_ok = [], {"calendar": True, "mail": True}
    account = mail_account()
    try:
        cal = calendar_items(fetch_calendar(now, a.calendar_json), now)
    except SourceError as e:
        cal, read_ok["calendar"] = [], False
        unread.append({"source": f"calendar:{calendar_account()}", "error": str(e)})
    try:
        mail = mail_items(fetch_mail(a.mail_json), now)
    except SourceError as e:
        mail, read_ok["mail"] = [], False
        unread.append({"source": f"mail:{account}", "error": str(e)})
    unread.append({"source": "mail:other accounts", "error": f"not read (only the connected Google account, {account})"})

    found, merged_into = merge(cal, mail)
    closed = closed_keys()
    for mk, ck in merged_into.items():
        if mk in items and items[mk].get("status") == "open":
            items[mk].update(status="merged", merged_into=ck, closed_at=iso(now))
    live = set()
    for it in found:
        key, prev = it["key"], items.get(it["key"])
        if key in closed or (prev and prev.get("status") != "open"):
            continue
        row = _to_row(it, prev, now)
        old = parse_dt(prev.get("start")) if prev else None
        new = parse_dt(row["start"])
        if prev and old and new and old != new:
            pc = row.get("pending_change") or {"old_start": prev["start"]}
            pc["new_start"] = row["start"]
            row["pending_change"] = pc
            if parse_dt(pc["old_start"]) == new:  # moved back to where it was: nothing to say
                row.pop("pending_change")
        if prev is None or old != new or prev.get("type") != row["type"] or "checkpoints" not in row:
            reschedule(row, now)
        items[key] = row
        live.add(key)
    # a source that could not be read keeps its stored items alive, so an outage never silences a leave-time
    for key, row in items.items():
        src = "calendar" if key.startswith("la:cal:") else "mail"
        if key not in live and not read_ok[src] and row.get("status") == "open" and key not in closed:
            live.add(key)

    due = []
    for key in live:
        row = items[key]
        cp = pick_due(row, now)
        if cp or row.get("pending_change"):
            due.append(_entry(row, cp))
    due.sort(key=lambda e: (e["start"] is None, parse_dt(e["start"]) or now, e["key"]))
    _prune(items, now)
    data["last_scan"] = {"at": iso(now), "items": len(live), "due": len(due), "sources_unread": unread}
    save(data)
    if not due:
        return {"wakeAgent": False}
    return {"wakeAgent": True, "context": {"due": due[:MAX_DUE], "sources_unread": unread}}


def cmd_fired(a, now: datetime) -> dict:
    data = load()
    row = data["items"].get(a.key)
    if not row:
        return {"error": f"unknown key {a.key}"}
    cps = row.setdefault("checkpoints", {})
    if a.checkpoint == "changed":
        if not row.get("pending_change"):
            return {"error": f"{a.key} has no pending change"}
    elif a.checkpoint not in cps:
        return {"error": f"{a.key} has no checkpoint {a.checkpoint}", "checkpoints": sorted(cps)}
    else:
        cps[a.checkpoint].update(state="fired", outcome=a.outcome, fired_at=iso(now))
    row.pop("pending_change", None)  # whatever was said covered the time move
    save(data)
    return {"status": "fired", "key": a.key, "checkpoint": a.checkpoint, "outcome": a.outcome}


def cmd_answer(a, now: datetime) -> dict:
    data = load()
    row = data["items"].get(a.key)
    if not row:
        return {"error": f"unknown key {a.key}"}
    row.setdefault("answers", []).append({"at": iso(now), "mode": a.mode, "note": (a.note or "")[:300]})
    if a.note:
        row["note"] = a.note[:300]
    if a.mode == "not-mine":
        row.update(status="dropped", closed_at=iso(now))
        row.pop("pending_change", None)
    elif a.mode == "later":
        for c in row.get("checkpoints", {}).values():
            if c.get("state") == "pending" and parse_dt(c.get("at")) and parse_dt(c["at"]) <= now:
                c["state"] = "skipped"
        nxt = next_checkpoint(row, now)
        row["later_until"] = nxt["at"] if nxt else row.get("start")
        row.pop("pending_change", None)
    elif a.mode == "booked":
        row["ownership"] = row["ownership_override"] = "confirmed"
    else:
        row["mode"] = a.mode
    save(data)
    return {"status": "recorded", "key": a.key, "mode": a.mode, "item_status": row.get("status"),
            "later_until": row.get("later_until"), "ownership": row.get("ownership")}


def cmd_list(a, now: datetime) -> dict:
    out = []
    for row in load()["items"].values():
        if row.get("status") != "open":
            continue
        start = parse_dt(row.get("start"))
        if start and (start < now or start > now + timedelta(days=a.days)):
            continue
        out.append({**{k: row.get(k) for k in ("key", "type", "tier", "title", "start", "end", "location",
                                                "ownership", "mode", "needs_detail", "later_until", "sources")},
                    "next_checkpoint": next_checkpoint(row, now)})
    out.sort(key=lambda r: (r["start"] is None, parse_dt(r["start"]) or now, r["key"]))
    return {"items": out, "count": len(out), "now": iso(now)}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--now", help="ISO time, tests only")
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    s = sub.add_parser("scan")
    s.add_argument("--calendar-json")
    s.add_argument("--mail-json")
    s.add_argument("--now", dest="now_sub")
    s = sub.add_parser("fired")
    s.add_argument("--key", required=True)
    s.add_argument("--checkpoint", required=True)
    s.add_argument("--outcome", required=True, choices=["said", "quiet"])
    s.add_argument("--now", dest="now_sub")
    s = sub.add_parser("answer")
    s.add_argument("--key", required=True)
    s.add_argument("--mode", required=True, choices=MODES)
    s.add_argument("--note")
    s.add_argument("--now", dest="now_sub")
    s = sub.add_parser("list")
    s.add_argument("--days", type=int, default=WINDOW_DAYS)
    s.add_argument("--now", dest="now_sub")
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    is_scan = "scan" in argv
    try:
        a = build_parser().parse_args(argv)
        stamp = getattr(a, "now_sub", None) or a.now
        if stamp and parse_dt(stamp) is None:
            raise ValueError(f"bad --now {stamp!r}")
        now = parse_dt(stamp).astimezone(tz()) if stamp else datetime.now(tz())
        res = {"scan": cmd_scan, "fired": cmd_fired, "answer": cmd_answer, "list": cmd_list}[a.cmd](a, now)
    except Exception as e:  # noqa: BLE001 - the contract: never crash, always JSON
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        if is_scan:
            print(json.dumps({"wakeAgent": False}))
        return 0
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
