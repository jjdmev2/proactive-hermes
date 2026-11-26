#!/usr/bin/env python3
"""Post desk updates in Bot Chat as real messages, through the gateway's canonical Bot Chat door.

The text is admitted as an internal turn ("DESK RELAY <id>"); the Bot Chat agent replies with it
verbatim. The owner sees a normal assistant message, live in the app and in the agent's context, and
gets the app's own reply notification. There is no separate push. The relay input card is hidden
afterwards (display_metadata.model_only, display-only). Never insert chat messages into state.db
directly: the live app and the gateway's cached agent would not see them.

If the owner is mid-turn in Bot Chat (turn lease held) or the door fails before admission, the message
is queued in say_outbox.jsonl and `flush` retries it (desk_tick.py, every 10 minutes). The door is
idempotent on the delivery id (sha256 of the message id), so a retry cannot double-post.
Tests set DESK_SAY_DISABLE_DOOR=1, which makes any real door call raise.

  desk_say.py say --key KEY [--job JOB] [--no-push] [--notes TEXT] [--file F | TEXT]   # JSON receipt
  desk_say.py flush                                         # retry queued messages, silent
  desk_say.py status
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import phconf

PROFILE = phconf.hermes_home()
INSTALL_HOME = PROFILE.parent.parent if PROFILE.parent.name == "profiles" else PROFILE
AGENT = INSTALL_HOME / "hermes-agent"
STATE = phconf.state_dir()
OUTBOX = STATE / "say_outbox.jsonl"
SAY_LOG = STATE / "say_log.jsonl"
STATE_DB = PROFILE / "state.db"
LEASE_WAIT_S = 90


def quiet_now() -> bool:
    """Inside the owner's quiet hours? No environment switch turns this off (tests patch this function)."""
    hour = datetime.now(phconf.tz()).hour
    start, end = phconf.quiet_hours()
    return start <= hour < end if start <= end else hour >= start or hour < end

MAX_ATTEMPTS = 30


def now_iso() -> str:
    return datetime.now(phconf.tz()).isoformat(timespec="seconds")


def msg_id(key: str, text: str) -> str:
    return hashlib.sha256(f"{key}\n{text}".encode()).hexdigest()[:24]


def _lines(path: Path) -> list[dict]:
    out = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except FileNotFoundError:
        pass
    return out


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if row.get("status") == "posting":
            fh.flush()
            os.fsync(fh.fileno())


@contextmanager
def _delivery_lock():
    # Lock an inode that flush never replaces; a producer must not race its outbox rewrite.
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "say.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def delivered_ids() -> set[str]:
    return {r.get("id") for r in _lines(SAY_LOG) if r.get("status") == "delivered"}


def _settled_ids() -> set[str]:
    ids = set()
    for row in _lines(SAY_LOG):
        mid, status = row.get("id"), row.get("status")
        if not isinstance(mid, str):
            continue
        if status in ("posting", "delivered", "unverified"):
            ids.add(mid)
        elif status == "retryable":
            ids.discard(mid)
    return ids


def _agent_env() -> None:
    os.environ["HERMES_HOME"] = str(PROFILE)
    if str(AGENT) not in sys.path:
        sys.path.insert(0, str(AGENT))


def bot_chat_tip() -> str | None:
    """The live Bot Chat session id (the compression-lineage tip of the chat titled 'Bot Chat')."""
    _agent_env()
    from tools.bot_live_delivery import _bot_chat_tip
    return _bot_chat_tip(PROFILE)


def _lineage(db: sqlite3.Connection, sid: str) -> set[str]:
    seen, cur = set(), sid
    while cur and cur not in seen:
        seen.add(cur)
        row = db.execute("SELECT parent_session_id FROM sessions WHERE id=?", (cur,)).fetchone()
        cur = row[0] if row else None
    return seen


def turn_active(sid: str) -> bool:
    db = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=10)
    try:
        ids = _lineage(db, sid)
        q = "SELECT 1 FROM session_turn_leases WHERE expires_at > ? AND conversation_id IN (%s) LIMIT 1"
        return db.execute(q % ",".join("?" * len(ids)), (time.time(), *ids)).fetchone() is not None
    finally:
        db.close()


RELAY_PREFIX = "DESK RELAY"


def relay_message(mid: str, text: str, notes: str = "") -> str:
    """The internal input the Bot Chat agent receives; its verbatim reply is what the owner sees."""
    owner = phconf.owner()
    context = f"NOTES FOR YOU, never shown to {owner} and never repeated:\n{notes}\n" if notes else ""
    return (f"{RELAY_PREFIX} {mid[:8]}. Reply to {owner} with exactly the text between the markers, word for "
            f"word, nothing before or after, no tools.\n{context}<<<\n{text}\n>>>")


