#!/usr/bin/env python3
"""Cheap pre-check gate for the Proactive Hermes desk.

Runs before the agent on every tick. It reads only envelope headers (no bodies,
no LLM) and prints ONE last line of JSON:

    {"wakeAgent": false}                                  -> nothing new, skip the LLM run
    {"wakeAgent": true, "context": {...}}                 -> new mail, wake the agent

The email cursor lives in the plugin state directory and is written atomically, so a
killed run never leaves a half-updated cursor. Read-only against the mailboxes:
no flag changes, no archiving, no sending.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import phconf

STATE = phconf.state_dir() / "email_cursor.json"
LOOKBACK_HOURS = 26          # first run / stale cursor window
MAX_REPORT = 20              # cap the context handed to the model
SCAN_LIMIT = 60              # envelopes read per folder, newest first (no server-side date filter)

# Common bulk-mail patterns, extended by the configured sender and subject lists.
NOISE_SENDERS = (
    "noreply@", "no-reply@", "donotreply@", "newsletter@", "news@",
    "notifications@", "notification@", "marketing@", "marketing.support@",
    "offers@", "mailing.", "updates@", "digest@", "promo", "deals@", "deals",
    "update@", "hello@news.",
)
NOISE_SUBJECTS = (
    "dmarc", "newsletter", "webinar", "% off", "sale ends", "black friday", "save up to",
    "your statement", "weekly digest", "price drop", "limited time", "holiday collection",
    "gifts for", "the wait is over", "better not miss out", "days left", "unsubscribe",
)


def himalaya(args: list[str], timeout: int = 25) -> str:
    proc = subprocess.run(
        ["himalaya", *args], capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise RuntimeError(f"himalaya failed: {' '.join(args[:4])}")
    return proc.stdout


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"accounts": {}}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(STATE.parent), prefix=".cursor-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, STATE)


SNIPPET_MAX_MESSAGES = 25


def body_snippet(account: str, folder: str, msg_id: str, chars: int = 600) -> str:
    """First part of the body, read with --preview so the mail stays unread. Empty on any error."""
    try:
        raw = himalaya(["message", "read", "--preview", "-a", account, "-f", folder, msg_id], timeout=20)
    except Exception:
        return ""
    lines = raw.splitlines()
    if "" in lines[:15]:
        lines = lines[lines.index(""):]
    return " ".join(" ".join(lines).split())[:chars]


def is_noise(sender: str, subject: str) -> bool:
    sender = (sender or "").lower()
    subject = (subject or "").lower()
    mail = phconf.CFG["mail"]
    return any(tag.strip().lower() in sender for tag in (*NOISE_SENDERS, *mail["noise_senders"])
               if tag.strip()) or any(tag.strip().lower() in subject
                                      for tag in (*NOISE_SUBJECTS, *mail["noise_subjects"]) if tag.strip())


def scan(account: str, folders: list[str], since: datetime, floors: dict) -> tuple[list[dict], list[str]]:
    """Read one account's folders.

    Floors are kept per (account, folder): message ids are only ordered inside a
    single mailbox, so one account-wide floor silently hides every folder whose
    ids happen to be lower. Folders that fail are returned to the caller so the
    cursor is not advanced over mail that was never read.
    """
    found: list[dict] = []
    failed: list[str] = []
    for folder in folders:
        floor_id = (floors.get(folder) or {}).get("floor_id")
        try:
            rows = json.loads(
                himalaya(
                    [
                        "envelope", "list", "-a", account, "-f", folder,
                        "-o", "json", "-s", str(SCAN_LIMIT),
                    ]
                )
            )
        except Exception:
            failed.append(folder)
            continue
        if not isinstance(rows, list):
            failed.append(folder)
            continue
        for row in rows:
            try:
                when = datetime.fromisoformat(row["date"])
            except Exception:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when < since:
                continue
            msg_id = str(row.get("id"))
            if floor_id and msg_id.isdigit() and floor_id.isdigit() and int(msg_id) <= int(floor_id):
                continue
            sender = (row.get("from") or {}).get("addr") or ""
            subject = row.get("subject") or ""
            if is_noise(sender, subject):
                continue
            found.append(
                {
                    "account": account,
                    "folder": folder,
                    "id": msg_id,
                    "date": when.isoformat(timespec="minutes"),
                    "from": sender,
                    "subject": subject[:120],
                }
            )
    return found, failed


def main() -> int:
    now = datetime.now(timezone.utc)
    state = load_state()
    accounts_state = state.setdefault("accounts", {})
    fresh: list[dict] = []
    gaps: list[str] = []

    for source in phconf.CFG["mail"]["himalaya_accounts"]:
        if isinstance(source, str):  # setup writes plain account names
            source = {"account": source, "folders": ["INBOX"]}
        account, folders = source["account"], source.get("folders") or ["INBOX"]
        if not folders:
            continue
        entry = accounts_state.get(account, {})
        last_seen = entry.get("last_seen")
        since = datetime.fromisoformat(last_seen) if last_seen else now - timedelta(hours=LOOKBACK_HOURS)
        floors = entry.get("folders") or {}
        found, failed = scan(account, folders, since, floors)

        fresh.extend(found)
        gaps.extend(f"{account}:{f}" for f in failed)

        # Advance the floor only for folders that were actually read.
        for folder in folders:
            if folder in failed:
                continue
            ids = [
                int(m["id"])
                for m in found
                if m["folder"] == folder and m["id"].isdigit()
            ]
            if ids:
                floors[folder] = {"floor_id": str(max(ids))}
            else:
                floors.setdefault(folder, {})

        accounts_state[account] = {"last_seen": now.isoformat(timespec="seconds"), "folders": floors}
        # A failed folder means coverage is incomplete: do not move its clock.
        if failed:
            accounts_state[account]["last_seen"] = (last_seen or (now - timedelta(hours=LOOKBACK_HOURS)).isoformat(timespec="seconds"))

    # 48h pruning: keep the state file small and the context clean.
    recent = [r for r in state.get("recent", []) if isinstance(r, dict)]
    recent.extend(
        {"id": f"{m['account']}:{m['folder']}:{m['id']}", "ts": m["date"]} for m in fresh
    )
    cutoff = now - timedelta(hours=48)
    pruned: list[dict] = []
    for row in recent:
        try:
            if datetime.fromisoformat(row["ts"]) >= cutoff:
                pruned.append(row)
        except Exception:
            continue
    state["recent"] = pruned[-400:]

    save_state(state)

    if not fresh and not gaps:
        print(json.dumps({"wakeAgent": False}), flush=True)
        return 0

    if not fresh and gaps:
        # Fail open: a folder we could not read may hold the one message that mattered.
        print(
            json.dumps(
                {
                    "wakeAgent": True,
                    "context": {
                        "new_mail": 0,
                        "gate_gaps": gaps[:12],
                        "note": "Folders failed to read this tick. Coverage is partial; the cursor was held for them.",
                    },
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0

    fresh.sort(key=lambda m: m["date"], reverse=True)
    # Fast first filter (OpenAI Decisions API): drop mail the owner almost surely does not need.
    # Headers only. Fail open: no key or any error keeps every message.
    triage = {"used": False}
    try:
        import ea_decide
        if ea_decide.api_key():
            for m in fresh[:SNIPPET_MAX_MESSAGES]:
                m["snippet"] = body_snippet(m["account"], m["folder"], m["id"], ea_decide.SNIPPET_CHARS)
        fresh, _dropped, triage = ea_decide.triage_mail(fresh)
    except Exception as exc:  # never let the filter hide mail
        triage = {"used": False, "reason": type(exc).__name__}
    if not fresh and not gaps:
        print(json.dumps({"wakeAgent": False, "triage": triage}), flush=True)
        return 0
    context = {
        "new_mail": len(fresh),
        "oldest": fresh[-1]["date"],
        "newest": fresh[0]["date"],
        "messages": fresh[:MAX_REPORT],
    }
    if gaps:
        context["gate_gaps"] = gaps[:12]
    if triage.get("used"):
        context["triage"] = triage
    print(
        json.dumps({"wakeAgent": True, "context": context}, ensure_ascii=False),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # fail open: never hide a broken gate
        print(json.dumps({"wakeAgent": True, "context": {"gate_error": str(exc)}}))
        sys.exit(0)
