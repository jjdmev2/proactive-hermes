"""Proactive Hermes runtime configuration.

Every runtime script reads its settings from here and nothing personal lives in code.
The config file is written by `hermes proactive setup` to
    <HERMES_HOME>/plugin-data/proactive-hermes/config.json
and private state lives next to it in .../state. Both paths always come from the active profile's Hermes home:
no environment variable picks another authority file or state dir (tests patch config_path / state_dir, or give
the process an isolated HERMES_HOME).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

PLUGIN = "proactive-hermes"
MOBILE_ACTIONS = frozenset({"reminders", "alarms", "leave_time"})
AUTO_ACTIONS = MOBILE_ACTIONS | {"calendar_blocks", "watch_actions"}
# Every action name the authority lists may hold (auto takes only AUTO_ACTIONS). Setup rejects anything else.
ACTIONS = AUTO_ACTIONS | {"send_email", "send_slack", "reply_offers", "register_events", "purchases", "bookings",
                          "payments", "share_credentials", "delete_data"}

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
        # IMAP IDLE pokes the mail radar on new mail (runtime/mail_idle.py, gateway only), for the mailboxes the
        # owner gave Hermes in Settings -> Plugins -> Proactive Hermes (mail_N_address + app password). Every other
        # mailbox stays on the radar schedule; polling always stays on as the safety net.
        "mail_push": True,
        "calendar_alerts": True,  # standard; time-critical reminders and alarms also become calendar alerts
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
    """The authority and settings file: always the active profile's own plugin-data config.json. The calling
    process cannot point it elsewhere (no environment override); tests patch this function."""
    return data_dir() / "config.json"


def state_dir() -> Path:
    """Private state (mail senders and subjects, ledgers, the outbox): <plugin-data>/state of the active profile,
    owner-only, 0700, also when it already existed. No environment override; tests patch this function."""
    d = data_dir() / "state"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if d.stat().st_mode & 0o077:
            d.chmod(0o700)
    except OSError:
        pass  # not ours to change: leave it, never crash a job
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


AUTHORITY_LISTS = ("auto", "ask_first", "never")


def authority_problem(cfg: dict | None = None) -> str:
    """Why the configured authority cannot be trusted, or "" when it is well formed: an object whose auto,
    ask_first and never are lists of strings. A malformed policy (e.g. "never": "calendar_blocks") is never
    guessed at: allowed() then denies every action, `hermes proactive doctor` reports it and setup refuses to
    write it."""
    authority = (CFG if cfg is None else cfg).get("authority", {})
    if not isinstance(authority, dict):
        return "authority must be an object with auto, ask_first and never lists"
    for name in AUTHORITY_LISTS:
        value = authority.get(name, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return f"authority.{name} must be a list of action names"
    return ""


def authority_errors(cfg: dict | None = None) -> list[str]:
    """What setup refuses to save beyond a malformed shape: an unknown action name, an action auto cannot hold, an
    action listed in more than one level. At run time an overlap still resolves by precedence (allowed())."""
    c = CFG if cfg is None else cfg
    if authority_problem(c):
        return []  # the shape is wrong: authority_problem says why
    authority = c.get("authority", {})
    errors = []
    unknown = sorted(set(authority) - set(AUTHORITY_LISTS))
    if unknown:
        errors.append(f"unknown approval level(s) {', '.join(map(repr, unknown))}; use auto, ask_first or never")
    for name in AUTHORITY_LISTS:
        bad = [a for a in authority.get(name, []) if a not in ACTIONS]
        if bad:
            errors.append(f"authority.{name}: unknown action(s) {', '.join(map(repr, bad))}; "
                          f"choose from {', '.join(sorted(ACTIONS))}")
    bad = [a for a in authority.get("auto", []) if a in ACTIONS and a not in AUTO_ACTIONS]
    if bad:
        errors.append(f"authority.auto: {', '.join(map(repr, bad))} can never be automatic; "
                      f"auto takes only {', '.join(sorted(AUTO_ACTIONS))}")
    seen: dict[str, list[str]] = {}
    for name in AUTHORITY_LISTS:
        for a in dict.fromkeys(authority.get(name, [])):
            seen.setdefault(a, []).append(name)
    for a, levels in seen.items():
        if len(levels) > 1:
            errors.append(f"{a!r} is in both {' and '.join(levels)}: each action belongs to exactly one level")
    return errors


def allowed(action: str, *, cfg: dict | None = None) -> str:
    """Resolve standing authority: "auto", "ask" or "never". Precedence never > ask_first > auto: an action listed
    in never is never, else one listed in ask_first is ask, else one listed in auto (and allowed to be automatic)
    is auto, else ask. A malformed authority (see authority_problem) is "never" for every action: it fails closed."""
    c = CFG if cfg is None else cfg
    if authority_problem(c):
        return "never"
    authority = c.get("authority", {})
    if action in authority.get("never", []):
        return "never"
    if action in authority.get("ask_first", []):
        return "ask"
    if action in AUTO_ACTIONS and action in authority.get("auto", []):
        return "auto"
    return "ask"


def authorize(action: str, *, cfg: dict | None = None) -> tuple[bool, str]:
    """The execution-boundary check every write the plugin makes on its own goes through (phone reminders and
    alarms, calendar blocks and alerts): (True, "auto") only when the action is automatic, else (False, "ask" or
    "never") and the caller must not touch the service. Ask first is never done by a script: after the owner's yes
    in chat, the chat agent uses Hermes's own tools (mobile reminders and alarms, calendar), with Hermes's own
    approvals."""
    level = allowed(action, cfg=cfg)
    return level == "auto", level


def authorize_cleanup(action: str, *, delete: bool = False, cfg: dict | None = None) -> tuple[bool, str, str]:
    """Cleanup of the plugin's own writes (ticking off its own phone reminder, deleting its own calendar block or
    alert) is a write too: it needs ACTION automatic, exactly like creating it. A delete also honours delete_data:
    listed in never or ask_first, it refuses. (ok, refused action, its level); under ask or never the caller
    refuses (exit 5) and leaves it to the chat flow, which uses Hermes's own tools after the owner's yes."""
    c = CFG if cfg is None else cfg
    level = allowed(action, cfg=c)
    if level != "auto":
        return False, action, level
    if delete:
        dd = allowed("delete_data", cfg=c)
        authority = c.get("authority", {})
        if dd == "never" or "delete_data" in authority.get("ask_first", []):
            return False, "delete_data", dd
    return True, action, "auto"


def calendar_alerts_on(*, cfg: dict | None = None) -> bool:
    """Time-critical reminders and alarms also become calendar alerts the phone fires by itself.
    On by default whenever a Google calendar is connected AND calendar_blocks is automatic authority
    (an alert is a calendar write made without asking); off with features.calendar_alerts, or when
    calendar_blocks is 'ask' or 'never'."""
    c = CFG if cfg is None else cfg
    return bool(c.get("calendar", {}).get("enabled") and c.get("features", {}).get("calendar_alerts", True)
                and allowed("calendar_blocks", cfg=c) == "auto")


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


RUNTIME_STAMP = ".runtime-stamp.json"   # written by jobs.install_runtime next to the installed scripts
CONTRACT = Path("skills") / "proactive-desk" / "references" / "desk-contract.md"
CONTRACT_NAME = CONTRACT.name           # installed (rendered) as scripts_dir() / desk-contract.md; the jobs read it
GENERATIONS = ".proactive-hermes-gen"   # scripts/<this>/<generation>: one complete runtime each; scripts_dir() points
STALE = "runtime out of date: run hermes proactive setup"
NO_CONTRACT = "desk contract missing: run hermes proactive setup"


def file_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def runtime_digest(plugin_root: Path) -> str:
    """Fingerprint of what install_runtime copies from a plugin checkout: runtime/*.py and the desk contract."""
    h = hashlib.sha256()
    for f in sorted(Path(plugin_root, "runtime").glob("*.py")) + [Path(plugin_root, CONTRACT)]:
        h.update(f.name.encode() + b"\0" + (f.read_bytes() if f.is_file() else b"") + b"\0")
    return h.hexdigest()


def runtime_stamp() -> dict:
    try:
        data = json.loads((scripts_dir() / RUNTIME_STAMP).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def runtime_drift(dest: Path | None = None, stamp: dict | None = None) -> str:
    """Empty when every installed file the stamp lists (runtime/*.py and the rendered desk-contract.md) is there with
    the content setup installed, else why not. A stamp without a file list (an older install) is drift too."""
    dest = dest or scripts_dir()
    stamp = runtime_stamp() if stamp is None else stamp
    if not (dest / CONTRACT_NAME).is_file():
        return NO_CONTRACT
    files = stamp.get("files")
    if not isinstance(files, dict) or CONTRACT_NAME not in files:
        return STALE
    for name, digest in sorted(files.items()):
        target = dest / name
        try:
            if not target.is_file():
                return f"installed {name} missing: run hermes proactive setup"
            if file_digest(target.read_bytes()) != digest:
                return f"installed {name} changed since setup: run hermes proactive setup"
        except OSError:
            return f"installed {name} unreadable: run hermes proactive setup"
    return ""


def runtime_status(plugin_root: Path | None = None) -> tuple[str, str]:
    """Is the installed runtime (scripts_dir) the one this plugin ships, unchanged? (state, note), state one of:
    "missing" (setup never installed it), "ok", "stale" (an older copy, setup was not re-run after an upgrade, or
    an installed file, the desk contract included, is missing or changed since setup), "unknown" (installed files
    match their stamp but there is no plugin checkout to compare with). plugin_root defaults to the stamp's source."""
    dest = scripts_dir()
    if not (dest / "phconf.py").is_file():
        return "missing", "runtime not installed yet: run hermes proactive setup"
    stamp = runtime_stamp()
    source = plugin_root or stamp.get("source")
    root = Path(source) if source else None
    if root is not None and (root / "runtime").is_dir():
        if stamp.get("digest") != runtime_digest(root):
            return "stale", STALE
        for f in (root / "runtime").glob("*.py"):
            target = dest / f.name
            if not target.is_file() or target.read_bytes() != f.read_bytes():
                return "stale", STALE
    else:
        root = None
    drift = runtime_drift(dest, stamp)
    if drift:
        return "stale", drift
    if root is None:
        return "unknown", "plugin source not found, cannot compare the installed runtime"
    return "ok", "installed runtime matches the plugin"


def agent_root() -> Path:
    """The Hermes install directory (so a runtime script started by a bare python3 can import Hermes), derived from
    the active Hermes home only: no environment variable can point the secret lookup at other code."""
    home = hermes_home()
    base = home.parent.parent if home.parent.name == "profiles" else home
    return base / "hermes-agent"


def _get_secret():
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        root = agent_root()
        if not root.is_dir():
            return None
        if str(root) not in sys.path:
            sys.path.append(str(root))
        try:
            from agent.secret_scope import get_secret
        except ImportError:
            return None
    return get_secret


def secret(name: str) -> str:
    """A credential the plugin declares (plugin.yaml), read only through Hermes: agent.secret_scope.get_secret,
    which honours the active profile. Never from a file (catalog rule 11). "" when it is unset, when Hermes cannot
    be imported, or when Hermes refuses the read (a multiplexed gateway thread with no profile scope): callers
    fail open."""
    get_secret = _get_secret()
    if get_secret is None:
        return ""
    try:
        return str(get_secret(name) or "").strip()
    except Exception:  # UnscopedSecretError and friends: never crash a job over an optional key
        return ""


def google_api() -> Path:
    for base in (hermes_home(), Path.home() / ".hermes"):
        p = base / "skills" / "productivity" / "google-workspace" / "scripts" / "google_api.py"
        if p.exists():
            return p
    return Path.home() / ".hermes" / "skills" / "productivity" / "google-workspace" / "scripts" / "google_api.py"