def door_deliver(mid: str, text: str, notes: str = "") -> dict:
    """Admit the message as a real Bot Chat turn through the profile's gateway (canonical door).
    The agent's reply is a normal assistant message: live in the app, in context, and notified by
    the app's own reply notification. No separate push."""
    if os.environ.get("DESK_SAY_DISABLE_DOOR") == "1":
        raise RuntimeError("door disabled (tests)")
    import hashlib as _h
    _agent_env()
    from tools.bot_live_delivery import authority_delivery
    did = _h.sha256(mid.encode()).hexdigest()[:32]
    return authority_delivery(PROFILE, {"id": did, "message": relay_message(mid, text, notes), "profile": phconf.profile_name(PROFILE)})


def hide_relay_cards(wait_s: float = 0) -> int:
    """Hide DESK RELAY input cards in Bot Chat once their reply exists (display_metadata.model_only:
    display-only, stripped before the provider call, so the agent's context is unchanged)."""
    if os.environ.get("DESK_SAY_DISABLE_DOOR") == "1":
        return 0
    return _hide_relay_cards(wait_s)


def _hide_relay_cards(wait_s: float) -> int:
    deadline = time.time() + wait_s
    hidden = 0
    while True:
        db = sqlite3.connect(STATE_DB, timeout=10)
        try:
            sid = bot_chat_tip()
            ids = _lineage(db, sid) if sid else set()
            if ids:
                q = ("SELECT m.id, m.display_metadata FROM messages m WHERE m.session_id IN (%s) AND m.role='user' "
                     "AND m.content LIKE ? AND COALESCE(json_extract(m.display_metadata,'$.model_only'),0)=0 "
                     "AND EXISTS (SELECT 1 FROM messages r WHERE r.session_id=m.session_id AND r.id>m.id "
                     "AND r.role='assistant' AND TRIM(COALESCE(r.content,''))<>'')")
                rows = db.execute(q % ",".join("?" * len(ids)), (*ids, RELAY_PREFIX + "%")).fetchall()
                for rid, meta in rows:
                    try:
                        m = json.loads(meta) if meta else {}
                    except ValueError:
                        m = {}
                    m["model_only"] = True
                    db.execute("UPDATE messages SET display_metadata=? WHERE id=?", (json.dumps(m), rid))
                    hidden += 1
                db.commit()
        finally:
            db.close()
        if hidden or time.time() >= deadline:
            return hidden
        time.sleep(5)


def _queue(mid: str, key: str, job: str, text: str, reason: str, attempts: int = 0,
           push_enabled: bool = True, urgent: bool = False, notes: str = "") -> dict:
    row = {"id": mid, "key": key, "job": job, "text": text, "notes": notes, "queued_at": now_iso(),
           "reason": reason[:200], "attempts": attempts, "push": push_enabled, "urgent": urgent}
    _append(OUTBOX, row)
    return {"status": "queued", "id": mid, "reason": reason[:200]}


def _pending(rows: list[dict]) -> dict[str, dict]:
    pending: dict[str, dict] = {}
    for row in rows:
        mid = row.get("id")
        if not isinstance(mid, str) or not mid or not isinstance(row.get("text"), str):
            continue
        previous = pending.get(mid, {})
        attempts = row.get("attempts", 0)
        pending[mid] = {
            **row,
            "attempts": max(previous.get("attempts", 0), attempts if isinstance(attempts, int) else 0),
            "push": previous.get("push") is not False and row.get("push") is not False,
        }
    return pending


def _chat_tip() -> str:
    sid = bot_chat_tip()
    if not sid:
        raise RuntimeError("Bot Chat session not found")
    return sid


