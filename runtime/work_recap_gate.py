#!/usr/bin/env python3
"""Wake gate for the work recaps (no LLM unless there is something to do).

Check for the daily-call transcript during the configured poll window in the call's timezone.
Both modes respect configured workdays and, when available, public holiday calendars.

  work_recap_gate.py --mode am        gate for the recap after the daily call
  work_recap_gate.py --mode pm        evening gate: workday/holiday check only in the owner's timezone
  work_recap_gate.py done --mode am   the agent marks today's recap done (stops further checks)

Prints {"wakeAgent": false} or {"wakeAgent": true, "context": {...}} as the last stdout line.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time

import phconf


def state_path():
    return phconf.state_dir() / "work_recap_state.json"


def day_off(d: date) -> str | None:
    if d.weekday() not in phconf.CFG["workdays"]:
        return "not a workday"
    try:
        import holidays
    except ImportError:
        return None  # never silently skip a workday because the library is missing
    for cc in phconf.CFG["holidays"]:
        name = holidays.country_holidays(cc, years=[d.year]).get(d)
        if name:
            return f"{cc} holiday: {name}"
    return None


def load() -> dict:
    try:
        return json.loads(state_path().read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save(data: dict) -> None:
    state = state_path()
    tmp = state.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    tmp.replace(state)


def gate(mode: str, now: datetime) -> dict:
    local = now.astimezone(phconf.daily_tz() if mode == "am" else phconf.tz())
    today = local.date()
    off = day_off(today)
    if off:
        return {"wakeAgent": False, "reason": off}
    if mode == "pm":
        return {"wakeAgent": True, "context": {"date": today.isoformat()}}
    daily = phconf.CFG["daily_call"]
    start, end = daily["poll_start"], daily["poll_end"]
    poll_start, poll_end = (time.fromisoformat(value) for value in (start, end))
    t = local.time().replace(second=0, microsecond=0)
    if not poll_start <= t <= poll_end:
        return {"wakeAgent": False, "reason": f"outside {start}-{end} daily-call time"}
    data = load()
    key = f"am:{today.isoformat()}"
    if data.get(key, {}).get("done"):
        return {"wakeAgent": False, "reason": "already done today"}
    row = data.setdefault(key, {"attempts": 0})
    row["attempts"] += 1
    row["last_check"] = local.isoformat(timespec="minutes")
    save(data)
    final = t == poll_end
    return {"wakeAgent": True, "context": {"date": today.isoformat(), "daily_time": local.strftime("%H:%M"),
                                           "attempt": row["attempts"], "final_attempt": final}}


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", nargs="?", default="gate", choices=["gate", "done"])
    p.add_argument("--mode", choices=["am", "pm"], default="am")
    p.add_argument("--now", help="ISO time, tests only")
    a = p.parse_args(argv)
    local_zone = phconf.tz()
    now = datetime.fromisoformat(a.now) if a.now else datetime.now(local_zone)
    now = now.astimezone(local_zone) if now.tzinfo else now.replace(tzinfo=local_zone)
    if a.cmd == "done":
        data = load()
        local = now.astimezone(phconf.daily_tz() if a.mode == "am" else local_zone)
        row = data.setdefault(f"{a.mode}:{local.date().isoformat()}", {"attempts": 0})
        row.update(done=True, done_at=now.isoformat(timespec="minutes"))
        save(data)
        print(json.dumps({"status": "done"}))
        return 0
    print(json.dumps(gate(a.mode, now)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
