#!/usr/bin/env python3
"""Local */10 maintenance: reminders, held/overflow items and the existing notice outbox.

Does not archive sessions; Hermes's public title filter uses substrings, not job ownership.
Prints nothing.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STEPS = [
    [sys.executable, str(HERE / "ea_reminders.py"), "tick"],
    # Rolled-over desk items go out on their own after an hour of quiet (no daily cap, nothing waits).
    [sys.executable, str(HERE / "ea_desk.py"), "flush"],
    [sys.executable, str(HERE / "desk_say.py"), "flush"],
]
if __name__ == "__main__":
    for cmd in STEPS:
        try:
            subprocess.run(cmd, capture_output=True, timeout=240)
        except Exception:
            pass
    sys.exit(0)
