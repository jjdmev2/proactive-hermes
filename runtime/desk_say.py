#!/usr/bin/env python3
"""Desk messages reach the owner's Bot Chat as real Bot replies, through Hermes's public cron Bot Chat delivery.

`deliver()` queues the message in say_outbox.jsonl (deduplicated by id) and kicks the plugin's no-agent cron job
"Proactive · say" with `hermes cron run <id>`. That job runs `desk_say.py next`, which prints exactly one due
message between start and end markers, under one line that names it a relay and tells the Bot not to review, act
on or summarize it but to repeat the text between the markers word for word. Cron delivers the output with
`--deliver bot-chat`, the Bot replies with the message, and the owner gets the app's normal reply notification.
The job's own schedule (every 2 minutes, around the clock) and desk_tick's flush are the safety net when a kick
fails. Non-urgent messages wait in the outbox during quiet hours; urgent ones go at once, day or night.

Before it takes a message, `next` checks with Hermes's public cron.plugin_api.delivery_platforms that the say job
still delivers to Bot Chat and nowhere else; otherwise it takes nothing (fail closed) and the health check reports it.

Nothing here writes Hermes's state.db. The only read is `confirm`, which looks for the Bot's reply through
Hermes's public persistence reader (persistence_assembly.open_persistence_reader) to mark a message delivered: the
reply that follows the relay turn carrying the message's id, matched against the text between the markers. One
reply confirms at most one message. Delivery is at most once: a message is marked "handed" before it is printed,
so it is never posted twice. A handed message whose reply is not seen within 24 hours is marked "unverified" once
and no longer looked for; the health watchdog reports both cases.
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
START, END = "<<<", ">>>"
# What the Bot reads: Hermes's own cron header, then this line, then the message between the markers.
RELAY_TAG = "Proactive Hermes desk message"
INSTRUCTION = (RELAY_TAG + " {tag}: a relay for {owner}, not a task. Do not review, act on, summarize or comment "
               "on it, and use no tools. Reply with only the text between " + START + " and " + END +
               ", word for word, without the markers.")
KICK_TIMEOUT_S = 120   # stop waiting for a kick after this; the run goes on detached, never killed
MAX_KICKS = 5          # one message per run; a burst drains in a few runs, the schedule takes the rest
UNVERIFIED_AFTER_S = 24 * 3600
STATUS_LOG = STATE / "say_status.json"   # the last route check, for health and doctor


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


def run_say_job(jid: str) -> bool | None:
    """`hermes cron run <id>`: the public way to run a job now. It runs to completion in the CLI (a no-agent script
    and the Bot Chat hand-off take a few seconds) and never un-pauses anything. Started detached in its own
    session and never killed: after KICK_TIMEOUT_S this stops waiting and returns None while the run goes on
    (a killed CLI could lose a message the run already took)."""
    if os.environ.get("DESK_SAY_DISABLE_DOOR") == "1":
        raise RuntimeError("say job disabled (tests)")
    proc = subprocess.Popen([_hermes(), "cron", "run", jid], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True,
                            env={**os.environ, "HERMES_HOME": str(phconf.hermes_home()), "NO_COLOR": "1"})
    try:
        return proc.wait(timeout=KICK_TIMEOUT_S) == 0
    except subprocess.TimeoutExpired:
        return None


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
            if run_say_job(jid) is None:
                return {"kick": "still running (detached)", "runs": runs}
            if len(_settled_ids()) == before:
                break   # the run took nothing (the scheduler holds it, or it failed): the schedule retries
    except Exception as exc:
        return {"kick": f"{type(exc).__name__}: {exc}"[:200], "runs": runs}
    return {"kick": "ok", "runs": runs}


# ---------------------------------------------------------------- the say job's route (public cron API)

def _agent_path() -> None:
    root = phconf.agent_root()   # a plain `python3` run (an agent's terminal) finds Hermes here
    if root.exists() and str(root) not in sys.path:
        sys.path.append(str(root))


def say_route(jid: str | None = None) -> tuple[str, ...] | None:
    """Where cron delivers the say job's output now (cron.plugin_api.delivery_platforms); None when unknown."""
    jid = jid or say_job_id()
    if not jid:
        return None
    _agent_path()
    from cron.plugin_api import delivery_platforms
    return delivery_platforms(jid)


