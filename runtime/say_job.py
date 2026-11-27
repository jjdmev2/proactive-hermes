#!/usr/bin/env python3
"""The "Proactive · say" job (no-agent cron, --deliver bot-chat): print exactly one due desk message, or nothing.

Cron relays the printed text to this profile's Bot Chat and asks the Bot to repeat it word for word.
Empty output is silent. See desk_say.py for recovery, quiet hours and delivery receipts."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import desk_say  # noqa: E402

if __name__ == "__main__":
    try:
        desk_say.main(["next"])
    except Exception:
        # Hermes records this definitive pre-delivery failure; setup pins failure-deliver to local.
        print("Proactive Hermes say worker failed before delivery.", file=sys.stderr)
        sys.exit(desk_say.SCRIPT_FAILED)
    sys.exit(0)
