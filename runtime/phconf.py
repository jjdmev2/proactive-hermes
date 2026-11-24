"""Proactive Hermes runtime configuration.

Every runtime script reads its settings from here and nothing personal lives in code.
The config file is written by `hermes proactive setup` to
    <HERMES_HOME>/plugin-data/proactive-hermes/config.json
and private state lives next to it in .../state (override with EA_STATE_DIR for tests).
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

PLUGIN = "proactive-hermes"

DEFAULTS: dict = {
    "version": 1,
    "preset": "custom",
    "owner": {"name": "there", "work_emails": [], "personal_emails": []},
    "timezone": "UTC",
    "quiet_hours": {"start": 23, "end": 7},
    "workdays": [0, 1, 2, 3, 4],
    "holidays": [],
    "chat": {"label": "Bot Chat"},
    "p0": {"deliver": ""},
    "calendar": {"enabled": False, "id": "primary", "blocks_per_day": 3},
    "reminders": {"enabled": False, "per_day": 6},
    "mail": {"gmail_api": False, "himalaya_accounts": [], "noise_senders": [], "noise_subjects": []},
    "slack": {"enabled": False, "channels": [], "dm_watch": True},
    "linear": {"enabled": False, "user": ""},
    "daily_call": {
        "enabled": False, "timezone": "UTC", "start": "09:00",
        "poll_start": "09:30", "poll_end": "11:00", "channels": [],
    },
    "decisions": {"enabled": True, "provider": "openai", "model": "gpt-6-luna", "drop_below": 0.10, "body_chars": 600},
    "caps": {"max_items": 3, "max_brief_items": 5, "line_chars": 140, "message_chars": 700},
    "features": {
        "morning_brief": True, "evening_brief": True, "weekly_review": True, "mail_radar": True,
        "slack_sweep": False, "promises": True, "resolve_first": True, "work_recap": False,
        "health": True,
    },
    "schedule": {
        "morning_brief": "0 8 * * 1-5",
        "evening_brief": "30 21 * * 0-4",
        "weekly_review": "0 15 * * 5",
        "mail_radar": "*/15 7-22 * * *",
        "slack_sweep": "0 9-19 * * 1-5",
        "promises": "30 11,15,19 * * 1-5",
        "work_recap_after_daily": "*/5 9-11 * * 1-5",
        "work_recap_evening": "0 21 * * 1-5",
        "health": "7 * * * *",
        "tick": "*/10 7-23 * * *",
    },
}


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # inside a Hermes process
        return Path(get_hermes_home())
    except Exception:
        env = os.environ.get("HERMES_HOME")
        return Path(env).expanduser() if env else Path.home() / ".hermes"


def profile_name(home: Path | None = None) -> str:
    home = home or hermes_home()
    return home.name if home.parent.name == "profiles" else "default"


def data_dir() -> Path:
    return hermes_home() / "plugin-data" / PLUGIN


def config_path() -> Path:
    env = os.environ.get("PROACTIVE_HERMES_CONFIG")
    return Path(env) if env else data_dir() / "config.json"


def state_dir() -> Path:
    env = os.environ.get("EA_STATE_DIR")
    d = Path(env) if env else data_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(path: Path | None = None) -> dict:
    p = path or config_path()
    try:
        data = json.loads(p.read_text())
    except (FileNotFoundError, ValueError):
        data = {}
    return merge(DEFAULTS, data)


CFG = load()


def tz() -> ZoneInfo:
    return ZoneInfo(CFG.get("timezone") or "UTC")


def daily_tz() -> ZoneInfo:
    return ZoneInfo(CFG["daily_call"].get("timezone") or CFG.get("timezone") or "UTC")


def owner() -> str:
    return CFG["owner"].get("name") or "there"


def work_emails() -> set[str]:
    return {e.lower() for e in CFG["owner"].get("work_emails", [])}


def quiet_hours() -> tuple[int, int]:
    q = CFG["quiet_hours"]
    return int(q["start"]), int(q["end"])


def scripts_dir() -> Path:
    """Where `setup apply` installs the runtime (cron gate scripts must live under HERMES_HOME/scripts)."""
    return hermes_home() / "scripts" / PLUGIN


def google_api() -> Path:
    for base in (hermes_home(), Path.home() / ".hermes"):
        p = base / "skills" / "productivity" / "google-workspace" / "scripts" / "google_api.py"
        if p.exists():
            return p
    return Path.home() / ".hermes" / "skills" / "productivity" / "google-workspace" / "scripts" / "google_api.py"
