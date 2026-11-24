#!/usr/bin/env python3
"""Phone reminder and urgent-alarm outbox for devices that may be offline.

The bridge expires pending requests after ~15 minutes. Each retry uses a new
bridge key only after the previous request is explicitly expired. Pending,
running, or unreadable requests are not replaced. Confirmations go in the desk
ledger; queueing alone does not mean the phone created the reminder or alarm.

  ea_reminders.py add --key KEY --title T [--due ISO | --due-date D] [--notes N] [--priority P]
  ea_reminders.py alarm --key KEY --at ISO --label TEXT  # urgent only, requires automatic alarm authority
  ea_reminders.py adopt --key KEY --request-id RID       # track a reminder made via the tool
  ea_reminders.py tick                                  # check / re-queue (silent)
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
MAX_ATTEMPTS = 40          # ~10h of 15-minute retries while the phone sleeps
TTL = 900


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
        if not any(i.get("status") == "queued" for i in box.values()):
            return 0
        _, store = bridge()
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


def cmd_status(_a) -> int:
    print(json.dumps(load(OUTBOX, {}), indent=1, ensure_ascii=False))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    ad = sub.add_parser("add")
    ad.add_argument("--key", required=True)
    ad.add_argument("--title", required=True)
    ad.add_argument("--due")
    ad.add_argument("--due-date", dest="due_date")
    ad.add_argument("--notes", default="")
    ad.add_argument("--priority", choices=["none", "low", "medium", "high"], default="medium")
    ad.add_argument("--force", action="store_true", help="ignore the reminder daily cap")
    alarm = sub.add_parser("alarm", help="queue an urgent, one-time alarm")
    alarm.add_argument("--key", required=True)
    alarm.add_argument("--at", required=True)
    alarm.add_argument("--label", required=True)
    ap = sub.add_parser("adopt")
    ap.add_argument("--key", required=True)
    ap.add_argument("--request-id", required=True)
    sub.add_parser("tick")
    sub.add_parser("status")
    a = p.parse_args()
    if not enabled():
        print(json.dumps({"status": "disabled"}))
        return 0
    return {"add": cmd_add, "alarm": cmd_alarm, "adopt": cmd_adopt, "tick": cmd_tick, "status": cmd_status}[a.cmd](a)


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
