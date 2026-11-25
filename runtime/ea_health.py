#!/usr/bin/env python3
"""Proactive Hermes desk health. Silent unless there is a fault to report.

Checks configured jobs, source access and the desk message contract. Owner-fixable
faults become one pending action; persistent internal faults become a quiet desk
note. Faults are deduplicated in health_state.json and logged to run_log.jsonl.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import ea_reminders as reminders
import phconf

PROFILE = phconf.hermes_home()
JOBS = PROFILE / "cron" / "jobs.json"
OUTPUT = PROFILE / "cron" / "output"
STATE_DIR = phconf.state_dir()
CURSOR = STATE_DIR / "email_cursor.json"
HEALTH_STATE = STATE_DIR / "health_state.json"
PENDING = STATE_DIR / "pending_queue.jsonl"
RUN_LOG = STATE_DIR / "run_log.jsonl"
PING_LOG = STATE_DIR / "ping_log.jsonl"

DESK_PREFIXES = ("Proactive ·",)
REALERT = timedelta(hours=24)
DESK_DOWN_AFTER = timedelta(hours=2)

faults: dict[str, tuple[str, str]] = {}


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def log(row: dict) -> None:
    try:
        with RUN_LOG.open("a") as fh:
            fh.write(json.dumps({"at": datetime.now(phconf.tz()).isoformat(timespec="seconds"), "job": "health", **row},
                                ensure_ascii=False) + "\n")
    except Exception:
        pass


def fault_key(line: str) -> str:
    key = re.sub(r"\d+(\.\d+)?", "N", line)
    key = re.sub(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b", "DAY", key)
    return re.sub(r"\s+", " ", key).strip().lower()[:160]


def fault(cls: str, text: str) -> None:
    faults[fault_key(text)] = (cls, text)


def load_jobs() -> list[dict]:
    raw = json.loads(JOBS.read_text())
    rows = raw.get("jobs", raw) if isinstance(raw, dict) else raw
    return [r for r in rows if isinstance(r, dict)]


def check_local_delivery(jobs: list[dict]) -> None:
    """Keep cron responses in the local lane; desk messages use the delivery helper."""
    for job in jobs:
        for field, flag in (("deliver", "--deliver"), ("failure_deliver", "--failure-deliver")):
            value = str(job.get(field) or "").strip()
            if value == "local" or (not value and field == "failure_deliver"):
                continue
            proc = subprocess.run(["hermes", "-p", phconf.profile_name(), "cron", "edit", job["id"], flag, "local"],
                                  capture_output=True, text=True, timeout=60)
            log({"healed": f"{field}_to_local", "job_id": job["id"], "ok": proc.returncode == 0})


def check_say_outbox() -> None:
    """Flag desk messages stuck in the outbox for over an hour."""
    path = STATE_DIR / "say_outbox.jsonl"
    try:
        rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    except FileNotFoundError:
        return
    stale = [r for r in rows if (t := parse_ts(r.get("queued_at"))) and now() - t > timedelta(hours=1)]
    if stale:
        fault("desk", f"{len(stale)} desk message(s) not posted for over an hour")


def check_say_receipts() -> None:
    """desk_say fails closed: a post whose outcome is unclear is never retried. Surface those so
    a message for the owner cannot vanish quietly (last status 'unverified', or 'posting' > 10 min)."""
    path = STATE_DIR / "say_log.jsonl"
    last: dict[str, dict] = {}
    try:
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row.get("id"), str):
                last[row["id"]] = row
    except FileNotFoundError:
        return
    stuck = [r for r in last.values() if r.get("status") == "unverified"
             or (r.get("status") == "posting" and (t := parse_ts(r.get("at"))) and now() - t > timedelta(minutes=10))]
    if stuck:
        fault("desk", f"{len(stuck)} desk message(s) with an unclear post, check say_log")


def _resolve_rows(path: Path) -> Iterator[dict]:
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield row
    except FileNotFoundError:
        return


def check_resolve() -> None:
    """Flag repeated unresolved items and a stuck resolve wait queue."""
    current = now()
    recent = current - timedelta(hours=24)
    unresolved: set[str] = set()
    for row in _resolve_rows(RUN_LOG):
        keys = row.get("unresolved")
        if isinstance(keys, list) and (stamp := parse_ts(row.get("at"))) and recent <= stamp <= current:
            unresolved.update(key for key in keys if isinstance(key, str) and key)
    if len(unresolved) >= 3:
        fault("desk", f"resolve step skipped on {len(unresolved)} items")

    stale = current - timedelta(minutes=60)
    if any((stamp := parse_ts(row.get("at"))) and stamp < stale
           for row in _resolve_rows(STATE_DIR / "resolve_wait.jsonl")):
        fault("desk", "resolve wait queue stuck")


def check_jobs(jobs: list[dict]) -> None:
    try:
        from croniter import croniter
    except Exception:
        croniter = None
    desk = [j for j in jobs if (j.get("name") or "").startswith(DESK_PREFIXES)]
    expected_jobs = sum(bool(enabled) for enabled in phconf.CFG["features"].values())
    if len(desk) < expected_jobs:
        fault("desk", f"only {len(desk)} desk jobs exist, expected {expected_jobs}")
    for job in desk:
        name = job.get("name") or job["id"]
        if not job.get("enabled", True) or job.get("state") == "paused":
            if job.get("paused_reason"):
                continue  # paused on purpose (rebuild, seasonal); not a fault
            fault("desk", f"{name}: disabled")
            continue
        status = (job.get("last_status") or "").lower()
        streak = int(job.get("failure_streak") or 0)
        if status in {"error", "failed", "timeout", "timed_out"} and streak >= 2:
            fault("desk", f"{name}: failing {streak} runs in a row")
        if job.get("last_delivery_error"):
            fault("desk", f"{name}: delivery failed")
        sched = job.get("schedule") or {}
        if croniter is None or sched.get("kind") != "cron" or not job.get("last_run_at"):
            continue
        tz = ZoneInfo(job["timezone"]) if job.get("timezone") else phconf.tz()
        expected = croniter(sched["expr"], datetime.now(tz) - timedelta(minutes=25)).get_prev(datetime)
        created = parse_ts(job.get("created_at"))
        if created and expected.astimezone(timezone.utc) < created:
            continue
        last = parse_ts(job.get("last_run_at"))
        if last < expected.astimezone(timezone.utc) - timedelta(minutes=10):
            fault("desk", f"{name}: missed its scheduled run")


def check_cursor() -> None:
    try:
        accounts = json.loads(CURSOR.read_text()).get("accounts") or {}
        newest = max((parse_ts(v.get("last_seen")) for v in accounts.values() if v.get("last_seen")), default=None)
    except Exception:
        fault("desk", "mail cursor unreadable")
        return
    if newest is None or now() - newest > timedelta(hours=26):
        fault("desk", "mail cursor stale, the radar is not reading mail")


def check_google() -> None:
    try:
        proc = subprocess.run([sys.executable, str(phconf.scripts_dir() / "ea_calendar_block.py"), "mine", "--days", "1"],
                              capture_output=True, text=True, timeout=90)
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:
        fault("desk", f"Google calendar check crashed: {type(exc).__name__}")
        return
    if data.get("status") != "ok":
        err = str(data.get("error", ""))
        if re.search(r"invalid_grant|unauthor|401|403|consent|refresh|expired", err, re.I):
            fault("owner", f"{phconf.owner()}, reconnect Google so I can read and block time on your calendar.")
        else:
            fault("desk", "Google calendar read failing")


def check_device() -> None:
    if not phconf.CFG["reminders"]["enabled"]:
        return
    try:
        with closing(reminders.bridge(read_only=True)) as db:   # the one mobile bridge access point
            row = db.execute("SELECT max(seen) FROM devices").fetchone()
    except Exception:
        return
    if not row or row[0] is None:
        fault("owner", f"{phconf.owner()}, pair your phone in Hermes under Settings > Device Access "
                       "so I can create reminders.")
    elif now().timestamp() - float(row[0]) > 48 * 3600:
        fault("owner", f"{phconf.owner()}, open Hermes on your phone to sync pending reminders.")


PHONE_STALE = timedelta(minutes=60)
PHONE_HOURS = (8, 22)  # owner's timezone; a phone asleep at night is normal


def phone_last_seen():
    """When the phone last talked to the bridge: devices.seen, else the newest landed/done outbox row."""
    try:
        with closing(reminders.bridge(read_only=True)) as db:
            row = db.execute("SELECT max(seen) FROM devices").fetchone()
        if row and row[0] is not None:
            return datetime.fromtimestamp(float(row[0]), timezone.utc)
    except Exception:
        pass
    try:
        box = json.loads((STATE_DIR / "reminder_outbox.json").read_text())
    except Exception:
        return None
    stamps = [parse_ts(v.get(f)) for v in box.values() if isinstance(v, dict) for f in ("landed", "done")]
    return max((s for s in stamps if s), default=None)


def check_phone_sync() -> None:
    """Phone reminders and alarms only land when the phone wakes the Hermes app. If it has not synced for an
    hour during the day, tell the owner once (the 2-day case is check_device's line, not this one)."""
    local = now().astimezone(phconf.tz())
    if not PHONE_HOURS[0] <= local.hour < PHONE_HOURS[1]:
        return
    seen = phone_last_seen()
    if seen is None:
        return  # unpaired is check_device's fault
    age = now() - seen
    if PHONE_STALE < age <= timedelta(hours=48):
        fault("owner", f"Phone has not synced for {int(age.total_seconds() // 60)} min: open Hermes once; "
                       "check Background App Refresh")


def last_response(job_id: str):
    try:
        latest = max((OUTPUT / job_id).glob("*.md"), key=lambda p: p.stat().st_mtime)
    except (ValueError, FileNotFoundError):
        return None
    text = latest.read_text(errors="ignore")
    if "## Response" not in text:
        return None
    return text.split("## Response", 1)[1].strip(), datetime.fromtimestamp(latest.stat().st_mtime, timezone.utc)


def _answer_only(text: str) -> str:
    """The job's own answer, with markdown quoting and post-run verifier notices removed.

    Hermes appends a 'File-mutation verifier' warning (starts with a warning glyph)
    after the model's answer; it is tooling output, not the reply. Comparing the raw
    response against '[SILENT]' therefore flagged compliant runs every tick.
    """
    text = text.split("\u26a0")[0]
    parts = []
    for line in text.splitlines():
        stripped = line.strip().lstrip(">#*-").strip()
        if stripped:
            parts.append(" ".join(stripped.split()))
    return " ".join(parts)


def check_contract(jobs: list[dict]) -> None:
    composed = set()
    try:
        for line in PING_LOG.read_text().splitlines()[-400:]:
            row = json.loads(line)
            if row.get("kind") == "desk_message":
                composed.add(" ".join(str(row.get("text", "")).split()))
    except Exception:
        pass
    for job in jobs:
        if job.get("no_agent"):
            continue
        res = last_response(job["id"])
        if not res or now() - res[1] > timedelta(hours=26):
            continue
        flat = _answer_only(res[0])
        if flat in ("[SILENT]", "SILENT") or flat in composed:
            continue
        fault("desk", f"{job['name']}: answered outside the desk gate")


def enqueue_for_owner(text: str, key: str) -> None:
    row = {"at": datetime.now(phconf.tz()).isoformat(timespec="seconds"), "job": "health", "key": f"health:{key[:60]}",
           "kind": "action", "text": text, "change": key, "ref": "desk health"}
    with PENDING.open("a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def report() -> list[str]:
    try:
        state = json.loads(HEALTH_STATE.read_text())
        state = state if isinstance(state, dict) else {}
    except Exception:
        state = {}
    t = now()
    out: list[str] = []
    for key, (cls, text) in faults.items():
        prev = state.get(key) or {}
        first = parse_ts(prev.get("first_seen")) if prev.get("status") == "open" else None
        first = first or t
        last_alert = parse_ts(prev.get("last_alerted"))
        state[key] = {"text": text, "class": cls, "first_seen": first.isoformat(timespec="seconds"),
                      "last_seen": t.isoformat(timespec="seconds"), "status": "open",
                      "last_alerted": prev.get("last_alerted")}
        due = last_alert is None or t - last_alert > REALERT
        if cls == "owner" and due:
            enqueue_for_owner(text, key)
            state[key]["last_alerted"] = t.isoformat(timespec="seconds")
        elif cls == "desk" and due and t - first >= DESK_DOWN_AFTER:
            out.append(text)
            state[key]["last_alerted"] = t.isoformat(timespec="seconds")
    for key, prev in state.items():
        if prev.get("status") == "open" and key not in faults:
            prev["status"] = "recovered"
            prev["recovered_at"] = t.isoformat(timespec="seconds")
    cutoff = t - timedelta(days=30)
    state = {k: v for k, v in state.items()
             if v.get("status") == "open" or (parse_ts(v.get("recovered_at") or v.get("last_seen")) or t) > cutoff}
    tmp = HEALTH_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(HEALTH_STATE)
    log({"open": [v[1] for v in faults.values()], "printed": out})
    return out


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        jobs = load_jobs()
    except Exception:
        fault("desk", "cron jobs.json unreadable")
        jobs = []
    desk_jobs = [job for job in jobs if (job.get("name") or "").startswith(DESK_PREFIXES)]
    steps = [lambda: check_local_delivery(desk_jobs), lambda: check_jobs(desk_jobs),
             lambda: check_contract(desk_jobs), check_say_outbox, check_say_receipts]
    if phconf.CFG["features"]["mail_radar"] and phconf.CFG["mail"]["himalaya_accounts"]:
        steps.append(check_cursor)
    if phconf.CFG["calendar"]["enabled"]:
        steps.append(check_google)
    if phconf.CFG["reminders"]["enabled"]:
        steps.append(check_device)
        steps.append(check_phone_sync)
    if phconf.CFG["features"]["resolve_first"]:
        steps.append(check_resolve)
    for step in steps:
        try:
            step()
        except Exception as exc:
            log({"error": f"{type(exc).__name__}: {exc}"[:200]})
    lines = report()
    if lines:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import desk_say
        text = (f"{phconf.owner()}, the desk has a fault and may miss things until it's fixed: "
                + "; ".join(lines[:2]))
        # No push: internal faults don't require the owner's intervention.
        desk_say.deliver(text, key="health:" + "|".join(sorted(lines))[:200], job="health", push_enabled=False)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        log({"crash": f"{type(exc).__name__}: {exc}"[:200]})
        sys.exit(0)
