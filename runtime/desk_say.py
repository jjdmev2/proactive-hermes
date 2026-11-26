#!/usr/bin/env python3
"""Desk messages reach the owner's Bot Chat as real Bot replies, through Hermes's public cron Bot Chat delivery.

`deliver()` queues the message in say_outbox.jsonl (deduplicated by id) and kicks the plugin's no-agent cron job
"Proactive · say" with `hermes cron run <id>`. That job runs `desk_say.py next`, which prints exactly one due
message under one short instruction line; cron delivers the output with `--deliver bot-chat`, the Bot replies with
the message word for word, and the owner gets the app's normal reply notification. The job's own schedule (every
2 minutes outside quiet hours) and desk_tick's flush are the safety net when a kick fails. Non-urgent messages
wait in the outbox during quiet hours; urgent ones go at once.

Nothing here writes Hermes's state.db. The only read is `confirm`, which looks for the Bot's reply through
Hermes's public persistence reader (persistence_assembly.open_persistence_reader) to mark a message delivered.
Delivery is at most once: a message is marked "handed" before it is printed, so it is never posted twice; a
handed message whose reply is not seen within 15 minutes is reported by the health watchdog.
Tests set DESK_SAY_DISABLE_DOOR=1, which makes every kick refuse.

  desk_say.py say --key KEY [--job JOB] [--urgent] [--file F | TEXT]   # JSON receipt
  desk_say.py next                                                      # the say job: print one due message
  desk_say.py flush                                                     # kick the say job if anything is due, silent
  desk_say.py confirm                                                   # mark replies seen in Bot Chat as delivered
  desk_say.py status
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import phconf

STATE = phconf.state_dir()
OUTBOX = STATE / "say_outbox.jsonl"
SAY_LOG = STATE / "say_log.jsonl"
SAY_JOB_KEY = "say"
# What the Bot reads above the message. Live-tested: the Bot Chat reply equals the message word for word.
INSTRUCTION = "Reply with exactly the message below, word for word, nothing before or after, no tools."
KICK_TIMEOUT_S = 120
MAX_KICKS = 5          # one message per run; a burst drains in a few runs, the schedule takes the rest
CONFIRM_AFTER_S = 15 * 60


def quiet_now() -> bool:
    """Inside the owner's quiet hours? No environment switch turns this off (tests patch this function)."""
    hour = datetime.now(phconf.tz()).hour
    start, end = phconf.quiet_hours()
    return start <= hour < end if start <= end else hour >= start or hour < end


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
        fh.flush()
        os.fsync(fh.fileno())


@contextmanager
def _delivery_lock():
    # Lock an inode that the outbox rewrite never replaces; producers and the say job serialize on it.
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "say.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _last_status() -> dict[str, dict]:
    last: dict[str, dict] = {}
    for row in _lines(SAY_LOG):
        if isinstance(row.get("id"), str):
            last[row["id"]] = row
    return last


def delivered_ids() -> set[str]:
    return {mid for mid, row in _last_status().items() if row.get("status") == "delivered"}


def _settled_ids() -> set[str]:
    """Ids that must never be printed again: handed to cron (and maybe delivered) or given up."""
    return {mid for mid, row in _last_status().items()
            if row.get("status") in ("handed", "delivered", "unverified", "posting", "abandoned")}


def _pending(rows: list[dict]) -> dict[str, dict]:
    pending: dict[str, dict] = {}
    for row in rows:
        mid = row.get("id")
        if not isinstance(mid, str) or not mid or not isinstance(row.get("text"), str):
            continue
        previous = pending.get(mid, {})
        pending[mid] = {**row, "urgent": bool(previous.get("urgent") or row.get("urgent"))}
    return pending


def _due(rows: dict[str, dict], settled: set[str], quiet: bool) -> list[dict]:
    """Due messages, urgent first, then in queue order. Non-urgent ones wait during quiet hours."""
    due = [r for mid, r in rows.items() if mid not in settled and (r.get("urgent") or not quiet)]
    return sorted(due, key=lambda r: not r.get("urgent"))


def _write_outbox(rows: list[dict]) -> None:
    tmp = OUTBOX.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(OUTBOX)


def pending_due() -> int:
    with _delivery_lock():
        return len(_due(_pending(_lines(OUTBOX)), _settled_ids(), quiet_now()))


# ---------------------------------------------------------------- the say job

def say_job_id() -> str | None:
    """The plugin's own say job: jobs.json key "say" written by `hermes proactive setup`."""
    try:
        reg = json.loads((phconf.data_dir() / "jobs.json").read_text())
    except (OSError, ValueError):
        return None
    entry = reg.get(SAY_JOB_KEY) if isinstance(reg, dict) else None
    jid = entry.get("id") if isinstance(entry, dict) else entry
    return jid if isinstance(jid, str) and re.fullmatch(r"[0-9a-f]{12}", jid) else None


def _hermes() -> str:
    return shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")


def run_say_job(jid: str) -> bool:
    """`hermes cron run <id>`: the public way to run a job now. It runs to completion in the CLI (a no-agent script
    and the Bot Chat hand-off take a few seconds) and never un-pauses anything."""
    if os.environ.get("DESK_SAY_DISABLE_DOOR") == "1":
        raise RuntimeError("say job disabled (tests)")
    proc = subprocess.run([_hermes(), "cron", "run", jid], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=KICK_TIMEOUT_S,
                          env={**os.environ, "HERMES_HOME": str(phconf.hermes_home()), "NO_COLOR": "1"})
    return proc.returncode == 0


