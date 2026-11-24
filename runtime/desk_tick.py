#!/usr/bin/env python3
"""Desk tick (no-agent cron, every 10 min): phone-reminder outbox, Bot Chat message outbox, and
archiving finished scheduled-run sessions so the app sidebar never shows job machinery. Findings
reach the owner only via desk_say. Archive only, never delete; recover with
`hermes sessions list --include-archived` in the current profile.
Prints nothing."""
import subprocess
import sys
from pathlib import Path

import phconf

HERE = Path(__file__).resolve().parent
PROFILE = phconf.profile_name()
HERMES = ["hermes", "-p", PROFILE] if PROFILE != "default" else ["hermes"]
STEPS = [
    [sys.executable, str(HERE / "ea_reminders.py"), "tick"],
    # Rolled-over desk items go out on their own after an hour of quiet (no daily cap, nothing waits).
    [sys.executable, str(HERE / "ea_desk.py"), "flush"],
    [sys.executable, str(HERE / "desk_say.py"), "flush"],
    [sys.executable, "-c", "import sys; sys.path.insert(0, %r); import desk_say; desk_say.hide_relay_cards()" % str(HERE)],
    [*HERMES, "sessions", "archive", "--source", "cron", "--older-than", "15m", "--yes"],
    [*HERMES, "sessions", "archive", "--source", "push", "--older-than", "15m", "--yes"],
]
for cmd in STEPS:
    try:
        subprocess.run(cmd, capture_output=True, timeout=240)
    except Exception:
        pass
sys.exit(0)