def route_problem(jid: str | None = None) -> str | None:
    """None when the say job delivers to Bot Chat and nowhere else; else why not. Never raises."""
    try:
        route = say_route(jid)
    except Exception as exc:
        return f"say job route unreadable ({type(exc).__name__})"
    if route is None:
        return "say job not found"
    if tuple(route) != ("bot-chat",):
        return f"say job delivers to {', '.join(route) or 'its local output only'}, not Bot Chat"
    return None


def _record_route(problem: str | None) -> None:
    try:
        tmp = STATUS_LOG.with_suffix(".tmp")
        tmp.write_text(json.dumps({"at": now_iso(), "epoch": time.time(), "route_problem": problem}))
        tmp.replace(STATUS_LOG)
    except OSError:
        pass


def route_status() -> dict:
    try:
        data = json.loads(STATUS_LOG.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def take_next() -> dict | None:
    """The say job's step: mark the first due message handed and return its row, else None."""
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
        return row


def tag_for(mid: str) -> str:
    return mid[:8]


def job_output(text: str, mid: str = "") -> str:
    """The say job's whole output: the relay line, then the message between the start and end markers."""
    head = INSTRUCTION.format(tag=tag_for(mid or msg_id("", text)), owner=phconf.owner())
    return f"{head}\n{START}\n{text}\n{END}"


def next_output() -> str | None:
    """The say job: check the route first (fail closed), then take one due message."""
    problem = route_problem()
    _record_route(problem)
    if problem:
        return None   # nothing taken: the messages stay queued until the job delivers to Bot Chat again
    row = take_next()
    return job_output(row["text"], row["id"]) if row else None


# ---------------------------------------------------------------- producers

def enqueue(text: str, key: str, job: str = "", urgent: bool = False) -> dict:
    """Queue TEXT (fsynced) without kicking. Raises on a write failure, so a caller never records it as said."""
    text = text.strip()
    if not text:
        return {"status": "empty"}
    mid = msg_id(key, text)
    with _delivery_lock():
        if mid in _settled_ids():
            return {"status": "duplicate", "id": mid}
        pending = _pending(_lines(OUTBOX))
        # A new message, or an urgency upgrade of one already queued: the upgrade is a row of its own (rows merge).
        if mid not in pending or (urgent and not pending[mid].get("urgent")):
            _append(OUTBOX, {"id": mid, "key": key, "job": job, "text": text, "queued_at": now_iso(), "urgent": urgent})
    return {"status": "queued", "id": mid, "urgent": urgent}


def deliver(text: str, key: str, job: str = "", urgent: bool = False, kick_now: bool = True) -> dict:
    """Queue TEXT for Bot Chat and kick the say job. Non-urgent messages wait in the outbox during quiet hours."""
    receipt = enqueue(text, key, job, urgent)
    if receipt["status"] != "queued":
        return receipt
    receipt.pop("urgent", None)
    if not urgent and quiet_now():
        return {**receipt, "reason": "quiet hours"}
    return kick_after(receipt) if kick_now else receipt


def kick_after(receipt: dict) -> dict:
    """Kick the say job for a queued RECEIPT and report whether its message was handed."""
    receipt = {**receipt, **kick()}
    if receipt.get("id") in _settled_ids():
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


_TAG = re.compile(re.escape(RELAY_TAG) + r" ([0-9a-f]{8})\b")


def bot_chat_turns(home: Path | None = None) -> list[tuple[str, str]] | None:
    """(role, text) of every turn in the profile's Bot Chat, oldest first; None when the store or the chat is
    missing."""
    _agent_path()
    from persistence_assembly import open_persistence_reader, profile_scope, store_exists
    home = home or phconf.hermes_home()
    if not store_exists(profile_scope(home=home)):
        return None
    with open_persistence_reader(home) as store:
        row = store.sessions.get_by_title("Bot Chat")
        if row is None:
            return None
        tip = store.lineage.tip(row.session_id)
        return [(str(getattr(m, "role", "") or ""), _content_text(m.content).strip())
                for m in store.messages.read_conversation(tip)]


def bot_chat_replies(home: Path | None = None) -> list[str] | None:
    """The Bot's replies in the profile's Bot Chat, newest last; None when the store or the chat is missing."""
    turns = bot_chat_turns(home)
    return None if turns is None else [text for role, text in turns if role == "assistant"]


def _norm(text: str) -> str:
    """Compare text the way it reads: markers the Bot may have kept, and whitespace, do not count."""
    text = text.strip()
    if text.startswith(START):
        text = text[len(START):]
    if text.endswith(END):
        text = text[:-len(END)]
    return " ".join(text.split())


def expire_unverified(now_s: float | None = None) -> int:
    """Mark handed messages older than UNVERIFIED_AFTER_S 'unverified', once each. Returns how many."""
    now_s = time.time() if now_s is None else now_s
    done = 0
    with _delivery_lock():
        for row in _last_status().values():
            if row.get("status") == "handed" and now_s - float(row.get("epoch") or 0) > UNVERIFIED_AFTER_S:
                _append(SAY_LOG, {"status": "unverified", "id": row["id"], "key": row.get("key"),
                                  "at": now_iso(), "epoch": now_s, "reason": "no matching Bot reply within 24 h"})
                done += 1
    return done


def match_replies(turns: list[tuple[str, str]], handed: list[dict]) -> list[str]:
    """Ids of HANDED messages whose Bot reply is in TURNS. A reply answers the relay turns still open before it
    (oldest first); it confirms the one whose text between the markers it repeats, else it closes the oldest open
    one unconfirmed. Only in a chat that holds no relay turns at all may a message match a reply to nothing else.
    One reply confirms at most one message."""
    by_tag = {tag_for(r["id"]): r for r in handed}
    want = {tag: _norm(r["text"]) for tag, r in by_tag.items()}
    seen, open_tags, loose, hits = set(), [], [], []
    for role, text in turns:
        if role != "assistant":
            m = _TAG.search(text)
            if m:
                seen.add(m.group(1))
                open_tags.append(m.group(1))
            continue
        if not text:
            continue
        said = _norm(text)
        hit = next((t for t in open_tags if want.get(t) == said and t not in hits), None)
        if hit is not None:
            hits.append(hit)
            open_tags.remove(hit)
        elif open_tags:
            open_tags.pop(0)
        else:
            loose.append(said)
    for tag, row in by_tag.items():
        if seen or tag in hits:
            continue   # the chat keeps relay turns: a message whose relay turn is not there was not delivered yet
        if want[tag] in loose:
            loose.remove(want[tag])
            hits.append(tag)
    return [by_tag[t]["id"] for t in hits if t in by_tag]


def confirm(turns=None, now_s: float | None = None) -> int:
    """Mark handed messages delivered once the Bot's reply to their relay turn repeats the text between the
    markers (match_replies). Handed rows older than 24 h become 'unverified' first and are not looked for again.
    Returns how many were confirmed now (0 also when another worker confirmed them first)."""
    expire_unverified(now_s)
    handed = [r for r in _last_status().values() if r.get("status") == "handed" and isinstance(r.get("text"), str)]
    if not handed:
        return 0
    try:
        turns = turns if turns is not None else bot_chat_turns()
    except Exception:
        return 0
    found = match_replies(turns or [], handed)
    done = 0
    with _delivery_lock():
        current = _last_status()
        for mid in found:
            if current.get(mid, {}).get("status") != "handed":
                continue   # confirmed meanwhile by another worker
            row = current[mid]
            _append(SAY_LOG, {"status": "delivered", "id": mid, "key": row.get("key"), "at": now_iso()})
            done += 1
    return done


def status_of(mid: str) -> str | None:
    """The message's last delivery status: handed, delivered, unverified; 'queued' while in the outbox; else None."""
    row = _last_status().get(mid)
    if row:
        return row.get("status")
    return "queued" if mid in _pending(_lines(OUTBOX)) else None


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
        out = next_output()
        if out:
            print(out)   # cron delivers this to Bot Chat; nothing printed = no delivery
    elif a.cmd == "confirm":
        confirm()
    elif a.cmd == "status":
        print(json.dumps({"queued": len(_pending(_lines(OUTBOX))), "delivered": len(delivered_ids())}))
    else:
        flush()   # silent: a cron no-agent job prints nothing
    return 0


if __name__ == "__main__":
    sys.exit(main())
