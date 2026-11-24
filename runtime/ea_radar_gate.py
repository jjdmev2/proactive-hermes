#!/usr/bin/env python3
"""Pre-run gate for the desk mail radar.

Wraps ea_mail_gate.py (header-only scan, per-folder cursors) and adds the desk's
pending queue (items from health or other desk-internal producers that need
the owner). Prints ONE last JSON line:

  {"wakeAgent": false}                                    nothing new: no LLM run
  {"wakeAgent": true, "context": {"mail": {...}, "pending": N}}

Fails open: a broken mail gate still wakes the agent with the error.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import phconf

HERE = Path(__file__).resolve().parent


def pending_count() -> int:
    try:
        pending = phconf.state_dir() / "pending_queue.jsonl"
        return sum(1 for line in pending.read_text().splitlines() if line.strip())
    except FileNotFoundError:
        return 0


def main() -> int:
    try:
        proc = subprocess.run([sys.executable, str(HERE / "ea_mail_gate.py")], capture_output=True,
                              text=True, timeout=240)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        gate = json.loads(lines[-1]) if lines else {"wakeAgent": True, "context": {"gate_error": "empty gate output"}}
    except Exception as exc:  # fail open
        gate = {"wakeAgent": True, "context": {"gate_error": f"{type(exc).__name__}: {exc}"[:200]}}
    pending = pending_count()
    if gate.get("wakeAgent") is False and not pending:
        print(json.dumps({"wakeAgent": False}))
        return 0
    context = {"mail": gate.get("context") if gate.get("wakeAgent") is not False else {"new_mail": 0},
               "pending": pending}
    print(json.dumps({"wakeAgent": True, "context": context}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
