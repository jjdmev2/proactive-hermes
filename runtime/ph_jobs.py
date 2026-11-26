"""Which Proactive Hermes jobs a config installs: the one source of truth.

The installer (jobs.py, `hermes proactive setup`) decides from `enabled()` which managed jobs to create or remove,
and the health watchdog (ea_health.py) compares the live jobs' identities (name or recorded id) with `expected()`. Both import this module, so the
watchdog can never expect a job the installer would not create (a feature flag that is not a job, like
resolve_first or mail_push, or a job whose connection is missing, like the Slack sweep without Slack).
It lives in runtime/ because the watchdog runs from the installed scripts directory, where jobs.py is not copied.
"""
from __future__ import annotations

# Every managed job key, in install order. jobs.JOBS holds one template per key (a test keeps them equal).
MANAGED_KEYS: tuple[str, ...] = (
    "morning_brief", "mail_radar", "slack_sweep", "promises", "work_recap_after_daily", "work_recap_evening",
    "evening_brief", "weekly_review", "lookahead", "meeting_prep", "health", "tick",
)

PREFIX = "Proactive \u00b7 "   # every managed job's name starts with it

# The name each managed job is installed under (PREFIX + name). jobs.JOBS takes its names from here, and the
# watchdog matches installed jobs to expected keys by this name (or by the job id the installer recorded).
NAMES: dict[str, str] = {
    "morning_brief": "morning brief", "mail_radar": "mail radar", "slack_sweep": "Slack and calendar sweep",
    "promises": "promise tracker", "work_recap_after_daily": "work recap after the daily",
    "work_recap_evening": "work wrap-up", "evening_brief": "evening brief", "weekly_review": "weekly review",
    "lookahead": "look-ahead", "meeting_prep": "meeting prep", "health": "health", "tick": "tick",
}

# Jobs switched by a feature flag with another name.
FEATURE = {"work_recap_after_daily": "work_recap", "work_recap_evening": "work_recap", "tick": "health"}


def job_name(key: str) -> str:
    """The full installed name of the managed job KEY ("watch:<name>" for a watch)."""
    if key.startswith("watch:"):
        return PREFIX + "watch: " + key.removeprefix("watch:")
    return PREFIX + NAMES[key]


def has_calendar(cfg: dict) -> bool:
    return bool(cfg["mail"].get("gmail_api") or cfg["calendar"].get("enabled"))


def enabled(cfg: dict, key: str) -> bool:
    """Is the managed job KEY installed for this config? Feature flag plus the connection it needs."""
    if key.startswith("watch:"):
        return True
    feats = cfg.get("features", {})
    if key == "slack_sweep":
        return bool(feats.get("slack_sweep") and cfg["slack"].get("enabled"))
    if key.startswith("work_recap"):
        return bool(feats.get("work_recap") and cfg["daily_call"].get("enabled"))
    if key == "lookahead":  # calendar and booking mail: Gmail API, Google Calendar, or any himalaya mailbox
        return bool(feats.get("lookahead") and (cfg["mail"].get("gmail_api") or cfg["calendar"].get("enabled")
                                                 or cfg["mail"].get("himalaya_accounts")))
    if key == "meeting_prep":  # needs a calendar with attendees: Google Calendar through the Gmail API or calendar.enabled
        return bool(feats.get("meeting_prep") and has_calendar(cfg))
    return bool(feats.get(FEATURE.get(key, key), False))


def expected(cfg: dict) -> list[str]:
    """Keys of every job `apply()` keeps installed for CFG: the enabled managed jobs, then one per watch."""
    keys = [key for key in MANAGED_KEYS if enabled(cfg, key)]
    keys += [f"watch:{w['name']}" for w in cfg.get("watches", []) if isinstance(w, dict) and w.get("name")]
    return keys