def kick() -> dict:
    """Run the say job until nothing is due (at most MAX_KICKS runs). Never raises."""
    jid = say_job_id()
    if not jid:
        return {"kick": "no say job (run `hermes proactive setup`)"}
    runs = 0
    try:
        while runs < MAX_KICKS and pending_due():
            before = len(_settled_ids())
            runs += 1
            run_say_job(jid)
            if len(_settled_ids()) == before:
                break   # the run took nothing (the scheduler holds it, or it failed): the schedule retries
    except Exception as exc:
        return {"kick": f"{type(exc).__name__}: {exc}"[:200], "runs": runs}
    return {"kick": "ok", "runs": runs}


def take_next() -> str | None:
    """The say job's step: mark the first due message handed and return it, else None."""
    with _delivery_lock():
        rows = _lines(OUTBOX)
        if not rows:
            return None
        pending = _pending(rows)
        settled = _settled_ids()
        due = _due(pending, settled, quiet_now())
        keep = [r for mid, r in pending.items() if mid not in settled]
        if not due:
            if len(keep) != len(pending):
                _write_outbox(keep)
            return None
        row = due[0]
        # Fail closed: once handed, never printed again, even if this process dies before printing.
        _append(SAY_LOG, {"status": "handed", "id": row["id"], "key": row.get("key"), "job": row.get("job"),
                          "text": row["text"], "at": now_iso(), "epoch": time.time()})
        _write_outbox([r for r in keep if r["id"] != row["id"]])
        return row["text"]


def job_output(text: str) -> str:
    return f"{INSTRUCTION}\n\n{text}"


# ---------------------------------------------------------------- producers

def deliver(text: str, key: str, job: str = "", urgent: bool = False, kick_now: bool = True) -> dict:
    """Queue TEXT for Bot Chat and kick the say job. Non-urgent messages wait in the outbox during quiet hours."""
    text = text.strip()
    if not text:
        return {"status": "empty"}
    mid = msg_id(key, text)
    with _delivery_lock():
        if mid in _settled_ids():
            return {"status": "duplicate", "id": mid}
        if mid not in _pending(_lines(OUTBOX)):
            _append(OUTBOX, {"id": mid, "key": key, "job": job, "text": text, "queued_at": now_iso(), "urgent": urgent})
    if not urgent and quiet_now():
        return {"status": "queued", "id": mid, "reason": "quiet hours"}
    receipt = {"status": "queued", "id": mid}
    if kick_now:
        receipt.update(kick())
        if mid in _settled_ids():
            receipt["status"] = "handed"
    return receipt


def flush() -> dict:
    """desk_tick's safety net: kick the say job when something is due."""
    return kick() if pending_due() else {"kick": "nothing due"}


# ---------------------------------------------------------------- confirmation (read only, public reader)

def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content or "")


def bot_chat_replies(home: Path | None = None) -> list[str] | None:
    """The Bot's replies in the profile's Bot Chat, newest last; None when the store or the chat is missing."""
    root = phconf.agent_root()   # a plain `python3` run (an agent's terminal) finds Hermes here
    if root.exists() and str(root) not in sys.path:
        sys.path.append(str(root))
    from persistence_assembly import open_persistence_reader, profile_scope, store_exists
    home = home or phconf.hermes_home()
    if not store_exists(profile_scope(home=home)):
        return None
    with open_persistence_reader(home) as store:
        row = store.sessions.get_by_title("Bot Chat")
        if row is None:
            return None
        tip = store.lineage.tip(row.session_id)
        return [_content_text(m.content).strip() for m in store.messages.read_conversation(tip)
                if getattr(m, "role", "") == "assistant"]


def confirm(replies=None) -> int:
    """Mark handed messages delivered once the Bot's reply with the same text is in Bot Chat. Returns how many."""
    handed = [r for r in _last_status().values() if r.get("status") == "handed" and isinstance(r.get("text"), str)]
    if not handed:
        return 0
    try:
        seen = set((replies if replies is not None else bot_chat_replies()) or [])
    except Exception:
        return 0
    done = 0
    with _delivery_lock():
        for row in handed:
            if row["text"].strip() in seen:
                _append(SAY_LOG, {"status": "delivered", "id": row["id"], "key": row.get("key"), "at": now_iso()})
                done += 1
    return done


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("say")
    s.add_argument("--key", required=True)
    s.add_argument("--job", default="manual")
    s.add_argument("--file")
    s.add_argument("--urgent", action="store_true", help="P0 only: deliver even during quiet hours")
    s.add_argument("text", nargs="?")
    for name in ("next", "flush", "confirm", "status"):
        sub.add_parser(name)
    a = p.parse_args(argv)
    if a.cmd == "say":
        text = Path(a.file).read_text() if a.file else (a.text if a.text is not None else sys.stdin.read())
        print(json.dumps(deliver(text, a.key, a.job, urgent=a.urgent), ensure_ascii=False))
    elif a.cmd == "next":
        confirm()
        text = take_next()
        if text:
            print(job_output(text))   # cron delivers this to Bot Chat; nothing printed = no delivery
    elif a.cmd == "confirm":
        confirm()
    elif a.cmd == "status":
        print(json.dumps({"queued": len(_pending(_lines(OUTBOX))), "delivered": len(delivered_ids())}))
    else:
        flush()   # silent: a cron no-agent job prints nothing
    return 0


if __name__ == "__main__":
    sys.exit(main())
