#!/usr/bin/env python3
"""Phone reminder and urgent-alarm outbox for devices that may be offline.

The bridge expires pending requests after ~15 minutes. Each retry uses a new
bridge key only after the previous request is explicitly expired. Pending,
running, or unreadable requests are not replaced. Confirmations go in the desk
ledger; queueing alone does not mean the phone created the reminder or alarm.
A reminder the desk created (it has the id) is ticked off on the phone once its item is
finished in the promise, goal or action records; the same expiry rules apply to that request.

  ea_reminders.py add --key KEY --title T [--due ISO | --due-date D] [--notes N] [--priority P]
                      [--no-calendar-alert]
  ea_reminders.py alarm --key KEY --at ISO --label TEXT  # urgent only
  ea_reminders.py adopt --key KEY --request-id RID       # track a reminder made via the tool
  ea_reminders.py close --key KEY [--evidence E]        # item is done: tick the phone reminder off
  ea_reminders.py tick                                  # check / re-queue / close finished items (silent)
  ea_reminders.py status

Authority is enforced here, at the execution boundary (enqueue), never only in a prompt or the CLI: a reminder
needs `reminders` and an alarm `alarms` in the automatic authority (phconf.authorize). 'ask' and 'never' always
refuse: this script never acts on an ask-first action. When the owner says yes in chat to an ask-first reminder or
alarm, the chat agent sets it with Hermes's own mobile tools, which carry Hermes's own approvals. A refusal prints
{"status": "not_allowed", ...}, is logged to run_log.jsonl, exits 5 (add and alarm) and nothing reaches the bridge.
Retries of an expired request follow the same rule: once the action is no longer automatic the item is marked
'not_allowed' (terminal), logged once and never polled again. Cleanup is a phone write too: ticking off the desk's
own reminder needs `reminders` (and every authority it was queued under) automatic. Under ask or never, `close`
answers not_allowed and exits 5, tick leaves it alone, and the chat flow ticks it off with Hermes's own tools after
the owner's yes; a close retry that is no longer allowed ends 'close_not_allowed' (terminal, logged once).

Exit codes (add, alarm, close): 0 queued or another receipt, 2 bad arguments, 5 not allowed by the configured
authority.

Phone access goes through the Hermes mobile device plugin's bridge (see bridge() below: the only code here that
touches that plugin's files). The desk never wakes the phone itself.

Time-critical = calendar alert. The bridge delivers only when the phone wakes the Hermes app, which can
take minutes (an alarm that arrives after its time is refused). With a calendar connected, calendar_blocks
in the automatic authority and features.calendar_alerts on (the default), a reminder that is priority high or due within 12 h ALSO gets
a calendar alert (key 'ea:alert:<key>', popup at 0 min, ea_calendar_block.py alert) that the phone fires
by itself; the phone reminder is a bonus. The alert is made only after the outbox accepted the request
(status queued), so a capped or duplicate request never leaves a stray alert. When a queued request expires
or fails and the due time is still more than 2 min away, the alert is made sure of (idempotent). Alarms always
get the alert; an alarm that expires or comes back 'already passed' is logged, never retried. The outbox is
read and written only under its lock, and never held across a calendar call: tick collects the alerts it
needs, releases the lock, makes them, then re-locks to record their ids.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

import phconf

PROFILE = phconf.hermes_home()
OUTBOX = "reminder_outbox.json"
LEDGER = "actions.json"
RUN_LOG = "run_log.jsonl"
BRIDGE_PLUGIN = "hermes-mobile-device-access"
PLUGIN = PROFILE / "plugins" / BRIDGE_PLUGIN
PROMISES = "promises.json"
GOALS = "goals.json"
MAX_ATTEMPTS = 40          # ~10h of 15-minute retries while the phone sleeps
TTL = 900
DONE = {"done", "closed", "dropped", "resolved", "cancelled", "complete", "completed"}
# Outbox keys that track the same thing as a promise or goal under another name: {outbox_key: record_key}.
ALIASES: dict[str, str] = {}
CAL_BLOCK = Path(__file__).resolve().parent / "ea_calendar_block.py"
CRITICAL_WITHIN = timedelta(hours=12)   # due this soon = time-critical, gets a calendar alert
RESCUE_LEAD = timedelta(minutes=2)      # bridge gave up: still worth an alert if due is further than this


def now_iso() -> str:
    return datetime.now(phconf.tz()).isoformat(timespec="seconds")


def load(name: str, default):
    try:
        return json.loads((phconf.state_dir() / name).read_text())
    except FileNotFoundError:
        return default


def save(name: str, data) -> None:
    path = phconf.state_dir() / name
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as fh:
        tmp = Path(fh.name)
        try:
            json.dump(data, fh, indent=1, ensure_ascii=False, sort_keys=True)
            fh.close()
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


@contextmanager
def state_lock(name: str):
    with (phconf.state_dir() / (name + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def log(row: dict) -> None:
    with (phconf.state_dir() / RUN_LOG).open("a") as fh:
        fh.write(json.dumps({"at": now_iso(), "job": "reminders", **row}, ensure_ascii=False) + "\n")


def auto_allowed(action: str) -> bool:
    """Same rule as everywhere else: 'never' wins, then the automatic list."""
    return phconf.allowed(action) == "auto"


def item_action(item: dict) -> str:
    """The authority an outbox item is created under."""
    return "alarms" if item_kind(item) == "alarm" else "reminders"


NOT_ALLOWED_EXIT = 5   # same exit code as ea_calendar_block.py for a refusal


def refusal(action: str, basis: str) -> dict:
    return {"status": "not_allowed", "action": action, "authority": basis}


def refuse(key: str, action: str, basis: str) -> dict:
    """Log a refusal to run_log.jsonl and return its receipt."""
    log({"not_allowed": key, "action": action, "authority": basis})
    return refusal(action, basis)


def retry_block(item: dict) -> tuple[str, str] | None:
    """A re-request is still a creation: (action, level) of the first authority that is no longer automatic
    (the item's own action, then every required_authority like leave_time for trips), or None when it may retry."""
    for action in (item_action(item), *item.get("required_authority", ())):
        level = phconf.allowed(action)
        if level != "auto":
            return action, level
    return None


def enabled() -> bool:
    return phconf.CFG["reminders"]["enabled"] and (PLUGIN / "bridge.py").is_file()


def bridge(*, read_only: bool = False):
    """THE ONLY code in this plugin that touches the Hermes mobile device plugin's files (disclosed in the README).

    Neither Hermes nor that plugin offers an API a scheduled script can call: its mobile_* tools run only inside an
    agent turn, and it has no CLI. So the outbox uses the plugin's own bridge.py (open_store) on its own database,
    <HERMES_HOME>/plugin-data/hermes-mobile-device-access/bridge.db, for creating and completing reminders and
    alarms and for reading request results.

    read_only=True: a read-only SQLite connection to that database (health checks and the trip planner's last known
    location). It never creates the file; a phone that was never paired raises sqlite3.OperationalError.
    Otherwise: (bridge module, its Store)."""
    db = PROFILE / "plugin-data" / BRIDGE_PLUGIN / "bridge.db"
    if read_only:
        import sqlite3
        return sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    base = PROFILE.parent.parent if PROFILE.parent.name == "profiles" else PROFILE
    agent = base / "hermes-agent"
    if agent.is_dir():
        sys.path.insert(0, str(agent))
    spec = importlib.util.spec_from_file_location("proactive_hermes_mobile_bridge", PLUGIN / "bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, mod.open_store(PROFILE)


@state_lock(LEDGER)
def ledger_set(key: str, patch: dict) -> None:
    led = load(LEDGER, {})
    row = led.get(key, {})
    row.update(patch)
    row["updated_at"] = now_iso()
    led[key] = row
    save(LEDGER, led)


def item_kind(item: dict) -> str:
    return "alarm" if item.get("operation") == "alarms.create" else "reminder"


def submit(store, box: dict, item: dict) -> None:
    attempt = int(item.get("attempts", 0)) + 1
    if item_kind(item) == "alarm":
        operation = "alarms.create"
        args = {f: item[f] for f in ("at", "label")}
        # The bridge limits keys to 128 characters; preserve both key identity and attempt.
        digest = hashlib.sha256(item["key"].encode()).hexdigest()
        idem = f"proactive-hermes:alarm:{digest}:{attempt}"
    else:
        operation = "reminders.create"
        args = {"title": item["title"]}
        args.update({f: item[f] for f in ("due", "due_date", "notes", "priority") if item.get(f)})
        # Keep the existing bridge idempotency format for reminder attempts already on disk.
        idem = f"proactive-hermes:{item['key']}:rem:{attempt}"[:128]
    req = store.request(operation, args, idempotency_key=idem, ttl=TTL)
    item.update(attempts=attempt, request_id=req["id"], last_try=now_iso(), status="queued")
    box[item["key"]] = item
    save(OUTBOX, box)


def alert_key(key: str) -> str:
    return "ea:alert:" + key


def parse_due(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=phconf.tz())


def time_critical(priority, due) -> bool:
    """Priority high, or a due time within the next 12 h (past due counts too; it just gets no alert)."""
    if str(priority or "").lower() == "high":
        return True
    when = parse_due(due)
    return bool(when and when - datetime.now(phconf.tz()) <= CRITICAL_WITHIN)


def calendar_cli(args: list[str], texts: dict[str, str] | None = None) -> dict:
    """Run ea_calendar_block.py. Text goes through --NAME-file in a private temp dir, never argv."""
    tmpdir = tempfile.mkdtemp(prefix="ph_alert_")
    try:
        argv = [sys.executable, str(CAL_BLOCK), *args]
        for name, text in (texts or {}).items():
            path = Path(tmpdir) / f"{name}.txt"
            path.write_text(text or "", encoding="utf-8")
            argv += [f"--{name}-file", str(path)]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        try:
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            return {"status": "error", "error": (proc.stderr or proc.stdout)[-200:]}
    finally:
        for f in Path(tmpdir).glob("*"):
            f.unlink(missing_ok=True)
        os.rmdir(tmpdir)


def calendar_alert(key: str, title: str, at: str, notes: str = "") -> dict:
    return calendar_cli(["alert", "--key", key, "--at", at], {"title": title, "notes": notes})


def ensure_alert(item: dict, lead: timedelta = timedelta(0), why: str = "time-critical") -> dict | None:
    """Make sure the item's calendar alert exists when its time is still more than `lead` away.
    Returns the alert result, or None when nothing was needed (off, opted out, no time, already made, too late)."""
    if item.get("no_calendar_alert") or not phconf.calendar_alerts_on():
        return None
    when = parse_due(item.get("at") if item_kind(item) == "alarm" else item.get("due"))
    if not when or when - datetime.now(phconf.tz()) <= lead:
        return None
    if (item.get("calendar_alert") or {}).get("status") in {"created", "updated"}:
        return None
    res = calendar_alert(alert_key(item["key"]), item.get("label") or item.get("title") or item["key"],
                         when.isoformat(), item.get("notes") or "")
    item["calendar_alert"] = {"key": alert_key(item["key"]), "id": res.get("id"), "status": res.get("status"),
                              "at": now_iso(), "why": why}
    if res.get("id"):
        ledger_set(item["key"], {"calendar_alert_id": res["id"], "calendar_alert_status": res.get("status")})
    log({"calendar_alert": alert_key(item["key"]), "status": res.get("status"), "why": why,
         **({"error": str(res.get("error"))[:200]} if res.get("error") else {})})
    return res


def alert_recorded(key: str) -> bool:
    """Do the desk's records say an 'ea:alert:<key>' event exists? (no calendar call when they say no)"""
    led = read_records(LEDGER, {})
    row = led.get(key) if isinstance(led, dict) else None
    if isinstance(row, dict) and row.get("calendar_alert_id"):
        return True
    akey, live = alert_key(key), False
    try:
        with (phconf.state_dir() / "actions_log.jsonl").open(encoding="utf-8") as fh:
            for line in fh:
                if akey not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("key") != akey:
                    continue
                if entry.get("kind") == "calendar_alert" and entry.get("id"):
                    live = True
                elif entry.get("kind") == "calendar_block_deleted":
                    live = False
    except FileNotFoundError:
        return False
    return live


def delete_alert(key: str) -> dict | None:
    """Delete KEY's calendar alert if the records show one (only events this plugin created)."""
    if not phconf.CFG["calendar"]["enabled"] or not alert_recorded(key):
        return None
    return calendar_cli(["delete", "--key", alert_key(key)])


def read_records(name: str, default):
    """Tolerant read of a record file this module never writes: unreadable counts as empty."""
    try:
        return load(name, default)
    except (OSError, ValueError):
        return default


def goal_rows(data) -> list:
    """goals.json is {"goals": [...]} (or a bare list)."""
    rows = data.get("goals", []) if isinstance(data, dict) else data
    return rows if isinstance(rows, list) else []


def finished_keys() -> dict[str, str]:
    """Keys whose item is finished somewhere in the desk's records, with the evidence."""
    out: dict[str, str] = {}
    promises, ledger = read_records(PROMISES, {}), read_records(LEDGER, {})
    for k, v in (promises if isinstance(promises, dict) else {}).items():
        if isinstance(v, dict) and str(v.get("status", "")).lower() in DONE:
            out[k] = str(v.get("evidence") or v.get("closed_evidence") or "promise closed")[:200]
    for g in goal_rows(read_records(GOALS, {})):
        if isinstance(g, dict) and g.get("key") and str(g.get("status", "")).lower() in DONE:
            out[g["key"]] = str(g.get("evidence") or "goal closed")[:200]
    for k, v in (ledger if isinstance(ledger, dict) else {}).items():
        if isinstance(v, dict) and (str(v.get("status", "")).lower() in DONE or v.get("closed")):
            out[k] = str(v.get("evidence") or v.get("closed") or "action closed")[:200]
    return out


def close_block(item: dict) -> tuple[str, str] | None:
    """Ticking a reminder off is a phone write: (action, level) of the first authority that is not automatic (the
    item's own action, then every required_authority it was queued under), or None when it may be ticked off."""
    for action in ("reminders", *item.get("required_authority", ())):
        ok, refused, level = phconf.authorize_cleanup(action)
        if not ok:
            return refused, level
    return None


def close_allowed(item: dict) -> bool:
    return close_block(item) is None


def _finished_evidence(key: str, done: dict[str, str]) -> str | None:
    return done.get(key) or done.get(ALIASES.get(key, ""))


def closable(box: dict) -> bool:
    done = finished_keys()
    return any(i.get("status") == "landed" and i.get("reminder_id") and _finished_evidence(k, done)
               and close_allowed(i) for k, i in box.items())


def request_close(store, item: dict, evidence: str) -> dict:
    attempt = int(item.get("close_attempts", 0)) + 1
    # The bridge limits keys to 128 characters; hash the item key so the attempt is never truncated away.
    digest = hashlib.sha256(item["key"].encode()).hexdigest()
    req = store.request("reminders.complete", {"id": item["reminder_id"]},
                        idempotency_key=f"proactive-hermes:done:{digest}:{attempt}", ttl=TTL)
    item.update(status="closing", close_attempts=attempt, close_request_id=req["id"],
                close_evidence=evidence[:200], close_try=now_iso())
    return item


def close_finished(store, box: dict) -> bool:
    """Tick off phone reminders whose item is finished. Only reminders the desk created (it has their id)."""
    done = finished_keys()
    changed = False
    for key, item in box.items():
        if item.get("status") != "landed" or not item.get("reminder_id") or not close_allowed(item):
            continue
        evidence = _finished_evidence(key, done)
        if evidence:
            try:
                request_close(store, item, evidence)
            except Exception as exc:  # leave the item landed; the next tick tries again
                log({"close_error": key, "error": f"{type(exc).__name__}: {exc}"[:200]})
                continue
            save(OUTBOX, box)  # the request exists now; never lose track of it
            log({"closing": key, "evidence": evidence})
            changed = True
    return changed


def check_closing(store, box: dict) -> bool:
    changed = False
    for key, item in box.items():
        if item.get("status") != "closing":
            continue
        if item.get("close_request_id"):
            try:
                res = store.result(item["close_request_id"])
            except Exception:
                continue  # A failed lookup is not evidence the phone didn't act.
        else:
            res = {"status": "expired"}
        status = res.get("status")
        if status == "succeeded":
            item.update(status="done", done=now_iso())
            ledger_set(key, {"reminder_status": "done"})
            log({"closed": key})
        elif status in {"pending", "running"}:
            continue
        elif status == "expired" and int(item.get("close_attempts", 0)) < MAX_ATTEMPTS:
            blocked = close_block(item)
            if blocked:
                # No longer automatic: terminal, logged once, never polled again; the chat flow can tick it off.
                action, level = blocked
                item.update(status="close_not_allowed", authority=level, close_error=f"close refused: {action} is {level}")
                ledger_set(key, {"reminder_status": "close_not_allowed"})
                log({"not_allowed": key, "action": action, "authority": level, "close": True})
                save(OUTBOX, box)
                changed = True
                continue
            try:
                request_close(store, item, item.get("close_evidence", ""))
            except Exception as exc:
                log({"close_retry_error": key, "error": f"{type(exc).__name__}: {exc}"[:200]})
                continue
        else:
            # Gone on the phone already (deleted or completed by hand) counts as done.
            err = str(res.get("error") or status)
            item.update(status="done" if "not found" in err.lower() else "close_failed", close_error=err[:200])
            log({"close_result": key, "why": err[:200]})
        save(OUTBOX, box)
        changed = True
    return changed


def close_reminder(key: str, evidence: str = "") -> dict:
    """Tick off the phone reminder the desk created for KEY. Returns a receipt; nothing else is touched."""
    if not enabled():
        return {"status": "disabled"}
    with state_lock(OUTBOX):
        box = load(OUTBOX, {})
        item = box.get(key)
        if not item or not item.get("reminder_id"):
            return {"status": "no_reminder", "key": key}
        if item.get("status") in {"closing", "done"}:
            return {"status": item["status"], "key": key}
        blocked = close_block(item)
        if blocked:
            action, level = blocked
            log({"not_allowed": key, "action": action, "authority": level, "close": True})
            return {**refusal(action, level), "key": key, "reminder_id": item.get("reminder_id")}
        _, store = bridge()
        request_close(store, item, evidence or "closed by owner")
        save(OUTBOX, box)
        log({"closing": key, "evidence": item["close_evidence"]})
        return {"status": "closing", "key": key, "request_id": item["close_request_id"]}


def enqueue(item: dict, *, force: bool = False) -> dict:
    """THE creation boundary: every reminder and alarm reaches the bridge through here, after its authority."""
    key, kind, action = item["key"], item_kind(item), item_action(item)
    ok, basis = phconf.authorize(action)
    if not ok:
        return refuse(key, action, basis)
    for extra in item.get("required_authority", ()):
        if not auto_allowed(extra):
            return refuse(key, extra, phconf.allowed(extra))
    with state_lock(OUTBOX):
        box = load(OUTBOX, {})
        if key in box:
            existing = box[key]
            if item_kind(existing) != kind:
                return {"status": "refused", "reason": "key already belongs to a different action"}
            return {**existing, "status": "exists", "outbox_status": existing["status"]}
        created = now_iso()
        today = created[:10]
        made_today = sum(1 for i in box.values() if item_kind(i) == kind and str(i.get("created", "")).startswith(today))
        config = phconf.CFG["reminders"]
        cap = config.get("alarms_per_day", 3) if kind == "alarm" else config["per_day"]
        if made_today >= cap and not force:
            return {"status": "refused", "reason": f"daily cap {cap} {kind}s reached"}
        item.update(attempts=0, created=created)
        _, store = bridge()
        submit(store, box, item)
        ledger_set(key, {f"{kind}_status": "queued"})
        return {"status": "queued", "key": key, "request_id": item["request_id"]}


def make_alerts(pending: list[tuple[dict, timedelta, str]]) -> dict[str, dict]:
    """Create the calendar alerts for (item copy, lead, why) OUTSIDE the outbox lock, then re-lock once to
    record what was made. Returns {key: calendar_alert} for the alerts created now."""
    made: dict[str, dict] = {}
    for item, lead, why in pending:
        try:
            res = ensure_alert(item, lead, why=why)
        except Exception as exc:  # the phone path still runs
            log({"calendar_alert_error": item.get("key"), "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        if res is not None and item.get("calendar_alert"):
            made[item["key"]] = item["calendar_alert"]
    if made:
        with state_lock(OUTBOX):
            box = load(OUTBOX, {})
            for key, alert in made.items():
                if isinstance(box.get(key), dict):
                    box[key]["calendar_alert"] = alert
            save(OUTBOX, box)
    return made


def with_alert(item: dict, result: dict, why: str) -> dict:
    """After enqueue: only a request the outbox accepted (queued) gets its calendar alert."""
    if result.get("status") != "queued":
        return result
    made = make_alerts([(dict(item), timedelta(0), why)])
    if item["key"] in made:
        result["calendar_alert"] = made[item["key"]]
    return result


def add(key: str, title: str, *, due=None, due_date=None, notes="", priority="medium", force=False,
        required_authority=(), calendar_alert: bool = True) -> dict:
    """Queue a reminder and return its receipt without a CLI round trip. Needs `reminders` (and every
    required_authority) automatic; anything else is refused."""
    if not enabled():
        return {"status": "disabled"}
    item = {"key": key, "title": title, "due": due, "due_date": due_date, "notes": notes, "priority": priority}
    if required_authority:
        item["required_authority"] = list(required_authority)
    if not calendar_alert:
        item["no_calendar_alert"] = True
    result = enqueue(item, force=force)
    if calendar_alert and time_critical(priority, due):
        with_alert(item, result, "time-critical")
    return result


def alarm(key: str, at_iso: str, label: str) -> dict:
    """Queue an urgent one-time alarm (plus its calendar alert). Needs `alarms` automatic; anything else is refused."""
    if not enabled():
        return {"status": "disabled"}
    ok, basis = phconf.authorize("alarms")
    if not ok:  # checked again in enqueue; refusing first keeps the input checks from leaking past a deny
        return refuse(key, "alarms", basis)
    try:
        at = datetime.fromisoformat(at_iso.replace("Z", "+00:00"))
    except ValueError:
        at = None
    if at is None or at.utcoffset() is None:
        return {"status": "error", "error": "--at must be an ISO date and time with timezone"}
    if len(label) > 100:
        return {"status": "error", "error": "--label must be at most 100 characters"}
    if at <= datetime.now(phconf.tz()):
        return {"status": "refused", "reason": "alarm time already passed"}
    item = {"operation": "alarms.create", "key": key, "at": at_iso, "label": label}
    return with_alert(item, enqueue(item), "alarm")


def receipt_exit(result: dict) -> int:
    """Print the receipt; a refusal exits non-zero so a caller cannot read it as success."""
    print(json.dumps(result, ensure_ascii=False))
    return NOT_ALLOWED_EXIT if result.get("status") == "not_allowed" else 0


def cmd_add(a) -> int:
    return receipt_exit(add(a.key, a.title, due=a.due, due_date=a.due_date, notes=a.notes, priority=a.priority,
                            force=a.force, calendar_alert=not a.no_calendar_alert))


def cmd_alarm(a) -> int:
    return receipt_exit(alarm(a.key, a.at, a.label))


def cmd_adopt(a) -> int:
    with state_lock(OUTBOX):
        box = load(OUTBOX, {})
        if a.key in box:
            print(json.dumps({"status": "exists", "key": a.key}))
            return 0
        _, store = bridge()
        req = store.result(a.request_id)
        if req.get("operation") != "reminders.create":
            print(json.dumps({"status": "refused", "reason": "can only adopt a reminder creation request"}))
            return 0
        args = req["arguments"]
        box[a.key] = {"key": a.key, "title": args.get("title"), "due": args.get("due"), "due_date": args.get("due_date"),
                      "notes": args.get("notes"), "priority": args.get("priority"), "attempts": 1,
                      "request_id": a.request_id, "created": now_iso(), "status": "queued"}
        save(OUTBOX, box)
        print(json.dumps({"adopted": a.key, "request_status": req["status"]}))
    return 0


def cmd_tick(_a) -> int:
    alerts: list[tuple[dict, timedelta, str]] = []   # made after the lock is released (calendar calls are slow)

    def need_alert(item: dict, why: str) -> None:
        alerts.append((json.loads(json.dumps(item)), RESCUE_LEAD, why))

    with state_lock(OUTBOX):
        box = load(OUTBOX, {})
        if not any(i.get("status") in {"queued", "closing"} for i in box.values()) and not closable(box):
            return 0
        _, store = bridge()
        close_finished(store, box)
        check_closing(store, box)
        for key, item in box.items():
            if item.get("status") != "queued":
                continue
            try:
                res = store.result(item["request_id"])
            except Exception:
                # A failed lookup is not evidence the phone didn't act.
                continue
            kind = item_kind(item)
            status = res.get("status")
            if status == "succeeded":
                data = ((res.get("result") or {}).get("data") or {})
                item_id = data.get("id")
                item.update(status="landed", landed=now_iso(), **{f"{kind}_id": item_id})
                ledger_set(key, {f"{kind}_id": item_id, f"{kind}_status": "landed"})
                log({"landed": key, f"{kind}_id": item_id, "attempts": item.get("attempts")})
            elif status in {"pending", "running"}:
                continue
            elif kind == "alarm":
                # Expired or 'The alarm time has already passed': log it, never retry. The calendar alert
                # (made when the alarm was queued) is the real safety net.
                item.update(status="expired" if status == "expired" else "failed",
                            error=str(res.get("error") or status)[:200])
                ledger_set(key, {"alarm_status": item["status"]})
                log({"alarm_failed": key, "why": item["error"], "no_retry": True})
                need_alert(item, f"alarm {item['status']}")
            elif status == "expired" and int(item.get("attempts", 0)) < MAX_ATTEMPTS:
                blocked = retry_block(item)
                if blocked:
                    # No longer automatic: terminal, logged once, never polled or listed as open again.
                    action, level = blocked
                    item.update(status="not_allowed", authority=level, error=f"retry refused: {action} is {level}")
                    ledger_set(key, {f"{kind}_status": "not_allowed"})
                    log({"not_allowed": key, "action": action, "authority": level, "retry": True})
                    save(OUTBOX, box)
                    continue
                need_alert(item, "bridge expired")  # the phone is not waking: make sure of the part that needs no app
                try:
                    submit(store, box, item)
                except Exception as exc:
                    log({"retry_error": key, "error": f"{type(exc).__name__}: {exc}"[:200]})
                    save(OUTBOX, box)
                continue
            else:
                item.update(status="failed", error=str(res.get("error") or status)[:200])
                ledger_set(key, {f"{kind}_status": "failed"})
                log({"failed": key, "why": item["error"], "attempts": item.get("attempts")})
                need_alert(item, "bridge failed")
            save(OUTBOX, box)
    make_alerts(alerts)
    return 0


def cmd_close(a) -> int:
    return receipt_exit(close_reminder(a.key, a.evidence))


def cmd_status(_a) -> int:
    print(json.dumps(load(OUTBOX, {}), indent=1, ensure_ascii=False))
    return 0


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


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    ad = sub.add_parser("add")
    ad.add_argument("--key", required=True)
    _text_option(ad, "title", required=True)
    ad.add_argument("--due")
    ad.add_argument("--due-date", dest="due_date")
    _text_option(ad, "notes", default="")
    ad.add_argument("--priority", choices=["none", "low", "medium", "high"], default="medium")
    ad.add_argument("--force", action="store_true", help="ignore the reminder daily cap")
    ad.add_argument("--no-calendar-alert", action="store_true",
                    help="do not add the calendar alert even when the reminder is time-critical")
    alarm = sub.add_parser("alarm", help="queue an urgent, one-time alarm")
    alarm.add_argument("--key", required=True)
    alarm.add_argument("--at", required=True)
    _text_option(alarm, "label", required=True)
    ap = sub.add_parser("adopt")
    ap.add_argument("--key", required=True)
    ap.add_argument("--request-id", required=True)
    cl = sub.add_parser("close", help="tick off the phone reminder for a finished item")
    cl.add_argument("--key", required=True)
    _text_option(cl, "evidence", default="")
    sub.add_parser("tick")
    sub.add_parser("status")
    a = p.parse_args()
    _load_text_files(a, p)
    if not enabled():
        print(json.dumps({"status": "disabled"}))
        return 0
    return {"add": cmd_add, "alarm": cmd_alarm, "adopt": cmd_adopt, "close": cmd_close,
            "tick": cmd_tick, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        try:
            log({"error": f"{type(exc).__name__}: {exc}"[:300]})
        except Exception:
            pass
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"[:300]}))
        sys.exit(0)
