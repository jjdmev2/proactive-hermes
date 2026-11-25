#!/usr/bin/env python3
"""One door to finish a desk item everywhere: ledger, promise, goal and the phone reminder.

Use it whenever the owner says an item is done, removed, cancelled, old or not needed, in any chat,
and whenever a desk job finds proof it is done. Without it, deleting the calendar event or
answering in chat leaves the desk's records open: the phone reminder stays and the item can
be raised again.

  ea_close.py --key KEY --why "owner: old invite, remove" [--status dropped|done]
  ea_close.py --find TEXT        # list open items whose key/title contains TEXT

Never deletes anything on the phone or calendar the desk did not create. Prints JSON.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

import ea_reminders as reminders
import phconf

LEDGER = reminders.LEDGER
PROMISES = reminders.PROMISES
GOALS = reminders.GOALS


def now() -> str:
    return datetime.now(phconf.tz()).isoformat(timespec="seconds")


goal_rows = reminders.goal_rows


def find(text: str) -> list[dict]:
    t = text.lower()
    out = []
    box, promises = reminders.read_records(reminders.OUTBOX, {}), reminders.read_records(PROMISES, {})
    for k, v in (box if isinstance(box, dict) else {}).items():
        if isinstance(v, dict) and v.get("status") in {"queued", "landed"} and (
                t in k.lower() or t in str(v.get("title", "")).lower()):
            out.append({"key": k, "where": "reminder", "title": v.get("title")})
    for k, v in (promises if isinstance(promises, dict) else {}).items():
        if isinstance(v, dict) and v.get("status") == "open" and (t in k.lower() or t in json.dumps(v).lower()):
            out.append({"key": k, "where": "promise", "title": v.get("what") or v.get("title")})
    for g in goal_rows(reminders.read_records(GOALS, {})):
        if isinstance(g, dict) and g.get("status") == "open" and (
                t in str(g.get("key", "")).lower() or t in str(g.get("title", "")).lower()):
            out.append({"key": g["key"], "where": "goal", "title": g.get("title")})
    return out


def close_ledger(key: str, why: str, status: str) -> bool:
    led = reminders.load(LEDGER, {})
    row = led.get(key)
    row = row if isinstance(row, dict) else {}
    row.update(status=status, closed=now(), evidence=why)
    led[key] = row
    reminders.save(LEDGER, led)
    return True


def close_promise(key: str, why: str, status: str) -> bool:
    promises = reminders.load(PROMISES, {})
    row = promises.get(key)
    if not (isinstance(row, dict) and row.get("status") == "open"):
        return False
    row.update(status=status, closed_at=now(), evidence=why)
    reminders.save(PROMISES, promises)
    return True


def close_goal(key: str, why: str, status: str) -> bool:
    data = reminders.load(GOALS, {})
    hit = False
    for g in goal_rows(data):
        if isinstance(g, dict) and g.get("key") == key and g.get("status") == "open":
            g.update(status=status, closed=now(), evidence=why)
            hit = True
    if hit:
        reminders.save(GOALS, data)
    return hit


def close(key: str, why: str, status: str) -> dict:
    touched, errors = [], {}
    # Each record is independent: one unreadable file must not leave the others open.
    for label, step in (("ledger", close_ledger), ("promise", close_promise), ("goal", close_goal)):
        try:
            if step(key, why, status):
                touched.append(label)
        except Exception as exc:
            errors[label] = f"{type(exc).__name__}: {exc}"[:200]
    try:
        rem_out = reminders.close_reminder(key, why)
    except Exception as exc:
        rem_out = {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:200]}
    if rem_out.get("status") in {"closing", "done"}:
        touched.append("phone reminder")
    # ea_desk.compose skips any key whose ledger status is closed, so it is never raised again.
    out = {"key": key, "status": status, "closed_in": touched, "reminder": rem_out}
    if errors:
        out["errors"] = errors
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--key")
    p.add_argument("--why", default="")
    p.add_argument("--status", choices=["done", "dropped"], default="done")
    p.add_argument("--find")
    a = p.parse_args()
    if a.find:
        print(json.dumps(find(a.find), ensure_ascii=False, indent=1))
        return 0
    if not a.key or not a.why:
        p.error("--key and --why are required")
    print(json.dumps(close(a.key, a.why, a.status), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
