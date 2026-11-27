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
import ph_jobs
import phconf

HERE = Path(__file__).resolve().parent   # once, at start: the runtime generation this run belongs to
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


SAY_JOB = ph_jobs.job_name("say")


def check_local_delivery(jobs: list[dict]) -> None:
    """Keep the plugin's own cron responses in the local lane; desk messages go out only through the say job, whose
    output (one desk message) is the one thing delivered to Bot Chat. Only jobs the installer recorded are healed
    (never another job because of its name). The say job is healed back to Bot Chat, never to local, and that
    raises a fault: while it pointed elsewhere it took nothing (desk_say fails closed). Once the say job is back on
    Bot Chat it is kicked (kick_say), so the messages that waited go out now, not on its next two-minute run."""
    recorded = registry_ids()
    say_id = recorded.get("say")
    own = set(recorded.values())
    say_healed = False
    for job in jobs:
        if job.get("id") not in own:
            continue
        say = job.get("id") == say_id
        for field, flag in (("deliver", "--deliver"), ("failure_deliver", "--failure-deliver")):
            value = str(job.get(field) or "").strip()
            want = "bot-chat" if say and field == "deliver" else "local"
            if value == want or (not value and field == "failure_deliver" and not say):
                continue
            proc = subprocess.run(["hermes", "-p", phconf.profile_name(), "cron", "edit", job["id"], flag, want],
                                  capture_output=True, text=True, timeout=60)
            log({"healed": f"{field}_to_{want.replace('-', '_')}", "job_id": job["id"], "ok": proc.returncode == 0})
            if say and field == "deliver":
                result = "set back to Bot Chat" if proc.returncode == 0 else "could not set it back to Bot Chat"
                fault("desk", f"the say job delivered to {value or 'nothing'}, not Bot Chat; {result}")
                say_healed = say_healed or proc.returncode == 0
    if say_healed:
        log({"kicked_say_after_heal": kick_say()})


def kick_say() -> dict:
    """Run the say job now through desk_say.flush (`hermes cron run <say job>` while a message is due; nothing when
    none is, and non-urgent messages still wait out quiet hours). Never raises."""
    try:
        sys.path.insert(0, str(HERE))
        import desk_say
        return desk_say.flush()
    except Exception as exc:
        return {"kick": f"{type(exc).__name__}: {exc}"[:200]}


def check_say_outbox() -> None:
    """Flag due or unresolved items without treating normal quiet-hours backlog as a failure."""
    import desk_say
    last = desk_say._last_status()
    stale = [r for r in desk_say._pending(desk_say._lines(desk_say.OUTBOX)).values()
             if last.get(r["id"], {}).get("status") in (None, "retry")
             and (r.get("urgent") or not desk_say.quiet_now())
             and (t := parse_ts(r.get("queued_at"))) and now() - t > timedelta(hours=1)]
    if stale:
        fault("relay", f"{len(stale)} desk message(s) not posted for over an hour")


def check_say_receipts() -> None:
    import desk_say
    desk_say.reconcile_handoffs()
    desk_say.confirm()
    if issues := desk_say.delivery_issues():
        fault("relay", f"desk delivery unresolved: {', '.join(issues)}; see say_log.jsonl")
    if problem := desk_say.route_problem():
        fault("relay", f"desk messages held: {problem}")
    elif (route := desk_say.route_status()).get("route_problem") and (s := parse_ts(route.get("at"))) and now() - s <= timedelta(hours=1):
        fault("relay", f"desk messages held: {route['route_problem']}")


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


def registry_ids() -> dict[str, str]:
    """{job key: cron job id} as the installer recorded them (<plugin-data>/jobs.json); {} when unreadable."""
    try:
        reg = json.loads((phconf.data_dir() / "jobs.json").read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for key, entry in (reg.items() if isinstance(reg, dict) else ()):
        jid = entry.get("id") if isinstance(entry, dict) else entry
        if isinstance(jid, str) and jid:
            out[key] = jid
    return out


def missing_jobs(live: list[dict]) -> list[str]:
    """Names of the managed jobs the installer keeps for this config (ph_jobs.expected, the installer's own rule:
    only flags that are jobs, only when the connection is there, one per watch) that no live job carries, matched
    one to one by identity. First every expected job with an id the installer recorded claims the live job with that
    id (renamed or not), then each remaining expected job claims an unclaimed live job with its installed name. A
    live job counts for at most one expected job, so a duplicate or a misnamed job never stands in for another.
    LIVE is every cron job, not only the ones named with the desk prefix: a renamed job keeps its recorded id."""
    expected = ph_jobs.expected(phconf.CFG)
    recorded = registry_ids()
    used: set[int] = set()
    matched: set[str] = set()

    def claim(key: str, test) -> None:
        for i, job in enumerate(live):
            if i not in used and test(job):
                used.add(i)
                matched.add(key)
                return

    for key in expected:
        jid = recorded.get(key)
        if jid:
            claim(key, lambda job, jid=jid: job.get("id") == jid)
    for key in expected:
        if key not in matched:
            name = ph_jobs.job_name(key)
            claim(key, lambda job, name=name: (job.get("name") or "").strip() == name)
    return [ph_jobs.job_name(key) for key in expected if key not in matched]


def check_jobs(jobs: list[dict]) -> None:
    try:
        from croniter import croniter
    except Exception:
        croniter = None
    desk = [j for j in jobs if (j.get("name") or "").startswith(DESK_PREFIXES)]
    missing = missing_jobs(jobs)   # all live jobs: ids are matched before the prefix filter
    if missing:
        fault("desk", "desk job missing: " + ", ".join(missing))
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


def report(notify) -> list[str]:
    try:
        state = json.loads(HEALTH_STATE.read_text())
        state = state if isinstance(state, dict) else {}
    except Exception:
        state = {}
    t = now()
    due_desk = {}
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
            due_desk[key] = text
    out = list(due_desk.values())
    notify(out)   # enqueue or write to the local failure lane before marking any desk alert
    for key in due_desk:
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
    steps = [lambda: check_local_delivery(jobs), lambda: check_jobs(jobs),
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
    local = [text for cls, text in faults.values() if cls == "relay"]

    def notify(lines):
        if lines:
            import desk_say
            # An old unresolved receipt is not proof the current route is broken.
            problem = desk_say.route_problem(require_active=True) or any(
                text.startswith("desk messages held:") for text in local)
            for i in range(0, len(lines), 2):
                batch = lines[i:i + 2]
                if not problem:
                    text = (f"{phconf.owner()}, the desk has a fault and may miss things until it's fixed: "
                            + "; ".join(batch))
                    try:
                        receipt = desk_say.deliver(text, key="health:" + "|".join(sorted(batch))[:200], job="health")
                        if receipt.get("status") in {"queued", "handed", "accepted", "delivered"}:
                            continue
                        if receipt.get("status") == "duplicate" and desk_say.status_of(receipt.get("id")) in {
                            "queued", "handed", "accepted", "delivered"
                        }:
                            continue
                    except OSError:
                        pass
                local.extend(batch)
        if local:
            # Hermes records stderr in this no-agent job's local failure output.
            print("Proactive Hermes delivery health: " + "; ".join(local), file=sys.stderr)

    report(notify)
    return int(bool(local))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        log({"crash": f"{type(exc).__name__}: {exc}"[:200]})
        sys.exit(0)
