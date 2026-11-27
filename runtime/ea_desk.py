#!/usr/bin/env python3
"""Desk gate: decides what reaches the owner's Bot Chat, deterministically.

The desk jobs never write to the owner directly. They hand their candidate items to
`compose`, then end their answer with `[SILENT]`. Compose posts to Bot Chat
itself; `--print-only` previews the visible message without calling the door.
`[RESOLVE-FIRST]` on stdout asks the producer to resolve those keys and retry.

Rules enforced here, not in prompts:
  * Only kinds `decision` (the owner must choose) and `action` (the owner must do something)
    can speak. `digest` items are queued for the end-of-day brief. Anything else
    is dropped.
  * Dedupe on what was SAID: an item key that was said before stays quiet until
    its `change` text differs, or 48h pass (decisions re-surface at most daily).
  * A key closed with ea_close.py stays quiet while the item still says what was closed, and for 7 days after
    the close. After that a changed text is a new matter (a monthly bill with a stable key speaks again).
  * No daily cap. Items are ranked by importance instead: urgent, then priority p0/p1/p2, then someone waiting on the owner,
    then the soonest due date, then decisions before actions, then the oldest first.
  * Configured item limits per message and brief, best first. The rest roll over: every compose
    run takes the overflow queue first, and `flush` (desk tick) sends it on its own after
    an hour of quiet. A message that has more lined up says so. Nothing speaking is dropped
    by age silently: after 7 days it moves to the end-of-day digest.
  * During configured quiet hours, items are held for the morning brief unless
    `urgent: true`. Unchanged urgent items can re-surface after 6h.
  * Supported `act` items wait for a stored resolution for up to 20 minutes.
    Urgent items and explicit skips never wait. The tick honors quiet hours.

Usage:
  ea_desk.py compose --job NAME [--file items.json | stdin] [--brief]
  ea_desk.py flush                    # send rolled-over items after an hour of quiet (tick)
  ea_desk.py digest [--take]          # read (and clear) the end-of-day queue
  ea_desk.py held   [--take]          # items held during quiet hours
  ea_desk.py said   [--days 3]        # what was said recently
  ea_desk.py forget --key KEY         # let a key speak again (the owner reopened it)

Item shape: {"key": "stable-matter-key", "kind": "decision|action|digest",
             "text": "one line, what + why now + next step", "change": "material
             state, e.g. 'proposal open'", "urgent": false, "ref": "source",
             "priority": "p0|p1|p2" (optional), "due": "ISO time" (optional),
             "waiting": true if someone is blocked on the owner (optional)}
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

import ea_resolve
import phconf

HERE = Path(__file__).resolve().parent   # once, at start: the runtime generation this run belongs to
STATE = phconf.state_dir()
SAID = STATE / "said.json"
DIGEST = STATE / "digest_queue.jsonl"
HELD = STATE / "held_queue.jsonl"
PING_LOG = STATE / "ping_log.jsonl"
RUN_LOG = STATE / "run_log.jsonl"
LEDGER = STATE / "actions.json"
PENDING = STATE / "pending_queue.jsonl"  # desk-internal producers (health) that need the owner
RESOLVE_WAIT = STATE / "resolve_wait.jsonl"
RESOLVE_WAIT_MAX = timedelta(minutes=20)
CLAIM_CAPS = {"reminder": phconf.CFG["reminders"]["per_day"],
              "block": phconf.CFG["calendar"]["blocks_per_day"]}

SPEAKING = {"decision", "action"}
COOLDOWN = timedelta(hours=48)
DECISION_RESURFACE = timedelta(hours=20)
URGENT_COOLDOWN = timedelta(hours=6)
QUEUE_MAX_AGE_HOURS = 72          # FYI digest rows
SPEAKING_MAX_AGE_HOURS = 168      # held / overflow / pending items: a week, then digest, never silent
FLUSH_GAP = timedelta(minutes=60)  # flush waits this long after the last desk message
PRIORITY = {"p0": 0, "p1": 1, "p2": 2}


_ledger_held = threading.local()


@contextmanager
def ledger_lock(path=None):
    """One cross-process lock for every read-modify-write of actions.json (the action ledger).

    `path` is the ledger file, a callable returning it (resolved when the lock is taken, so
    `@ledger_lock(lambda: LEDGER)` can decorate a writer), or None for this module's LEDGER.
    The lock file sits next to the ledger as `<ledger name>.lock`. Reentrant per thread, so a writer
    called inside the block does not deadlock. Never hold it across a network call.
    """
    target = path() if callable(path) else path
    target = Path(target) if target is not None else LEDGER
    target.parent.mkdir(parents=True, exist_ok=True)
    name = str(target.with_name(target.name + ".lock"))
    held = _ledger_held.__dict__.setdefault("names", {})
    if held.get(name):
        held[name] += 1
        try:
            yield
        finally:
            held[name] -= 1
        return
    with open(name, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        held[name] = 1
        try:
            yield
        finally:
            held.pop(name, None)


def now() -> datetime:
    return datetime.now(phconf.tz())


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp file in the same directory: two writers never share one half-written file.
    with NamedTemporaryFile(mode="w", dir=path.parent, prefix=path.name + ".", suffix=".tmp",
                            encoding="utf-8", delete=False) as fh:
        tmp = Path(fh.name)
        try:
            fh.write(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True))
            fh.flush()
            os.fsync(fh.fileno())
            fh.close()
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


def append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_lines(path: Path) -> list[dict]:
    rows = []
    try:
        for line in path.read_text().splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return rows


def fp(item: dict) -> str:
    """Fingerprint of the material state: order- and punctuation-insensitive, so a reworded
    but unchanged `change` does not count as news."""
    import re
    basis = str(item.get("change") or item.get("text") or "").lower()
    tokens = sorted(set(re.findall(r"[a-z0-9€$.:-]+", basis)) - {"the", "a", "an", "is", "still", "and", "of", "to"})
    return hashlib.sha256(" ".join(tokens).encode()).hexdigest()[:16]


def pings_today() -> int:
    today = now().date().isoformat()
    return sum(1 for r in read_lines(PING_LOG)
               if r.get("kind") == "desk_message" and str(r.get("at", "")).startswith(today))


def last_ping_at() -> datetime | None:
    stamps = [at for r in read_lines(PING_LOG)
              if r.get("kind") == "desk_message" and (at := timestamp(r.get("at"))) is not None]
    return max(stamps) if stamps else None


def importance(it: dict, t: datetime) -> tuple:
    """Sort key, most important first. Missing fields fall back to safe defaults."""
    urgent = bool(it.get("urgent"))
    raw = str(it.get("priority") or "").strip().lower()
    raw = raw if raw.startswith("p") else f"p{raw}" if raw else ""
    prio = PRIORITY.get(raw, 0 if urgent else 1)
    due = timestamp(it.get("due"))
    due_rank = (due - t).total_seconds() if due else float("inf")
    seen = timestamp(it.get("first_seen")) or timestamp(it.get("at")) or t
    return (not urgent, prio, not it.get("waiting"), due_rank, it.get("kind") != "decision", seen)


def in_quiet(t: datetime) -> bool:
    start, end = phconf.quiet_hours()
    hour = t.astimezone(phconf.tz()).hour
    return start <= hour < end if start <= end else hour >= start or hour < end


def normalize_item(it) -> dict | None:
    if not (isinstance(it, dict) and it.get("key") and it.get("text")):
        return None
    text = it["text"]
    if not isinstance(text, str):
        return None
    # `text` is the producer's one-line synopsis, not a source field. Do not relay
    # raw fields, pasted blocks or an identifiable verbatim copy of a source field.
    raw_fields = {"subject", "body", "html", "snippet", "web_text", "source_text", "raw_text", "raw"}
    raw = [" ".join(it[field].split()).casefold() for field in raw_fields if isinstance(it.get(field), str)]
    flat = " ".join(text.split())
    if (len(text.splitlines()) > 1 or re.search(r"<[/!][a-z]", text, re.I)
            or any(value and value in flat.casefold() for value in raw)):
        flat = "Check the original source for details."
    return {**{key: value for key, value in it.items() if key not in raw_fields},
            "key": str(it["key"]).strip()[:120], "kind": str(it.get("kind") or "digest").strip().lower(),
            "text": flat[:400]}


def parse_items(raw: str) -> list[dict]:
    data = json.loads(raw or "[]")
    if isinstance(data, dict):
        data = data.get("items", [])
    if not isinstance(data, list):
        return []
    return [item for it in data if (item := normalize_item(it)) is not None]


def timestamp(value) -> datetime | None:
    if isinstance(value, str):
        try:
            result = datetime.fromisoformat(value)
        except ValueError:
            return None
        if result.tzinfo is not None:
            return result
    return None


def queue_is_fresh(row: dict, cutoff: datetime) -> bool:
    at = timestamp(row.get("at"))
    return at is None or at >= cutoff


def consume_rows(path: Path, consumed: list[dict], remaining: list[dict]) -> None:
    if consumed:
        with path.with_suffix(".consumed.jsonl").open("a") as fh:
            fh.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in consumed))
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in remaining))


def take(path: Path, max_age_hours: float = SPEAKING_MAX_AGE_HOURS) -> list[dict]:
    """Consume a queue file, dropping only rows with a known stale timestamp."""
    rows = read_lines(path)
    consume_rows(path, rows, [])
    cutoff = now() - timedelta(hours=max_age_hours)
    return [r for r in rows if queue_is_fresh(r, cutoff)]


def recent_said(cutoff: datetime) -> dict:
    said = load_json(SAID, {})
    if not isinstance(said, dict):
        return {}
    return {key: row for key, row in said.items()
            if isinstance(row, dict) and (at := timestamp(row.get("said_at"))) is not None and at > cutoff}


_MESSAGE_URL = re.compile(
    r"\b(?:[a-z][a-z0-9+.-]*://|mailto:|javascript:|data:)[^\s<>()\[\]`*]+"
    r"|(?<![\w.@-])(?:[a-z0-9-]+\.)+[a-z]{2,63}(?![\w-])"
    r"(?::\d+)?(?:[/?#][^\s<>()\[\]`*]*)?", re.IGNORECASE,
)
MAX_NOTICE_CHARS = 700


def strip_message_urls(text: str) -> str:
    # Keep sentence punctuation (and digest label colons) beside a link.
    return _MESSAGE_URL.sub(lambda m: m[0][len(m[0].rstrip(".,!?;:")):], text)


def _clean_message_text(text: str) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^\s)]+\)", r"\1", text)
    text = re.sub(r"\*+|_{2,}|`+", "", text)
    text = strip_message_urls(text)
    text = re.sub(r"#|\(\s*\)|<\s*>", "", text)
    text = re.sub(r"(?m)^[ \t]*(?:[>+•][ \t]+)+|(?<!\S)-+(?!\S)", " ", text)
    text = re.sub(r"\s*—\s*", ", ", text)
    return re.sub(r" +([.,!?;:])", r"\1", " ".join(text.split()))


def _clip_message_item(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    sentence_end = max((m.end() for m in re.finditer(r"[.?!](?= )", text[:limit + 1])), default=0)
    if sentence_end >= 60:
        return text[:sentence_end]
    word_end = text.rfind(" ", 0, limit)
    return text[:word_end if word_end != -1 else limit - 1] + "…"


def render_message(header: str | None, items: list[str], worth: list[str], more: int = 0) -> str:
    """Render display text only; selection, receipts and desk state stay in compose."""
    return render_parts(header, items, worth, more)[0]


# Jobs whose items carry a whole briefing in one line (meeting prep: who, last thread, owe, goal, ask).
LONG_LINE_JOBS = {"meeting-prep": 400}


def render_parts(header: str | None, items: list[str], worth: list[str], more: int = 0,
                 suffixes: list[str] | None = None, line_limit: int | None = None) -> tuple[str, int]:
    """Return (message, how many of `items` made it in). Items cut for length are the caller's
    to roll over, so nothing is marked said that the owner never saw."""
    heading = ""
    if header and len(header) <= 60 and header.splitlines() == [header]:
        heading = _clean_message_text(header)
        if len(heading) > 60:
            heading = ""

    count = len(items)
    if count > 1:
        heading = heading or f"Hey {phconf.owner()}, {count} things:"
    caps = phconf.CFG["caps"]
    total_chars = max(1, min(MAX_NOTICE_CHARS, caps["message_chars"]))
    line_chars = min(line_limit or caps["line_chars"], total_chars)
    lines = []
    for i, text in enumerate(items, 1):
        base = _clean_message_text(text) or "Note"
        prefix = f"{i}. " if count > 1 else ""
        suffix = suffixes[i - 1].replace("{n}", str(i)) if suffixes else ""
        if suffix:
            base = _clip_message_item(base, line_chars - len(prefix) - len(suffix) - 2)
            if not base.endswith((".", "?", "!", "\u2026")):
                base += "."   # "... by Friday. Draft ready, reply 1." not "... by Friday Draft ready, reply 1."
            base += " " + suffix
        else:
            base = _clip_message_item(base, line_chars)
        lines.append(prefix + base)

    digest = ""
    if worth:
        labels = []
        for text in worth[:3]:
            prefix = _clean_message_text(text).partition(":")[0].strip()
            label = prefix if len(prefix) <= 40 else " ".join(prefix.split()[:5])
            labels.append(_clip_message_item(label, line_chars) or "Note")
        extra = f" and {len(worth) - 3} more" if len(worth) > 3 else ""
        digest = f"Also saved: {', '.join(labels)}{extra} (ask if you want details)"

    while True:
        pending = more + count - len(lines)
        body = [heading] if heading else []
        body.extend(lines)
        if pending:
            body.append(f"+{pending} more lined up, coming next (or ask me)")
        message = "\n\n".join(part for part in ("\n".join(body), digest) if part)
        if len(message) <= total_chars:
            return message, len(lines)
        if len(lines) > 1:
            lines.pop()
        elif digest:
            digest = ""
        else:
            # A tiny custom cap should never truncate a reply number into a different one.
            return _clip_message_item(lines[0] if lines else message, total_chars), len(lines)


def is_resolvable(it: dict) -> bool:
    act = it.get("act")
    return it["kind"] in SPEAKING and isinstance(act, str) and act in ea_resolve.RESOLVABLE


def resolution_for(it: dict, t: datetime, *, bind=False) -> dict | None:
    key, item_fp = it["key"], fp(it)
    try:
        rec = ea_resolve.get(key, item_fp=item_fp, now=t)
        if bind and rec is not None:
            rec = ea_resolve.stamp_fp(key, item_fp)
            if rec.get("item_fp") != item_fp or rec.get("sent_at") or rec.get("dropped_at"):
                return None
        return rec
    except (OSError, ValueError):
        # An unreadable store must not keep an urgent or timed-out item silent.
        return None


def wait_expired(started: datetime | None, t: datetime) -> bool:
    return started is None or t.astimezone(timezone.utc) - started.astimezone(timezone.utc) >= RESOLVE_WAIT_MAX


def write_resolve_wait(rows) -> None:
    # Private, atomic snapshot. Health can read it while another process is composing.
    with NamedTemporaryFile(mode="w", dir=RESOLVE_WAIT.parent, encoding="utf-8", delete=False) as out:
        tmp = Path(out.name)
        try:
            for row in rows:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.close()
            tmp.replace(RESOLVE_WAIT)
        finally:
            tmp.unlink(missing_ok=True)


def cmd_compose(a, raw: str | None = None) -> int:
    if raw is None:
        raw = Path(a.file).read_text() if a.file else sys.stdin.read()
    try:
        items = parse_items(raw)
    except ValueError as exc:
        append(RUN_LOG, {"at": now().isoformat(timespec="seconds"), "job": a.job,
                         "error": f"bad items json: {exc}"[:200]})
        print("[SILENT]")
        return 0
    return compose(a, items)


def _notice_key(ping: dict) -> str:
    key = f"{ping['job']}:{','.join(ping['keys'])}"
    if versions := ping.get("draft_versions"):
        key += ":drafts:" + hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()
    return key


def _commit_notice(ping, said, receipt) -> None:
    updated = recent_said(now() - timedelta(days=30))
    updated.update({key: row for key, row in said.items() if key not in updated
                    or row.get("said_at", "") >= updated[key].get("said_at", "")})
    save_json(SAID, updated)
    mid = receipt.get("id")
    if not mid or not any(row.get("say_id") == mid and row.get("text") == ping["text"] for row in read_lines(PING_LOG)):
        append(PING_LOG, {**ping, "delivery": receipt.get("status"), "say_id": mid})


def _urgent_path() -> Path:
    return STATE / "urgent_enqueues.json"


def retry_urgent() -> None:
    """The */2 say job retries already-composed urgent notices without reading sources or kicking itself."""
    path = _urgent_path()
    if not path.exists():
        return
    import desk_say
    with (STATE / "compose.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not path.exists():
            return
        waiting = json.loads(path.read_text(encoding="utf-8"))
        closed = closed_keys()
        for key, row in list(waiting.items()):
            if closed.intersection(row["ping"]["keys"]):
                del waiting[key]
                continue
            try:
                ping = row["ping"]
                receipt = desk_say.enqueue(ping["text"], key=key, job=ping["job"], urgent=True)
                _commit_notice(ping, row["said"], receipt)
            except OSError:
                continue
            del waiting[key]
        if waiting:
            save_json(path, waiting)
        else:
            path.unlink(missing_ok=True)


def compose(a, items: list[dict], *, flush=False) -> int:
    print_only = getattr(a, "print_only", False)
    STATE.mkdir(parents=True, exist_ok=True)
    # The tick and producers share these queues. Serialize selection and state changes,
    # but release the lock before desk_say runs the say job.
    with (STATE / "compose.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        t = now()
        wait = {it["key"]: it for row in read_lines(RESOLVE_WAIT)
                if (it := normalize_item(row)) and is_resolvable(it)}
        expired_wait = {key for key, row in wait.items() if wait_expired(timestamp(row.get("at")), t)}
        overflow_allowed = True
        if flush:
            last = last_ping_at()
            # Process timeouts first so a full ordinary queue cannot delay them another tick.
            overflow_allowed = not expired_wait and (last is None or t - last >= FLUSH_GAP)
            held_ready = not in_quiet(t) and bool(read_lines(HELD))
            eligible = held_ready or bool(expired_wait)
            if overflow_allowed and not in_quiet(t):
                eligible = (eligible or any(resolution_for(it, t) for it in wait.values())
                            or any((it := normalize_item(row)) and it["kind"] in SPEAKING for row in read_lines(DIGEST)))
            if in_quiet(t) or not eligible:
                if print_only:
                    print("[SILENT]")
                return 0
        prepared = prepare_compose(a, items, t, wait, expired_wait, overflow_allowed)
        if prepared is None:
            print("[SILENT]")
            return 0
        ping, urgent, said, speak = prepared
        message = ping["text"]
        receipt = {"status": "printed"}
        if not print_only:
            # The desk posts in Bot Chat as a Bot reply (the say job). Queue first, still under the compose lock:
            # only a queued message is recorded as said. A failed enqueue rolls the items over for the next tick.
            sys.path.insert(0, str(HERE))
            import desk_say
            key = _notice_key(ping)
            waiting = load_json(_urgent_path(), {}) if urgent else {}
            if urgent:
                waiting[key] = {"ping": ping, "said": {k: said[k] for k in ping["keys"]}}
                save_json(_urgent_path(), waiting)
            try:
                receipt = desk_say.enqueue(message, key=key, job=a.job, urgent=urgent)
            except Exception as exc:
                if not urgent:
                    for it in speak:
                        append(DIGEST, {"at": ping["at"], "job": a.job, **it, "overflow": True,
                                        "first_seen": it.get("first_seen") or it.get("at") or ping["at"]})
                append(RUN_LOG, {"at": ping["at"], "job": a.job, "error": f"enqueue failed: {type(exc).__name__}"[:200],
                                 "rolled_over": ping["keys"]})
                print("[SILENT]")
                return 1
        if print_only:
            save_json(SAID, said)
            append(PING_LOG, {**ping, "delivery": "printed", "say_id": None})
        else:
            _commit_notice(ping, said, receipt)
            if urgent:
                del waiting[key]
                if waiting:
                    save_json(_urgent_path(), waiting)
                else:
                    _urgent_path().unlink(missing_ok=True)
    if not print_only and receipt.get("status") == "queued" and (urgent or not desk_say.quiet_now()):
        desk_say.kick()   # after the compose lock: the say job runs now; its schedule is the safety net
    print(message if print_only else "[SILENT]")
    return 0


CLOSED_STATUSES = {"done", "closed", "dropped", "resolved", "cancelled"}
RECUR_WINDOW = timedelta(days=7)  # a closed key stays shut this long even when its text changed


def closed_rows() -> dict:
    """Ledger rows closed through ea_close.py (status closed/done/dropped/...)."""
    led = load_json(LEDGER, {})
    if not isinstance(led, dict):
        return {}
    return {k: v for k, v in led.items() if isinstance(v, dict) and str(v.get("status", "")).lower() in CLOSED_STATUSES}


def closed_keys() -> set[str]:
    """Keys closed in the action ledger (ea_close.py writes status there), recurring or not."""
    return set(closed_rows())


def same_text(a, b) -> bool:
    return " ".join(str(a or "").split()).casefold() == " ".join(str(b or "").split()).casefold()


def stays_closed(row: dict, item: dict, t: datetime) -> bool:
    """Does a closed ledger row still silence this item? A stable key can recur (a monthly bill), so the
    closure holds while the item says what was closed or within RECUR_WINDOW of closed_at. Rows closed before
    the text and time were recorded cannot prove a recurrence and stay shut."""
    closed_text = row.get("closed_text")
    closed_at = timestamp(row.get("closed_at")) or timestamp(row.get("closed"))
    if not closed_text or closed_at is None:
        return True
    return same_text(closed_text, item.get("text")) or t - closed_at < RECUR_WINDOW


def drop_closed(items: list[dict], t: datetime) -> tuple[list[dict], list[str]]:
    """(items still allowed to speak, keys dropped because the owner closed them)."""
    closed = closed_rows()
    if not closed:
        return items, []
    kept, dropped = [], []
    for it in items:
        row = closed.get(it["key"])
        (dropped if row is not None and stays_closed(row, it, t) else kept).append(it)
    return kept, [i["key"] for i in dropped]


def prepare_compose(a, items, t, wait, expired_wait, overflow_allowed):
    at = t.isoformat(timespec="seconds")
    said_at = t.isoformat()
    said = recent_said(t - timedelta(days=30))
    take_held = getattr(a, "take_held", False)
    attach_digest = getattr(a, "attach_digest", None)
    if take_held and not in_quiet(t):
        keys = {i["key"] for i in items}
        items += [r for row in take(HELD) if (r := normalize_item(row)) and r["key"] not in keys]
    if getattr(a, "take_pending", False):
        keys = {i["key"] for i in items}
        items += [r for row in take(PENDING) if (r := normalize_item(row)) and r["key"] not in keys]

    # Closed anywhere (the owner said done/remove in chat, or proof was found): never raise it again.
    items, skipped_closed = drop_closed(items, t)

    consumed, digest_rows, digest_additions = [], [], []
    keys = {i["key"] for i in items}
    cutoff = t - timedelta(hours=QUEUE_MAX_AGE_HOURS)
    speaking_cutoff = t - timedelta(hours=SPEAKING_MAX_AGE_HOURS)
    expired = []
    # Every run takes the rolled-over items first: no daily cap, nothing waits for a brief.
    for row in (read_lines(DIGEST) if overflow_allowed else ()):
        item = normalize_item(row)
        if item and item["kind"] in SPEAKING:
            consumed.append(row)
            if item["key"] in keys:  # the job re-raised it: keep the new text, keep its place in line
                for it in items:
                    if it["key"] == item["key"] and not it.get("first_seen"):
                        it["first_seen"] = item.get("first_seen") or item.get("at")
                continue
            keys.add(item["key"])
            if queue_is_fresh(row, speaking_cutoff):
                items.append(item)  # overflow items get their turn here, ranked with the new ones
            else:  # a week unanswered: keep it visible in the evening digest instead of dropping it
                expired.append(item["key"])
                digest_additions.append({**item, "at": at, "kind": "digest", "expired_from": item["kind"]})
        else:
            digest_rows.append((row, item))

    # A fresh submission supersedes the saved text. Otherwise reoffer only rows
    # that have resolved or run out of time. A within-gap flush takes only timeouts.
    for key, row in wait.items():
        if key not in keys and (key in expired_wait or (overflow_allowed and resolution_for(row, t))):
            items.append(row)
            keys.add(key)
    # Overflow and waiting rows joined the batch above: a key the owner closed meanwhile must not speak.
    items, late_closed = drop_closed(items, t)
    skipped_closed += late_closed
    if skipped_closed:
        append(RUN_LOG, {"at": at, "job": a.job, "skipped_closed": skipped_closed})
    remaining_wait = dict(wait)
    for key in skipped_closed:
        remaining_wait.pop(key, None)
    need, drafts_by_item, returned_wait = {}, {}, {}
    unresolved = set()
    speak, queued, held, quiet = [], 0, 0, [(k, "expired 7d, moved to digest") for k in expired]
    for it in items:
        key = it["key"]
        remaining_wait.pop(key, None)
        if it["kind"] == "digest":
            digest_additions.append({"at": at, "job": a.job, **it})
            queued += 1
            continue
        if it["kind"] not in SPEAKING:
            quiet.append((it["key"], "not actionable"))
            continue
        item_fp = fp(it)
        identity = (key, item_fp)
        prev = said.get(key)
        if prev and prev.get("fp") == item_fp:
            last = datetime.fromisoformat(prev["said_at"])
            window = COOLDOWN
            if it.get("urgent"):
                window = URGENT_COOLDOWN
            elif a.brief and it["kind"] == "decision":
                window = DECISION_RESURFACE
            if t - last < window:
                quiet.append((key, "already said"))
                continue
        started = None
        if is_resolvable(it):
            old = wait.get(key)
            if old is not None and (fp(old) != item_fp or old.get("act") != it.get("act")):
                old = None
            started = timestamp(old.get("at")) if old is not None else timestamp(it.get("_resolve_wait_at"))
            if old is not None and started is None:
                started = t.astimezone(timezone.utc) - RESOLVE_WAIT_MAX
            wait_row = {**it, "job": a.job, "at": (started or t).isoformat()}
            if started is not None:
                returned_wait[identity] = wait_row
            rec = resolution_for(it, t, bind=True)
            if rec is not None:
                if (rec.get("draft") or "").strip():
                    drafts_by_item[identity] = rec
            elif it.get("urgent") or it.get("resolve_skipped") or (started and wait_expired(started, t)):
                unresolved.add(identity)
            else:
                remaining_wait[key] = wait_row
                need[key] = None
                continue
        if in_quiet(t) and not it.get("urgent"):
            if identity in unresolved and started is not None:
                it = {**it, "_resolve_wait_at": started.isoformat()}
            append(HELD, {"at": at, "job": a.job, **it})
            held += 1
            continue
        speak.append(it)

    cap = phconf.CFG["caps"]["max_brief_items" if a.brief else "max_items"]
    speak.sort(key=lambda i: importance(i, t))
    unique = []
    seen = set()
    for it in speak:
        identity = (it["key"], fp(it))
        if identity in seen:
            quiet.append((it["key"], "duplicate candidate"))
            continue
        seen.add(identity)
        unique.append(it)
    if any(it.get("urgent") for it in unique):
        urgent_items = [it for it in unique if it.get("urgent")]
        overflow = urgent_items[cap:] + [it for it in unique if not it.get("urgent")]
        speak = urgent_items[:cap]
    else:
        overflow, speak = unique[cap:], unique[:cap]

    # Attach FYIs only after a message is certain; untouched rows retain their order.
    limit = (attach_digest or 0) if speak and not speak[0].get("urgent") else 0
    worth, remaining = [], []
    for row, item in digest_rows:
        if (len(worth) < limit and item and item["kind"] == "digest" and item["key"] not in keys
                and item["key"] not in said and queue_is_fresh(row, cutoff)):
            worth.append(item["text"])
            consumed.append(row)
            said[item["key"]] = {"fp": fp(item), "said_at": said_at, "kind": "digest",
                                 "text": item["text"], "job": a.job}
        else:
            remaining.append(row)
    message = ""
    draft_rows = []
    if speak:
        draft_rows = [drafts_by_item.get((i["key"], fp(i))) if is_resolvable(i) else None for i in speak]
        suffixes = [ea_resolve.suffix(rec, n) for n, rec in enumerate(draft_rows, 1)]
        texts = [i["text"].replace("\u2013", "-") if is_resolvable(i) else i["text"] for i in speak]
        message, kept = render_parts(a.header if a.brief else None, texts, worth, more=len(overflow), suffixes=suffixes,
                                     line_limit=LONG_LINE_JOBS.get(getattr(a, "job", "")))
        overflow = speak[kept:] + overflow  # cut for length: rolls over, never marked said
        speak, draft_rows = speak[:kept], draft_rows[:kept]
    for it in overflow:
        key = it["key"]
        if is_resolvable(it) and (row := returned_wait.get((key, fp(it)))) is not None:
            # Keep the deadline through rollover, so the next tick can still bypass the gap.
            remaining_wait.setdefault(key, row)
        else:
            digest_additions.append({"at": at, "job": a.job, **it, "overflow": True,
                                     "first_seen": it.get("first_seen") or it.get("at") or at})

    consume_rows(DIGEST, consumed, remaining)
    for row in digest_additions:
        append(DIGEST, row)
    if remaining_wait != wait:
        write_resolve_wait(remaining_wait.values())

    keys_said = [i["key"] for i in speak]
    unresolved_said = dict.fromkeys(i["key"] for i in speak if is_resolvable(i) and (i["key"], fp(i)) in unresolved)
    run = {"at": at, "job": a.job, "candidates": len(items),
           "said": keys_said, "rolled_over": [i["key"] for i in overflow],
           "digest": queued, "held": held, "quiet": quiet[:20],
           "resolve_wait": list(need), "unresolved": list(unresolved_said)}
    append(RUN_LOG, run)
    if need:
        print(f"[RESOLVE-FIRST] {','.join(need)}")
    if not speak:
        return None

    for it in speak:
        said[it["key"]] = {"fp": fp(it), "said_at": said_at,
                           "kind": it["kind"], "text": it["text"], "job": a.job}
    drafts = {key: rec["status"] for key, rec in zip(keys_said, draft_rows) if rec is not None}
    ping = {"at": at, "kind": "desk_message", "job": a.job, "keys": keys_said,
            "refs": [i.get("ref") for i in speak], "text": message, "drafts": drafts,
            "draft_versions": {key: ea_resolve.draft_version(rec) for key, rec in zip(keys_said, draft_rows) if rec}}
    # SAID is saved by compose() only after the message is queued, so a failed enqueue is retried, never silenced.
    return ping, any(i.get("urgent") for i in speak), said, speak


def cmd_flush(a) -> int:
    """Desk tick: release held items after quiet hours; ordinary overflow waits an hour."""
    args = argparse.Namespace(job="overflow", print_only=getattr(a, "print_only", False), brief=False,
                              take_held=True)
    return compose(args, [], flush=True)


def cmd_queue(path: Path, take: bool) -> int:
    rows = read_lines(path)
    print(json.dumps(rows, ensure_ascii=False))
    if take and rows:
        path.with_suffix(".consumed.jsonl").open("a").write(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        path.write_text("")
    return 0


def cmd_said(a) -> int:
    cutoff = now() - timedelta(days=a.days)
    print(json.dumps(recent_said(cutoff), ensure_ascii=False, indent=1))
    return 0


def cmd_forget(a) -> int:
    said = load_json(SAID, {})
    gone = said.pop(a.key, None)
    save_json(SAID, said)
    print(json.dumps({"forgot": a.key, "was": gone}, ensure_ascii=False))
    return 0


def cmd_ledger(a) -> int:
    """What the desk already created for a matter (reminder id, calendar block key)."""
    if a.list:
        print(json.dumps(load_json(LEDGER, {}), ensure_ascii=False, indent=1))
        return 0
    if not a.key:
        print(json.dumps({"error": "--key or --list"}))
        return 0
    # Read-modify-write under the shared actions.json lock: ea_close.py and ea_reminders.py write it too.
    with ledger_lock():
        led = load_json(LEDGER, {})
        if a.claim:
            kind = a.claim
            row = led.get(a.key, {})
            today = now().date().isoformat()
            used = sum(1 for r in led.values() if str(r.get(f"claimed_{kind}", "")).startswith(today))
            if row.get(f"{kind}_id") or row.get(f"claimed_{kind}"):
                print(json.dumps({"allowed": False, "reason": f"{kind} already exists for this key", "row": row}))
                return 0
            if used >= CLAIM_CAPS[kind]:
                print(json.dumps({"allowed": False, "reason": f"daily {kind} cap {CLAIM_CAPS[kind]} reached"}))
                return 0
            row[f"claimed_{kind}"] = now().isoformat(timespec="seconds")
            led[a.key] = row
            save_json(LEDGER, led)
            print(json.dumps({"allowed": True, "key": a.key, "kind": kind,
                              "idempotency_key": f"{phconf.profile_name()}:{a.key}:{kind}"[:120]}))
            return 0
        if a.set:
            try:
                patch = json.loads(a.set)
            except ValueError as exc:
                print(json.dumps({"error": f"bad --set json: {exc}"}))
                return 0
            row = led.get(a.key, {})
            row.update(patch)
            row["updated_at"] = now().isoformat(timespec="seconds")
            led[a.key] = row
            save_json(LEDGER, led)
        print(json.dumps({a.key: led.get(a.key)}, ensure_ascii=False))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compose")
    c.add_argument("--job", required=True)
    c.add_argument("--file")
    c.add_argument("--print-only", action="store_true",
                   help="print the message instead of posting it in Bot Chat (tests, dry runs)")
    c.add_argument("--brief", action="store_true", help="use the configured brief item limit; decisions resurface daily")
    c.add_argument("--header", default="", help="brief mode only: one opening line (e.g. today's shape)")
    c.add_argument("--take-held", action="store_true", help="add held and overflow/ceiling items")
    c.add_argument("--take-pending", action="store_true", help=f"add desk-internal items that need {phconf.owner()} (health)")
    c.add_argument("--take-overflow", action="store_true",
                   help="kept for old prompts; every run now takes the overflow queue")
    c.add_argument("--attach-digest", type=int, metavar="N",
                   help="up to N FYIs ride along only if speaking")
    fl = sub.add_parser("flush")
    fl.add_argument("--print-only", action="store_true")
    lg = sub.add_parser("ledger")
    lg.add_argument("--key")
    lg.add_argument("--set", help="JSON to merge, e.g. '{\"reminder_id\": \"...\"}'")
    lg.add_argument("--list", action="store_true")
    lg.add_argument("--claim", choices=["reminder", "block"],
                    help="reserve the right to create one reminder/block for this key (idempotent + daily cap)")
    d = sub.add_parser("digest")
    d.add_argument("--take", action="store_true")
    h = sub.add_parser("held")
    h.add_argument("--take", action="store_true")
    s = sub.add_parser("said")
    s.add_argument("--days", type=int, default=3)
    f = sub.add_parser("forget")
    f.add_argument("--key", required=True)
    a = p.parse_args()
    if a.cmd == "compose":
        return cmd_compose(a)
    if a.cmd == "flush":
        return cmd_flush(a)
    if a.cmd == "digest":
        return cmd_queue(DIGEST, a.take)
    if a.cmd == "held":
        return cmd_queue(HELD, a.take)
    if a.cmd == "said":
        return cmd_said(a)
    if a.cmd == "ledger":
        return cmd_ledger(a)
    return cmd_forget(a)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never let a gate crash report garbage to the owner
        try:
            append(RUN_LOG, {"at": now().isoformat(timespec="seconds"), "error": f"{type(exc).__name__}: {exc}"[:300]})
        except Exception:
            pass
        print("[SILENT]")
        sys.exit(0)
