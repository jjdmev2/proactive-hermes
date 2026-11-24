#!/usr/bin/env python3
"""Reminder outbox for devices that may be offline when a reminder is queued.

The device bridge expires a pending request after ~15 minutes. When the phone is idle,
a request can expire without reaching it. The outbox keeps each pending reminder and
re-queues it on the bridge until the phone confirms
it (status succeeded with a reminder id), then records the id in the action ledger.

No LLM, no push, no message: re-queued requests reuse the bridge's own background wake.
Idempotency: each attempt has a fresh bridge key derived from the stable outbox key plus
attempt number, but a new attempt is only made after the previous request EXPIRED
(never while one is pending or running), so a reminder can never be created twice.

  ea_reminders.py add --key KEY --title T [--due ISO | --due-date D] [--notes N] [--priority P]
  ea_reminders.py adopt --key KEY --request-id RID     # track a request made via the tool
  ea_reminders.py tick                                 # cron: check / re-queue (silent)
  ea_reminders.py status
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from datetime import datetime

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
    except Exception:
        return default


def save(name: str, data) -> None:
    path = phconf.state_dir() / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True))
    tmp.replace(path)


def log(row: dict) -> None:
    with (phconf.state_dir() / RUN_LOG).open("a") as fh:
        fh.write(json.dumps({"at": now_iso(), "job": "reminders", **row}, ensure_ascii=False) + "\n")


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
    """Ask the push relay to wake the phone so it fetches the request; best effort, never fatal.
    Same call the plugin makes (plugins/hermes-mobile-device-access/__init__.py wake), but reads
    the relay settings from the profile .env so it works outside a scoped Hermes process."""
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


def submit(store, item: dict) -> dict:
    args = {"title": item["title"]}
    for f in ("due", "due_date", "notes", "priority"):
        if item.get(f):
            args[f] = item[f]
    attempt = int(item.get("attempts", 0)) + 1
    req = store.request("reminders.create", args, idempotency_key=f"proactive-hermes:{item['key']}:rem:{attempt}"[:128],
                        ttl=TTL)
    item.update(attempts=attempt, request_id=req["id"], last_try=now_iso(), status="queued")
    wake(req["device_id"])
    return item


def cmd_add(a) -> int:
    box = load(OUTBOX, {})
    if a.key in box and box[a.key].get("status") in {"queued", "landed"}:
        print(json.dumps({"status": "exists", **box[a.key]}, ensure_ascii=False))
        return 0
    today = datetime.now(phconf.tz()).date().isoformat()
    made_today = sum(1 for i in box.values() if str(i.get("created", "")).startswith(today))
    daily_cap = phconf.CFG["reminders"]["per_day"]
    if made_today >= daily_cap and not a.force:
        print(json.dumps({"status": "refused", "reason": f"daily cap {daily_cap} reminders reached"}))
        return 0
    item = {"key": a.key, "title": a.title, "due": a.due, "due_date": a.due_date, "notes": a.notes,
            "priority": a.priority, "attempts": 0, "created": now_iso()}
    _, store = bridge()
    item = submit(store, item)
    box[a.key] = item
    save(OUTBOX, box)
    ledger_set(a.key, {"reminder_status": "queued"})
    print(json.dumps({"status": "queued", "key": a.key, "request_id": item["request_id"]}))
    return 0


def cmd_adopt(a) -> int:
    _, store = bridge()
    req = store.result(a.request_id)
    args = req["arguments"]
    box = load(OUTBOX, {})
    box[a.key] = {"key": a.key, "title": args.get("title"), "due": args.get("due"), "due_date": args.get("due_date"),
                  "notes": args.get("notes"), "priority": args.get("priority"), "attempts": 1,
                  "request_id": a.request_id, "created": now_iso(), "status": "queued"}
    save(OUTBOX, box)
    print(json.dumps({"adopted": a.key, "request_status": req["status"]}))
    return 0


def cmd_tick(_a) -> int:
    box = load(OUTBOX, {})
    if not any(i.get("status") == "queued" for i in box.values()):
        return 0
    _, store = bridge()
    changed = False
    for key, item in box.items():
        if item.get("status") != "queued":
            continue
        try:
            res = store.result(item["request_id"])
        except Exception as exc:
            res = {"status": "expired", "error": str(exc)}
        status = res.get("status")
        if status == "succeeded":
            data = ((res.get("result") or {}).get("data") or {})
            item.update(status="landed", reminder_id=data.get("id"), landed=now_iso())
            ledger_set(key, {"reminder_id": data.get("id"), "reminder_status": "landed"})
            log({"landed": key, "reminder_id": data.get("id"), "attempts": item.get("attempts")})
            changed = True
        elif status in {"pending", "running"}:
            continue
        elif status == "expired" and int(item.get("attempts", 0)) < MAX_ATTEMPTS:
            submit(store, item)
            changed = True
        else:
            item.update(status="failed", error=str(res.get("error") or status)[:200])
            ledger_set(key, {"reminder_status": "failed"})
            log({"failed": key, "why": item["error"], "attempts": item.get("attempts")})
            changed = True
    if changed:
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
    ad.add_argument("--force", action="store_true", help="ignore the daily cap")
    ap = sub.add_parser("adopt")
    ap.add_argument("--key", required=True)
    ap.add_argument("--request-id", required=True)
    sub.add_parser("tick")
    sub.add_parser("status")
    a = p.parse_args()
    if not phconf.CFG["reminders"]["enabled"] or not (PLUGIN / "bridge.py").is_file():
        print(json.dumps({"status": "disabled"}))
        return 0
    return {"add": cmd_add, "adopt": cmd_adopt, "tick": cmd_tick, "status": cmd_status}[a.cmd](a)


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
