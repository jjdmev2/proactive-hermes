#!/usr/bin/env python3
"""Cron gate for the look-ahead job: runs `ea_lookahead.py scan` (no LLM unless a checkpoint is due)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ea_lookahead  # noqa: E402

if __name__ == "__main__":
    sys.exit(ea_lookahead.main(["scan"]))
