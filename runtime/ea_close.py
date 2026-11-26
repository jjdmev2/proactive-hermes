#!/usr/bin/env python3
"""One door to finish a desk item everywhere: ledger, promise, goal and the phone reminder.

Use it whenever the owner says an item is done, removed, cancelled, old or not needed, in any chat,
and whenever a desk job finds proof it is done. Without it, deleting the calendar event or
answering in chat leaves the desk's records open: the phone reminder stays and the item can
be raised again.

  ea_close.py --key KEY --why-file PATH [--evidence-file PATH] [--status dropped|done]
  ea_close.py --key KEY --why "owner: old invite, remove"    # fixed text you wrote yourself only
  ea_close.py --find TEXT        # list open items whose key/title contains TEXT

Text that came from mail, chat, a calendar or the web never goes on the command line: write it to a file
with the write_file tool and pass `--why-file` / `--evidence-file` (`-` reads stdin). `--why` is the owner's
words or the reason, `--evidence` is the proof found; either one alone is enough, and each defaults to the other.

A key that exists in no desk record (ledger, promises, goals, phone outbox, said, look-ahead) is not closed:
the answer is {"status": "unknown_key"} and nothing is written. Closing a key that recurs (a monthly bill)
records when it was closed and what it said; ea_desk lets it speak again once its text changes after 7 days.

Also deletes the item's calendar alert ('ea:alert:<key>', see ea_reminders.py) when the desk's records show
one exists, through `ea_calendar_block.py delete`, which only touches events Proactive Hermes created.

Ticking off the phone reminder and deleting the alert are cleanup: each needs `cleanup` in the automatic authority.
A refused one is not dropped: the records still close, and the answer lists what remains ("phone reminder",
"calendar alert") under "ask" (cleanup is ask first: offer to remove it and, on a yes, do it with Hermes's own phone
or calendar tools) or "never" (just tell the owner what is still there).

Never deletes anything on the phone or calendar the desk did not create. Prints JSON.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import ea_desk
import ea_reminders as reminders
import phconf

LEDGER = reminders.LEDGER
PROMISES = reminders.PROMISES
GOALS = reminders.GOALS
SAID = "said.json"
LOOKAHEAD = "lookahead.json"
TEXT_CHARS = 400  # same clip as the desk gate applies to an item's text


def now() -> str:
    return datetime.now(phconf.tz()).isoformat(timespec="seconds")


goal_rows = reminders.goal_rows


def ledger_lock():
    """The one actions.json lock (ea_desk.ledger_lock) for the ledger this script writes."""
    return ea_desk.ledger_lock(lambda: phconf.state_dir() / LEDGER)


def records(name: str) -> dict:
    data = reminders.read_records(name, {})
    return data if isinstance(data, dict) else {}


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


def known_key(key: str) -> bool:
    """Is KEY in any desk record? A typo must not become a closed row that silences nothing real."""
    ledger, promises, outbox = records(LEDGER), records(PROMISES), records(reminders.OUTBOX)
    said, lookahead = records(SAID), records(LOOKAHEAD).get("items")
    if any(key in store for store in (ledger, promises, outbox, said)):
        return True
    if isinstance(lookahead, dict) and key in lookahead:
        return True
    return any(isinstance(g, dict) and g.get("key") == key for g in goal_rows(reminders.read_records(GOALS, {})))


def field(store, key: str, name: str):
    row = store.get(key) if isinstance(store, dict) else None
    return row.get(name) if isinstance(row, dict) else None


def item_text(key: str) -> str:
    """What the item said, for the desk to recognise a recurrence: the last text spoken, else the title."""
    promises, outbox, lookahead = records(PROMISES), records(reminders.OUTBOX), records(LOOKAHEAD).get("items")
    candidates = [field(records(SAID), key, "text"), field(promises, key, "what"), field(promises, key, "title"),
                  field(outbox, key, "title"), field(lookahead, key, "title")]
    candidates += [g.get("title") for g in goal_rows(reminders.read_records(GOALS, {}))
                   if isinstance(g, dict) and g.get("key") == key]
    for text in candidates:
        if isinstance(text, str) and text.strip():
            return " ".join(text.split())[:TEXT_CHARS]
    return ""


def close_ledger(key: str, why: str, status: str, evidence: str | None = None, stamp: str | None = None,
                 text: str = "") -> bool:
    led = reminders.load(LEDGER, {})
    row = led.get(key)
    row = row if isinstance(row, dict) else {}
    stamp = stamp or now()
    row.update(status=status, closed=stamp, closed_at=stamp, closed_text=text, evidence=evidence or why)
    if why and why != row["evidence"]:
        row["why"] = why
    led[key] = row
    reminders.save(LEDGER, led)
    return True


def close_promise(key: str, why: str, status: str, evidence: str | None = None, stamp: str | None = None,
                  text: str = "") -> bool:
    promises = reminders.load(PROMISES, {})
    row = promises.get(key)
    if not (isinstance(row, dict) and row.get("status") == "open"):
        return False
    row.update(status=status, closed_at=stamp or now(), evidence=evidence or why)
    reminders.save(PROMISES, promises)
    return True


def close_goal(key: str, why: str, status: str, evidence: str | None = None, stamp: str | None = None,
               text: str = "") -> bool:
    data = reminders.load(GOALS, {})
    hit = False
    for g in goal_rows(data):
        if isinstance(g, dict) and g.get("key") == key and g.get("status") == "open":
            g.update(status=status, closed=stamp or now(), evidence=evidence or why)
            hit = True
    if hit:
        reminders.save(GOALS, data)
    return hit


def close(key: str, why: str = "", status: str = "done", evidence: str | None = None) -> dict:
    why, evidence = why or evidence or "", evidence or why or ""
    touched, errors = [], {}
    # Known-key check and every record write share the actions.json lock with reminders.ledger_set and the
    # desk's own ledger commands. The phone bridge runs after it is released.
    with ledger_lock():
        if not known_key(key):
            return {"status": "unknown_key"}
        stamp, text = now(), item_text(key)
        # Each record is independent: one unreadable file must not leave the others open.
        for label, step in (("ledger", close_ledger), ("promise", close_promise), ("goal", close_goal)):
            try:
                if step(key, why, status, evidence, stamp, text):
                    touched.append(label)
            except Exception as exc:
                errors[label] = f"{type(exc).__name__}: {exc}"[:200]
    try:
        rem_out = reminders.close_reminder(key, evidence)
    except Exception as exc:
        rem_out = {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:200]}
    if rem_out.get("status") in {"closing", "done"}:
        touched.append("phone reminder")
    try:
        alert_out = reminders.delete_alert(key)
    except Exception as exc:
        alert_out = {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:200]}
    if alert_out and alert_out.get("status") == "deleted":
        touched.append("calendar alert")
    # ea_desk.compose skips a closed key while its text is unchanged (and for 7 days), so it is not raised again.
    out = {"key": key, "status": status, "closed_in": touched, "reminder": rem_out}
    if alert_out is not None:
        out["calendar_alert"] = alert_out
    for what, res in (("phone reminder", rem_out), ("calendar alert", alert_out or {})):
        if res.get("status") == "not_allowed":
            out.setdefault("never" if res.get("authority") == "never" else "ask", []).append(what)
    if errors:
        out["errors"] = errors
    return out


def text_option(parser: argparse.ArgumentParser, value: str, path: str | None, name: str) -> str:
    """The text of --NAME or --NAME-file (`-` is stdin; one trailing newline is dropped)."""
    if path is None:
        return value
    if value:
        parser.error(f"use --{name} or --{name}-file, not both")
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        parser.error(f"cannot read --{name}-file: {type(exc).__name__}")
    return text[:-2] if text.endswith("\r\n") else text[:-1] if text.endswith("\n") else text


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--key")
    p.add_argument("--why", default="", help="fixed text you wrote yourself; for anything from mail, chat or web use --why-file")
    p.add_argument("--why-file", metavar="PATH", help="UTF-8 file with the reason (`-` is stdin)")
    p.add_argument("--evidence", default="", help="fixed text you wrote yourself; otherwise --evidence-file")
    p.add_argument("--evidence-file", metavar="PATH", help="UTF-8 file with the proof found (`-` is stdin)")
    p.add_argument("--status", choices=["done", "dropped"], default="done")
    p.add_argument("--find")
    a = p.parse_args()
    if a.find:
        print(json.dumps(find(a.find), ensure_ascii=False, indent=1))
        return 0
    if a.why_file == "-" and a.evidence_file == "-":
        p.error("only one of --why-file and --evidence-file can read stdin")
    why = text_option(p, a.why, a.why_file, "why")
    evidence = text_option(p, a.evidence, a.evidence_file, "evidence")
    if not a.key or not (why or evidence):
        p.error("--key and --why (or --why-file / --evidence-file) are required")
    print(json.dumps(close(a.key, why, a.status, evidence or None), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