def _send(mid: str, key: str, job: str, text: str, sid: str, push_enabled: bool, notes: str) -> dict:
    receipt = {"status": "posting", "id": mid, "key": key, "job": job, "session_id": sid,
               "at": now_iso()}
    # A process may die between the chat append and the final receipt. Fail closed in that gap.
    _append(SAY_LOG, receipt)
    try:
        answer = door_deliver(mid, text, notes)
    except Exception as exc:
        # PreDialError (nothing admitted) is safe to retry; anything after the dial may be admitted,
        # and the door is idempotent on the delivery id, so a retry cannot double-post.
        _append(SAY_LOG, {**receipt, "status": "retryable"})
        raise
    status = str((answer or {}).get("status") or "")
    if status in ("refused", "failed", "cancelled") or (answer or {}).get("refused"):
        receipt.update(status="unverified", push="door", reason=f"door {status}: {(answer or {}).get('reason')}"[:200])
    else:
        receipt.update(status="delivered", row_id=None, push="door", admission_id=(answer or {}).get("admission_id"),
                       door_status=status, session_id=(answer or {}).get("session_id") or sid)
        try:
            hide_relay_cards(wait_s=90)
        except Exception:
            pass  # desk_tick hides it on its next run
    try:
        _append(SAY_LOG, receipt)
    except OSError as exc:
        # The reservation still blocks retries; report the missing receipt without queueing.
        receipt["log_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return receipt


def deliver(text: str, key: str, job: str = "", wait_s: int = LEASE_WAIT_S, push_enabled: bool = True,
            urgent: bool = False, notes: str = "") -> dict:
    """Post in Bot Chat. Non-urgent messages during quiet hours wait in the outbox."""
    text = text.strip()
    if not text:
        return {"status": "empty"}
    mid = msg_id(key, text)
    if not urgent and quiet_now():
        with _delivery_lock():
            if mid in _settled_ids():
                return {"status": "duplicate", "id": mid}
            pending = _pending(_lines(OUTBOX)).get(mid, {})
            if pending:
                return {"status": "queued", "id": mid, "reason": "quiet hours"}
            return _queue(mid, key, job, text, "quiet hours", push_enabled=push_enabled, notes=notes)
    deadline = time.monotonic() + wait_s
    while True:
        with _delivery_lock():
            if mid in _settled_ids():
                return {"status": "duplicate", "id": mid}
            pending = _pending(_lines(OUTBOX)).get(mid, {})
            notify = push_enabled and pending.get("push") is not False
            relay_notes = notes or pending.get("notes", "")
            try:
                sid = _chat_tip()
                if not turn_active(sid):
                    return _send(mid, key, job, text, sid, notify, relay_notes)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"{phconf.owner()} is mid-turn in Bot Chat")
            except Exception as exc:
                return _queue(mid, key, job, text, f"{type(exc).__name__}: {exc}",
                              attempts=pending.get("attempts", 0), push_enabled=notify, urgent=urgent, notes=relay_notes)
        # Let flush or another producer run while waiting for the turn lease.
        time.sleep(min(5, remaining))


def flush() -> list[dict]:
    with _delivery_lock():
        rows = _lines(OUTBOX)
        if not rows:
            return []
        done = _settled_ids()
        results, keep = [], []
        for mid, r in _pending(rows).items():
            if mid in done:
                continue
            if quiet_now() and not r.get("urgent"):
                keep.append(r)  # held for the morning; not a failed attempt
                continue
            try:
                sid = _chat_tip()
                if turn_active(sid):
                    raise TimeoutError(f"{phconf.owner()} is mid-turn in Bot Chat")
                results.append(_send(mid, r.get("key", ""), r.get("job", ""), r["text"], sid, r["push"],
                                     r.get("notes", "")))
            except Exception as exc:
                attempts = r["attempts"] + 1
                if attempts < MAX_ATTEMPTS:
                    keep.append({**r, "attempts": attempts, "reason": f"{type(exc).__name__}: {exc}"[:200]})
                else:
                    _append(SAY_LOG, {"status": "abandoned", "id": mid, "key": r.get("key"), "at": now_iso()})
        tmp = OUTBOX.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(k, ensure_ascii=False) + "\n" for k in keep), encoding="utf-8")
        tmp.replace(OUTBOX)
        return results


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("say")
    s.add_argument("--key", required=True)
    s.add_argument("--job", default="manual")
    s.add_argument("--file")
    s.add_argument("--notes", default="", help="Bot Chat context, outside the visible reply")
    s.add_argument("--no-push", action="store_true", help="kept for compatibility; delivery is a real chat message")
    s.add_argument("--urgent", action="store_true", help="P0 only: deliver even during quiet hours")
    s.add_argument("text", nargs="?")
    sub.add_parser("flush")
    sub.add_parser("status")
    a = p.parse_args()
    if a.cmd == "say":
        text = Path(a.file).read_text() if a.file else (a.text if a.text is not None else sys.stdin.read())
        receipt = deliver(text, a.key, a.job, push_enabled=not a.no_push, urgent=a.urgent, notes=a.notes)
        print(json.dumps(receipt, ensure_ascii=False))
    elif a.cmd == "status":
        print(json.dumps({"queued": len(_lines(OUTBOX)), "delivered": len(delivered_ids())}))
    else:
        flush()  # silent: a cron no-agent job prints nothing
    return 0


if __name__ == "__main__":
    sys.exit(main())
