#!/usr/bin/env python3
"""The "Proactive · say" job (no-agent cron, --deliver bot-chat): print exactly one due desk message, or nothing.

Cron hands the printed text to this profile's Bot Chat, where the Bot replies with the message word for word.
Empty output is silent. See desk_say.py for the outbox, quiet hours and deduplication."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import desk_say  # noqa: E402

if __name__ == "__main__":
    try:
        desk_say.main(["next"])
    except Exception:
        pass   # never print a traceback into Bot Chat; the message stays queued or handed, health reports it
    sys.exit(0)
