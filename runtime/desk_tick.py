#!/usr/bin/env python3
"""Desk tick (no-agent cron, every 10 min): phone-reminder outbox, the Bot Chat message outbox (kicks the
"Proactive · say" job when a message is due), and archiving the plugin's own finished scheduled-run sessions
(cron sessions titled "Proactive · ...") so the app sidebar never shows desk machinery. Other sessions are never
touched. Findings reach the owner only via desk_say. Archive only, never delete; recover with
`hermes sessions list --include-archived` in the current profile.
Prints nothing."""
import subprocess
import sys
from pathlib import Path

import phconf
import ph_jobs

HERE = Path(__file__).resolve().parent
PROFILE = phconf.profile_name()
HERMES = ["hermes", "-p", PROFILE] if PROFILE != "default" else ["hermes"]
STEPS = [
    [sys.executable, str(HERE / "ea_reminders.py"), "tick"],
    # Rolled-over desk items go out on their own after an hour of quiet (no daily cap, nothing waits).
    [sys.executable, str(HERE / "ea_desk.py"), "flush"],
    [sys.executable, str(HERE / "desk_say.py"), "flush"],
    [*HERMES, "sessions", "archive", "--source", "cron", "--title", ph_jobs.PREFIX.strip(),
     "--older-than", "15m", "--yes"],
]
if __name__ == "__main__":
    for cmd in STEPS:
        try:
            subprocess.run(cmd, capture_output=True, timeout=240)
        except Exception:
            pass
    sys.exit(0)
