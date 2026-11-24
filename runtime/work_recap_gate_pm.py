#!/usr/bin/env python3
"""Evening work wrap-up gate: workday and not a public holiday (no polling)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from work_recap_gate import main  # noqa: E402

sys.exit(main(["--mode", "pm"]))
