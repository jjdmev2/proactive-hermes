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
MOBILE_ACTIONS = frozenset({"reminders", "alarms", "leave_time"})
AUTO_ACTIONS = MOBILE_ACTIONS | {"calendar_blocks", "watch_actions"}

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
    "authority": {
        "auto": [],
        "ask_first": ["send_email", "send_slack", "reply_offers", "register_events", "purchases", "bookings"],
        "never": ["payments", "share_credentials", "delete_data"],
    },
    "mobile": {"enabled": False},
    # Look-ahead (ea_lookahead.py). Empty strings fall back: calendar_account -> calendar.id, mail_account ->
    # owner.work_emails[0]. home_places = regex words for where the owner lives; watch_words = title words of
    # things watched remotely (see ea_lookahead.watched_remotely); senders/keywords = {kind: [extra words]}.
    # Mail is read from the Gmail API plus the configured himalaya accounts (mail.himalaya_accounts; none configured
    # means none read) minus skip_accounts. folders = {account: folder}; unset, the folder is detected per scan
    # ("[Gmail]/All Mail" if the account has it, else INBOX). generic_words = extra words too common in event titles
    # for a "You are registered for X" mail to confirm the event.
    "lookahead": {"calendar_account": "", "mail_account": "", "home_places": [], "watch_words": [],
                  "airport_codes": [], "senders": {}, "keywords": {}, "mail_query": "",
                  "skip_accounts": [], "folders": {}, "generic_words": []},
    # Meeting prep (meeting_prep_gate.py): meetings starting window_min..window_max minutes from now are offered once.
    # own_domains = mail domains that count as internal (empty: the domains of owner.work_emails); daily_regex marks
    # the team daily that is always prepped (matched case-insensitively against the title).
    "meeting_prep": {"window_min": 15, "window_max": 75, "own_domains": [],
                     "daily_regex": "\\b(daily|stand-?up|scrum)\\b", "max_meetings": 3},
    "reminders": {"enabled": False, "per_day": 6},
    "watches": [],
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
        "health": True, "lookahead": False,  # look-ahead is opt-in through setup
        "meeting_prep": True,  # standard; the job only exists when a calendar is connected
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
        "lookahead": "*/30 7-22 * * *",
        "meeting_prep": "*/15 7-21 * * *",
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
    """Private state (mail senders and subjects, ledgers): owner-only, 0700, also when it already existed."""
    env = os.environ.get("EA_STATE_DIR")
    d = Path(env) if env else data_dir() / "state"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if d.stat().st_mode & 0o077:
            d.chmod(0o700)
    except OSError:
        pass  # not ours to change (e.g. a shared EA_STATE_DIR): leave it, never crash a job
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


def allowed(action: str, *, cfg: dict | None = None) -> str:
    """Resolve standing authority. A deny overrides any automatic grant."""
    authority = (CFG if cfg is None else cfg).get("authority", {})
    if not isinstance(authority, dict):
        return "ask"
    never = authority.get("never")
    if isinstance(never, list) and action in never:
        return "never"
    auto = authority.get("auto")
    if action in AUTO_ACTIONS and isinstance(auto, list) and action in auto:
        return "auto"
    return "ask"


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
