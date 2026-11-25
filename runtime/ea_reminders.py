#!/usr/bin/env python3
"""Phone reminder and urgent-alarm outbox for devices that may be offline.

The bridge expires pending requests after ~15 minutes. Each retry uses a new
bridge key only after the previous request is explicitly expired. Pending,
running, or unreadable requests are not replaced. Confirmations go in the desk
ledger; queueing alone does not mean the phone created the reminder or alarm.
A reminder the desk created (it has the id) is ticked off on the phone once its item is
finished in the promise, goal or action records; the same expiry rules apply to that request.

  ea_reminders.py add --key KEY --title T [--due ISO | --due-date D] [--notes N] [--priority P]
  ea_reminders.py alarm --key KEY --at ISO --label TEXT  # urgent only, requires automatic alarm authority
  ea_reminders.py adopt --key KEY --request-id RID       # track a reminder made via the tool
  ea_reminders.py close --key KEY [--evidence E]        # item is done: tick the phone reminder off
  ea_reminders.py tick                                  # check / re-queue / close finished items (silent)
  ea_reminders.py status
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

import phconf

PROFILE = phconf.hermes_home()
OUTBOX = "reminder_outbox.json"
LEDGER = "actions.json"
RUN_LOG = "run_log.jsonl"
PLUGIN = PROFILE / "plugins" / "hermes-mobile-device-access"
PROMISES = "promises.json"
GOALS = "goals.json"
MAX_ATTEMPTS = 40          # ~10h of 15-minute retries while the phone sleeps
TTL = 900
DONE = {"done", "closed", "dropped", "resolved", "cancelled", "complete", "completed"}
# Outbox keys that track the same thing as a promise or goal under another name: {outbox_key: record_key}.
ALIASES: dict[str, str] = {}


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


def enabled() -> bool:
    return phconf.CFG["reminders"]["enabled"] and (PLUGIN / "bridge.py").is_file()


def bridge():
    base = PROFILE.parent.parent if PROFILE.parent.name == "profiles" else PROFILE
    agent = base / "hermes-agent"
    if agent.is_dir():
        sys.path.insert(0, str(agent))
    spec = importlib.util.spec_from_file_location("proactive_hermes_mobile_bridge", PLUGIN / "bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, mod.open_store(PROFILE)


def wake(device_id: str) -> None:
    """Ask the push relay to wake the phone; best effort, never fatal."""
    try:
        import urllib.request
        from urllib.parse import quote
        env = {}
        for line in (PROFILE / ".env").read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
        relay = env.get("HERMES_MOBILE_PUSH_RELAY_URL", "").rstrip("/")
        key = env.get("HERMES_MOBILE_PUSH_INSTALL_KEY", "")
        if not relay.startswith("https://") or not key:
            return
        req = urllib.request.Request(f"{relay}/devices/{quote(device_id, safe='')}/wake", data=b"{}", method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                              "User-Agent": "proactive-hermes (reminder outbox)"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            resp.read(256)
    except Exception:
        pass


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
    wake(req["device_id"])


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


def close_allowed(item: dict) -> bool:
    """Ticking a reminder off is still a phone action: honour 'never' and the authority it was queued under."""
    return phconf.allowed("reminders") != "never" and all(map(auto_allowed, item.get("required_authority", ())))


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
    wake(req["device_id"])
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
            if not close_allowed(item):
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
        if not close_allowed(item):
            return {"status": "not_allowed", "key": key}
        _, store = bridge()
        request_close(store, item, evidence or "closed by owner")
        save(OUTBOX, box)
        log({"closing": key, "evidence": item["close_evidence"]})
        return {"status": "closing", "key": key, "request_id": item["close_request_id"]}


def enqueue(item: dict, *, force: bool = False) -> dict:
    key, kind = item["key"], item_kind(item)
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


def add(key: str, title: str, *, due=None, due_date=None, notes="", priority="medium", force=False,
        required_authority=()) -> dict:
    """Queue a reminder and return its receipt without a CLI round trip."""
    if not enabled():
        return {"status": "disabled"}
    if not all(map(auto_allowed, required_authority)):
        return {"status": "not_allowed"}
    item = {"key": key, "title": title, "due": due, "due_date": due_date, "notes": notes, "priority": priority}
    if required_authority:
        item["required_authority"] = list(required_authority)
    return enqueue(item, force=force)


def cmd_add(a) -> int:
    result = add(a.key, a.title, due=a.due, due_date=a.due_date, notes=a.notes, priority=a.priority, force=a.force)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_alarm(a) -> int:
    if not auto_allowed("alarms"):
        result = {"status": "not_allowed"}
    else:
        try:
            at = datetime.fromisoformat(a.at.replace("Z", "+00:00"))
        except ValueError:
            at = None
        if at is None or at.utcoffset() is None:
            result = {"status": "error", "error": "--at must be an ISO date and time with timezone"}
        elif len(a.label) > 100:
            result = {"status": "error", "error": "--label must be at most 100 characters"}
        else:
            result = enqueue({"operation": "alarms.create", "key": a.key, "at": a.at, "label": a.label})
    print(json.dumps(result, ensure_ascii=False))
    return 0


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
            elif status == "expired" and int(item.get("attempts", 0)) < MAX_ATTEMPTS:
                required = ("alarms",) if kind == "alarm" else item.get("required_authority", ())
                if not all(map(auto_allowed, required)):
                    continue
                try:
                    submit(store, box, item)
                except Exception as exc:
                    log({"retry_error": key, "error": f"{type(exc).__name__}: {exc}"[:200]})
                continue
            else:
                item.update(status="failed", error=str(res.get("error") or status)[:200])
                ledger_set(key, {f"{kind}_status": "failed"})
                log({"failed": key, "why": item["error"], "attempts": item.get("attempts")})
            save(OUTBOX, box)
    return 0


def cmd_close(a) -> int:
    print(json.dumps(close_reminder(a.key, a.evidence), ensure_ascii=False))
    return 0


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
