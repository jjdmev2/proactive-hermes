#!/usr/bin/env python3
"""Private resolution store and read-only evidence probe for the desk."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

import phconf
RESOLVABLE = {"send", "reply", "intro", "questions", "followup", "book", "decide", "pay"}
FRESH = timedelta(hours=48)
MAX_DRAFT = 2500
MAX_PROBE = 6000
PLACEHOLDER = re.compile(r"\[[A-Z ]+\]|<[a-z ]+>|TBD|XXX")
GMAIL_ID = re.compile(r"\b1[0-9a-f]{15}\b")
SLACK_CHANNEL = re.compile(r"\b[CDG][0-9A-Z]{8,11}\b")
SLACK_TS = re.compile(r"\b\d{10}\.\d{6}\b")
LEDGERS = ("promises", "goals", "actions", "said")


def now() -> datetime:
    return datetime.now(phconf.tz())


def _timestamp(value) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=phconf.tz()).astimezone(timezone.utc)
    return None


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def lint_draft(text: str) -> list[str]:
    problems = []
    if "\u2014" in text or "\u2013" in text:
        problems.append("Draft contains an em or en dash.")
    if len(text) > MAX_DRAFT:
        problems.append(f"Draft exceeds {MAX_DRAFT} characters.")
    if PLACEHOLDER.search(text):
        problems.append("Draft contains a placeholder.")
    return problems


def _work_channel(rec: dict) -> bool:
    channel = _text(rec.get("channel")).strip()
    labels = set(re.split(r"[\s_:/-]+", channel.lower()))
    if rec.get("scope") == "personal" or "personal" in labels:
        return False
    return (rec.get("scope") == "work" or bool(labels & {"work", "slack"})
            or bool(SLACK_CHANNEL.fullmatch(channel)) or bool(_addresses(channel) & phconf.work_emails()))


def _personal_in_work(rec: dict) -> bool:
    return _work_channel(rec) and any(fact.get("scope") == "personal" for fact in rec.get("facts", []))


def validate(rec: dict) -> tuple[str, list[str]]:
    if not isinstance(rec, dict):
        return "rejected", ["Resolution must be an object."]
    problems = []
    if not _text(rec.get("key")).strip():
        problems.append("Resolution needs a key.")
    act = _text(rec.get("act"))
    if act not in RESOLVABLE:
        problems.append("Resolution needs a supported act.")
    draft = rec.get("draft", "")
    if not isinstance(draft, str):
        problems.append("Draft must be text.")
    else:
        problems.extend(lint_draft(draft))
        if act == "pay" and draft.strip():
            problems.append("Payment resolutions store facts only.")
    facts = rec.get("facts", [])
    if not isinstance(facts, list):
        problems.append("Facts must be a list.")
    else:
        for i, fact in enumerate(facts, 1):
            if not isinstance(fact, dict) or not _text(fact.get("ref")).strip():
                problems.append(f"Fact {i} needs a ref.")
            if not isinstance(fact, dict) or not _text(fact.get("text")).strip():
                problems.append(f"Fact {i} needs text.")
    for field in ("missing", "assumptions"):
        if field in rec and (not isinstance(rec[field], list) or not all(isinstance(x, str) for x in rec[field])):
            problems.append(f"{field} must be a list of strings.")
    if problems:
        return "rejected", problems
    if rec.get("missing") or _personal_in_work(rec):
        return "partial", []
    return ("ready" if facts or draft.strip() else "none"), []


def _load(root: Path) -> dict:
    try:
        rows = json.loads((root / "resolutions.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(rows, dict):
        raise ValueError("Resolution store must be an object.")
    return rows


def _save(root: Path, rows: dict) -> None:
    # A private temp file keeps old or new complete contents visible to readers.
    with NamedTemporaryFile(mode="w", dir=root, encoding="utf-8", delete=False) as out:
        tmp = Path(out.name)
        try:
            os.fchmod(out.fileno(), 0o600)
            json.dump(rows, out, ensure_ascii=False, indent=1, sort_keys=True)
            out.close()
            tmp.replace(root / "resolutions.json")
        finally:
            tmp.unlink(missing_ok=True)


@contextmanager
def _write_store():
    root = phconf.state_dir()
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / "resolutions_log.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as log:
        fcntl.flock(log, fcntl.LOCK_EX)
        os.fchmod(log.fileno(), 0o600)
        yield root, _load(root), log


def _log(log, event: str, row: dict, fields=()) -> None:
    log.write(json.dumps({"at": now().isoformat(), "event": event, "key": row["key"],
                          "status": row["status"], "fields": list(fields)}, ensure_ascii=False) + "\n")


def put(rec: dict) -> dict:
    status, problems = validate(rec)
    if problems:
        return {"status": "rejected", "problems": problems}
    fields = ("key", "act", "to", "channel", "thread", "scope", "facts", "missing", "assumptions",
              "confidence", "searched", "draft", "item_fp")
    row = {field: rec[field] for field in fields if field in rec}
    row.update(status=status, created_at=now().isoformat())
    with _write_store() as (root, rows, log):
        rows[row["key"]] = row
        _save(root, rows)
        _log(log, "put", row)
    return row


def _open(row: dict, at: datetime) -> bool:
    created = _timestamp(row.get("created_at"))
    return bool(created and at - created < FRESH and not (row.get("sent_at") or row.get("dropped_at")))


def get(key, item_fp=None, now=None) -> dict | None:
    row = _load(phconf.state_dir()).get(key)
    at = _timestamp(now if now is not None else _now())
    if not isinstance(row, dict) or at is None or not _open(row, at):
        return None
    # The first compose can bind a new resolution that was put before it knew the item's fingerprint.
    if item_fp is not None and row.get("item_fp") not in (None, item_fp):
        return None
    return row


def _now() -> datetime:
    return now()


def stamp_fp(key, fp):
    return _mark(key, {"item_fp": fp}, bind=True)


def suffix(rec, n) -> str:
    if rec and rec.get("status") == "ready":
        return f"Draft ready, reply {n}."
    if rec and rec.get("status") == "partial" and _text(rec.get("draft")).strip():
        return f"Draft ready with 1 open point, reply {n}."
    return ""


def render_show(rec) -> str:
    sections = [f"To: {rec.get('to') or 'Not recorded'}"]
    if rec.get("channel"):
        sections.append(f"Channel: {rec['channel']}")
    if rec.get("thread"):
        sections.append(f"Thread: {rec['thread']}")
    if rec.get("draft"):
        sections.append(f"Draft:\n{rec['draft']}")
    else:
        sections.append("No draft text stored.")
    missing = list(rec.get("missing", []))
    if _personal_in_work(rec):
        missing.append("Personal source in a work resolution. Check the draft before sharing.")
    for title, lines in (("Open points", missing), ("Assumptions", rec.get("assumptions", [])),
                         ("Sources", [f"{fact['text']} ({fact['ref']})" for fact in rec.get("facts", [])])):
        if lines:
            sections.append(title + ":\n" + "\n".join(f"- {line}" for line in lines))
    return "\n\n".join(sections)


AMBIGUOUS = "ambiguous"


def draft_version(row: dict) -> str:
    """Bind the draft and its destination/evidence to the notice the owner actually saw."""
    fields = ("created_at", "key", "item_fp", "act", "status", "draft", "to", "channel", "thread",
              "scope", "facts", "missing", "assumptions")
    return hashlib.sha256(json.dumps([row.get(key) for key in fields], sort_keys=True).encode()).hexdigest()


def _say_statuses() -> dict[str, dict]:
    import desk_say
    order = {}
    try:
        turns = desk_say.bot_chat_turns() or []
        desk_say.confirm(turns=turns)
        order = {rid: i for i, (rid, role, _) in enumerate(turns) if role == "assistant"}
    except Exception:
        pass
    return {mid: {**row, "reply_order": order.get(row.get("reply_id"))}
            for mid, row in desk_say._last_status().items()}


def _message_id(row: dict) -> str | None:
    if isinstance(row.get("say_id"), str):
        return row["say_id"]
    keys, text = row.get("keys"), row.get("text")
    if not isinstance(keys, list) or not isinstance(text, str):
        return None
    key = f"{row.get('job') or ''}:{','.join(str(k) for k in keys)}"
    return hashlib.sha256(f"{key}\n{text.strip()}".encode()).hexdigest()[:24]


def _numbered_draft(n, max_age_hours=36, statuses=None) -> tuple[dict | None, str]:
    if n < 1:
        return None, "none"
    cutoff = _timestamp(now()) - timedelta(hours=max_age_hours)
    rows = {}
    try:
        with (phconf.state_dir() / "ping_log.jsonl").open(encoding="utf-8") as log:
            for line in log:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("kind") != "desk_message":
                    continue
                keys = row.get("keys")
                if not row.get("drafts") and not (isinstance(keys, list) and len(keys) > 1):
                    continue   # an unnumbered notice is not a reply-N target
                at = _timestamp(row.get("at"))
                if at is not None and at >= cutoff and (mid := _message_id(row)):
                    rows.setdefault(mid, row)
    except FileNotFoundError:
        return None, AMBIGUOUS
    if not rows:
        return None, AMBIGUOUS
    statuses = _say_statuses() if statuses is None else statuses
    received = {}
    for mid, row in rows.items():
        receipt = statuses.get(mid) or {}
        if not isinstance(receipt, dict):
            return None, AMBIGUOUS   # legacy records had no reply order
        state = receipt.get("status")
        order = receipt.get("reply_order", receipt.get("reply_id"))
        if state == "delivered" and type(order) is int and order not in received:
            received[order] = row
        elif state not in ("queued", "retry", "failed", None):
            # A compose timestamp says nothing about where an unconfirmed relay appeared in the chat.
            return None, AMBIGUOUS
    if not received:
        return None, AMBIGUOUS
    latest = received[max(received)]
    keys = latest.get("keys")
    if not isinstance(keys, list) or n > len(keys) or not isinstance(keys[n - 1], str):
        return None, AMBIGUOUS
    key = keys[n - 1]
    versions = latest.get("draft_versions")
    try:
        row = get(key)
    except (OSError, ValueError):
        row = None
    if not row or not isinstance(versions, dict) or versions.get(key) != draft_version(row):
        return None, AMBIGUOUS
    return row, "ok"


def number_target(n, max_age_hours=36, statuses=None) -> tuple[str | None, str]:
    """Resolve a number by confirmed Bot reply order, and only to the same draft version."""
    row, why = _numbered_draft(n, max_age_hours, statuses)
    return (row["key"] if row else None), why


def key_for_number(n, max_age_hours=36, statuses=None):
    return number_target(n, max_age_hours, statuses)[0]


def _mark(key, fields, *, bind=False, expected_version=None) -> dict:
    with _write_store() as (root, rows, log):
        row = rows.get(key)
        if not isinstance(row, dict):
            return {"status": "unknown", "key": key}
        if expected_version is not None and (not _open(row, now()) or draft_version(row) != expected_version):
            return {"status": "changed", "key": key}
        if (bind and row.get("item_fp") is not None) or all(row.get(k) == v for k, v in fields.items()):
            return row
        row.update(fields)
        _save(root, rows)
        _log(log, "mark", row, fields)
        return row


def mark(key, **fields) -> dict:
    return _mark(key, fields)


def extract_pointers(text: str) -> dict:
    gmail = list(dict.fromkeys(GMAIL_ID.findall(text)))
    slack = {}
    channels = list(SLACK_CHANNEL.finditer(text))
    for i, match in enumerate(channels):
        end = channels[i + 1].start() if i + 1 < len(channels) else len(text)
        stamps = slack.setdefault(match[0], [])
        for ts in SLACK_TS.findall(text, match.end(), end):
            if ts not in stamps:
                stamps.append(ts)
    return {"gmail": gmail, "slack": [{"channel": ch, "ts": ts} for ch, ts in slack.items()]}


def _clip(value, limit: int) -> str:
    text = _text(value).replace("\u2014", "-").replace("\u2013", "-")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def _needles(who, terms) -> list[str]:
    return list(dict.fromkeys(s for raw in [who, *terms] if (s := _text(raw).strip())))


def search_ledgers(who, terms) -> dict:
    needles = _needles(who, terms)
    patterns = [re.compile(r"\b" + re.escape(re.sub(r"[-_]+", " ", n)) + r"\b", re.I) for n in needles]

    def matches(text):
        return any(p.search(re.sub(r"[-_]+", " ", text)) for p in patterns)

    root = phconf.state_dir()
    ledger, decisions, related, sources, gaps = [], [], [], [], []
    cutoff = _timestamp(now()) - timedelta(days=7)
    try:
        for line in (root / "DECISIONS.md").read_text(encoding="utf-8").splitlines():
            if matches(line):
                decisions.append(_clip(line, 240))
                if len(decisions) == 8:
                    break
    except (OSError, UnicodeError) as exc:
        gaps.append(f"DECISIONS.md: {type(exc).__name__}")
    for name in LEDGERS:
        try:
            data = json.loads((root / f"{name}.json").read_text(encoding="utf-8"))
            if name == "goals" and isinstance(data, dict) and "goals" in data:
                data = data["goals"]
            if isinstance(data, list):
                data = {row["key"]: row for row in data if isinstance(row, dict) and isinstance(row.get("key"), str)}
            if not isinstance(data, dict):
                raise ValueError("Expected ledger rows.")
        except (OSError, ValueError) as exc:
            gaps.append(f"{name}: {type(exc).__name__}")
            continue
        for key, row in data.items():
            if not isinstance(row, dict):
                continue
            if name == "said" and ((at := _timestamp(row.get("said_at"))) is None or at < cutoff):
                continue
            summary = " | ".join(_text(row.get(field)) for field in
                                 ("to", "what", "title", "text", "next_step", "status", "source") if _text(row.get(field)))
            if not matches(f"{key} {summary}"):
                continue
            if name == "promises" and row.get("source") and len(sources) < 8:
                sources.append(_text(row["source"]))
            if len(ledger) + len(decisions) < 8:
                ledger.append({"source": name, "key": key, "text": _clip(summary or key, 240)})
            if len(related) < 8 and key not in related:
                related.append(key)
    return {"ledger": ledger, "decisions": decisions, "related_keys": related, "gaps": gaps, "sources": sources}


def search_sessions(who, terms, days=14, db=None) -> list[dict]:
    if db is None:
        db = phconf.hermes_home() / "state.db"   # Hermes's chat database of the active profile
    query = " OR ".join('"' + part.replace('"', '""') + '"' for part in _needles(who, terms))
    if not query:
        return []
    cutoff = (_timestamp(now()) - timedelta(days=days)).timestamp()
    with closing(sqlite3.connect(Path(db).absolute().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
        rows = conn.execute("""
            SELECT m.session_id, m.id, m.role, snippet(messages_fts, 0, '', '', '...', 24)
              FROM messages_fts JOIN messages AS m ON m.id = messages_fts.rowid
              JOIN sessions AS s ON s.id = m.session_id
             WHERE messages_fts MATCH ? AND m.timestamp >= ?
               AND m.role IN ('user', 'assistant') AND s.source NOT IN ('cron', 'subagent')
               AND m.content <> '' AND m.content NOT LIKE 'DESK RELAY%'
               AND m.content NOT LIKE '[Cronjob "Proactive%' AND m.content NOT LIKE '%Proactive Hermes desk message%'
               AND m.content NOT LIKE '%ea_resolve.py%' AND m.content NOT LIKE '%resolve-before-ask%'
             ORDER BY m.timestamp DESC, m.id DESC LIMIT 5
        """, (query, cutoff)).fetchall()
    return [{"session": sid, "id": mid, "role": role, "snippet": _clip(snippet, 200)}
            for sid, mid, role, snippet in rows]


def _mail_date(value) -> datetime | None:
    try:
        return _timestamp(parsedate_to_datetime(_text(value)))
    except (TypeError, ValueError, OverflowError):
        return _timestamp(value)


def _addresses(header) -> set[str]:
    return {addr.lower() for _, addr in getaddresses([_text(header)]) if addr}


def search_gmail(email, who, runner=None, *, since=None, before=None, origin_ids=()) -> dict:
    import subprocess

    empty = {"mail": [], "already_done": None, "gaps": []}
    address = _text(email).strip()
    if not address:
        return empty
    if not re.fullmatch(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+", address):
        return {**empty, "gaps": ["gmail: invalid address"]}
    query = f"(from:{address} OR to:{address}) newer_than:21d"
    before_at = _timestamp(before)
    if before_at:
        query += f" before:{int(before_at.timestamp())}"
    script = phconf.google_api()
    argv = [sys.executable, str(script), "gmail", "search", query, "--max", "8"]
    kwargs = {"capture_output": True, "text": True, "timeout": 60}
    try:
        if runner is None:
            # The google-workspace skill's own wrapper signs in with its own token; this script never opens,
            # copies or refreshes that token itself (catalog rule 11).
            result = subprocess.run(argv, **kwargs)
        else:
            result = runner(argv, **kwargs)
        if result.returncode:
            return {**empty, "gaps": [f"gmail: search exited {result.returncode}"]}
        headers = json.loads(result.stdout)
        if not isinstance(headers, list) or not all(isinstance(h, dict) for h in headers):
            raise ValueError("Unexpected Gmail headers.")
    except Exception as exc:
        return {**empty, "gaps": [f"gmail: {type(exc).__name__}"]}

    address = address.lower()
    dated = []
    for header in headers[:8]:
        msg_id = _text(header.get("id"))
        if not GMAIL_ID.fullmatch(msg_id):
            continue
        at = _mail_date(header.get("date"))
        if before_at and (at is None or at >= before_at):
            continue
        sender = _addresses(header.get("from"))
        recipients = _addresses(header.get("to"))
        labels = header.get("labels")
        sent = isinstance(labels, list) and "SENT" in labels
        direction = ("outbound" if address in recipients and (sent or sender & phconf.work_emails())
                     else "inbound" if address in sender else "unknown")
        clean = {"id": msg_id, "direction": direction}
        thread = _text(header.get("threadId"))
        if GMAIL_ID.fullmatch(thread):
            clean["threadId"] = thread
        for field, limit in (("from", 110), ("to", 110), ("subject", 160), ("date", 80)):
            value = _clip(header.get(field), limit)
            if value:
                clean[field] = value
        dated.append((at, clean))
    dated.sort(key=lambda pair: pair[0] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    anchor = _timestamp(since)
    if anchor is None:
        anchor = max((at for at, row in dated if at and row["id"] in origin_ids and row["direction"] == "outbound"),
                     default=None)
    done = next((row for at, row in dated if anchor and at and at > anchor and row["direction"] == "outbound"), None)
    return {"mail": [row for _, row in dated], "already_done": done, "gaps": []}


def build_next(act, pointers, terms, mail) -> list[dict]:
    steps = []
    if pointers["slack"]:
        steps.append({"tool": "FETCH_CONVERSATION_HISTORY", "args": {"channel": pointers["slack"][0]["channel"], "limit": 15}})
    elif terms:
        steps.append({"tool": "SEARCH_MESSAGES", "args": {"query": terms[0], "count": 10}})
    inbound = next((row for row in mail if row.get("direction") == "inbound"), None)
    if inbound:
        steps.append({"tool": "gmail", "args": ["get", inbound["id"]]})
    if act in {"book", "decide"}:
        steps.append({"tool": "ea_calendar_block.py", "args": ["free"]})
    return steps


def _probe(args) -> dict:
    terms = [term.strip() for term in args.terms.split(",") if term.strip()]
    evidence = search_ledgers(args.who, terms)
    sources, gaps = evidence.pop("sources"), evidence.pop("gaps")
    evidence["related_keys"] = [key for key in evidence["related_keys"] if key != args.key]
    pointers = extract_pointers("; ".join([args.ref, *sources]))
    try:
        sessions = search_sessions(args.who, terms)
    except (OSError, sqlite3.Error, ValueError) as exc:
        sessions = []
        gaps.append(f"sessions: {type(exc).__name__}")
    mail, done = [], None
    if not args.no_mail and phconf.CFG["mail"]["gmail_api"]:
        result = search_gmail(args.email, args.who, since=args.since, before=args.before, origin_ids=pointers["gmail"])
        mail, done = result["mail"], result["already_done"]
        gaps.extend(result["gaps"])
    return {"pointers": pointers, **evidence, "sessions": sessions, "mail": mail, "already_done": done,
            "next": build_next(args.act, pointers, terms, mail), "gaps": gaps}


def _probe_json(data: dict) -> str:
    # Trim whole lower-priority hits; preserve parseable JSON, first pointers and next actions.
    def dump():
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    raw = dump()
    if len(raw) < MAX_PROBE:
        return raw
    data["gaps"].append("output: capped")
    slack, gmail = data["pointers"]["slack"], data["pointers"]["gmail"]
    # Bound pointers before serializing again if the ref itself is oversized.
    del gmail[32:]
    del slack[8:]
    for row in slack:
        del row["ts"][8:]
    raw = dump()
    for rows, keep in ((gmail, 1), (slack, 1), *((row["ts"], 2) for row in slack),
                       (data["mail"], 1), (data["sessions"], 1), (data["ledger"], 1),
                       (data["decisions"], 1), (data["related_keys"], 1),
                       (data["related_keys"], 0), (data["ledger"], 0), (data["sessions"], 0), (data["mail"], 0)):
        while len(rows) > keep and len(raw) >= MAX_PROBE:
            rows.pop()
            raw = dump()
    if len(raw) >= MAX_PROBE:
        for step in data["next"]:
            if step["tool"] == "SEARCH_MESSAGES":
                step["args"]["query"] = _clip(step["args"]["query"], 240)
        raw = dump()
    return raw


def _iso(value):
    at = _timestamp(value)
    if at is None:
        raise argparse.ArgumentTypeError("Expected an ISO date or timestamp.")
    return at


def _date(value):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise argparse.ArgumentTypeError("Expected YYYY-MM-DD.")
    return _iso(value)


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
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("put").add_argument("--file", required=True, type=Path)
    sub.add_parser("get").add_argument("--key", required=True)
    s = sub.add_parser("show")
    select = s.add_mutually_exclusive_group(required=True)
    select.add_argument("--key")
    select.add_argument("--n", type=int)
    s = sub.add_parser("mark")
    s.add_argument("--key", required=True)
    select = s.add_mutually_exclusive_group(required=True)
    select.add_argument("--approved", action="store_true")
    _text_option(select, "sent", group=False, metavar="REF")
    _text_option(select, "dropped", group=False, metavar="WHY")
    sub.add_parser("list").add_argument("--open", action="store_true")
    s = sub.add_parser("probe")
    s.add_argument("--key", required=True)
    s.add_argument("--act", required=True, choices=sorted(RESOLVABLE))
    _text_option(s, "who", required=True)
    s.add_argument("--email")
    _text_option(s, "terms", required=True)
    _text_option(s, "ref", default="")
    s.add_argument("--since", type=_iso)
    s.add_argument("--before", type=_date)
    s.add_argument("--no-mail", action="store_true")
    a = p.parse_args(argv)
    _load_text_files(a, p)
    if a.cmd == "probe":
        print(_probe_json(_probe(a)))
        return 0
    if a.cmd == "put":
        try:
            rec = json.loads(a.file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            result = {"status": "rejected", "problems": [f"Cannot read resolution input: {type(exc).__name__}."]}
        else:
            result = put(rec)
        print(json.dumps(result, ensure_ascii=False))
        return 2 if result["status"] == "rejected" else 0
    if a.cmd == "get":
        result = get(a.key)
    elif a.cmd == "show":
        row, why = (get(a.key), "ok") if a.key is not None else _numbered_draft(a.n)
        if why == AMBIGUOUS:
            print("Not sure which item or draft version that number refers to. Ask the owner which item they mean "
                  "before showing or sending anything.")
        elif row is None:
            print("No stored draft for that item.")
        else:
            shown = _mark(row["key"], {"shown_at": now().isoformat()}, expected_version=draft_version(row))
            if shown.get("status") in {"changed", "unknown"}:
                print("The draft changed. Ask which item the owner means before showing or sending anything.")
            else:
                print(render_show(shown))
        return 0
    elif a.cmd == "mark":
        at = now().isoformat()
        if a.approved:
            result = mark(a.key, approved_at=at)
        elif a.sent is not None:
            result = mark(a.key, sent_at=at, sent_ref=a.sent)
        else:
            result = mark(a.key, dropped_at=at, dropped_why=a.dropped)
    else:
        at = _timestamp(now())
        result = [row for row in _load(phconf.state_dir()).values() if not a.open or _open(row, at)]
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
