#!/usr/bin/env python3
"""Promise ledger: what the owner said they would do, to whom, by when, and whether he did it.

The promise-tracker job reads the owner's sent mail, Slack messages and Bot Chat, records each
commitment here, and closes it only on evidence (a later sent mail/Slack reply to that person,
the thing delivered, or the owner saying it's done). Due and overdue promises feed the desk (compose items, close-out,
evening brief). Private state; nothing here is ever sent to anyone.

  ea_promises.py add --key K --to WHO --what WHAT [--due ISO] --source REF --quote "exact words" [--channel mail|slack|chat|imessage]
  ea_promises.py close --key K --evidence REF        # done, with proof
  ea_promises.py drop --key K --why TEXT             # not a promise / the owner cancelled it
  ea_promises.py snooze --key K --until ISO
  ea_promises.py list [--open] [--due-within HOURS]  # JSON
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta

import phconf

STATE = phconf.state_dir()
LEDGER = STATE / "promises.json"
DEFAULT_DUE_WORKDAYS = 2


def now() -> datetime:
    return datetime.now(phconf.tz())


def parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=phconf.tz())


def add_workdays(start: datetime, n: int) -> datetime:
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d.replace(hour=17, minute=0, second=0, microsecond=0)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "promise"


def load() -> dict:
    try:
        data = json.loads(LEDGER.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save(data: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))
    tmp.replace(LEDGER)


def cmd_add(a) -> dict:
    data = load()
    key = a.key or "promise-" + slug(f"{a.to} {a.what}")
    made = parse(a.made) or now()
    due = parse(a.due) or add_workdays(made, DEFAULT_DUE_WORKDAYS)
    prev = data.get(key)
    if prev and prev.get("status") in ("done", "dropped"):
        return {"status": "exists", "key": key, "state": prev["status"]}
    row = {**(prev or {}), "key": key, "to": a.to, "what": a.what, "due": due.isoformat(timespec="minutes"),
           "made": (prev or {}).get("made") or made.isoformat(timespec="minutes"),
           "source": a.source, "quote": (a.quote or (prev or {}).get("quote") or "")[:300], "channel": a.channel,
           "status": (prev or {}).get("status", "open")}
    data[key] = row
    save(data)
    return {"status": "updated" if prev else "added", "key": key, "due": row["due"]}


def _set(key: str, **fields) -> dict:
    data = load()
    if key not in data:
        return {"status": "unknown", "key": key}
    data[key].update(fields)
    save(data)
    return {"status": data[key]["status"], "key": key}


def cmd_list(a) -> list:
    t = now()
    out = []
    for row in load().values():
        if a.open and row.get("status") != "open":
            continue
        snooze = parse(row.get("snoozed_until"))
        if a.open and snooze and snooze > t:
            continue
        due = parse(row.get("due"))
        if a.due_within is not None and (due is None or due > t + timedelta(hours=a.due_within)):
            continue
        out.append({**row, "overdue": bool(due and due < t), "age_days": _age(row, t)})
    out.sort(key=lambda r: r.get("due") or "")
    return out


def _age(row: dict, t: datetime) -> float | None:
    made = parse(row.get("made"))
    return round((t - made).total_seconds() / 86400, 1) if made else None


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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add")
    s.add_argument("--key")
    _text_option(s, "to", required=True)
    _text_option(s, "what", required=True)
    s.add_argument("--due")
    s.add_argument("--made", help="when the promise was made (ISO); default now")
    _text_option(s, "source", required=True)
    _text_option(s, "quote", default="")
    s.add_argument("--channel", default="mail", choices=["mail", "slack", "chat", "imessage", "whatsapp", "meeting"])
    s = sub.add_parser("close")
    s.add_argument("--key", required=True)
    _text_option(s, "evidence", required=True)
    s = sub.add_parser("drop")
    s.add_argument("--key", required=True)
    _text_option(s, "why", required=True)
    s = sub.add_parser("snooze")
    s.add_argument("--key", required=True)
    s.add_argument("--until", required=True)
    s = sub.add_parser("list")
    s.add_argument("--open", action="store_true")
    s.add_argument("--due-within", type=float)
    a = p.parse_args(argv)
    _load_text_files(a, p)
    t = now().isoformat(timespec="minutes")
    if a.cmd == "add":
        res = cmd_add(a)
    elif a.cmd == "close":
        res = _set(a.key, status="done", closed_at=t, evidence=a.evidence[:300])
    elif a.cmd == "drop":
        res = _set(a.key, status="dropped", closed_at=t, evidence=a.why[:300])
    elif a.cmd == "snooze":
        res = _set(a.key, snoozed_until=a.until)
    else:
        res = cmd_list(a)
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
