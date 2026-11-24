"""`hermes proactive ...`: set up, inspect and remove Proactive Hermes.

    hermes proactive setup                      interactive: pick "Juan's setup" or "My own", answer a few questions
    hermes proactive setup --preset juan --answers answers.json   non-interactive (the setup skill uses this)
    hermes proactive setup --dry-run            show what would be created, change nothing
    hermes proactive status                     config summary and the jobs this plugin owns
    hermes proactive remove                     remove every job this plugin created (config and state stay)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "runtime"))
sys.path.insert(0, str(HERE))
import phconf  # noqa: E402

PRESETS = HERE / "presets"

# (path, question, default-from-config, kind). Asked in this order by the CLI and by the setup skill.
QUESTIONS = [
    ("owner.name", "What should I call you?", None, "str"),
    ("timezone", "Your timezone (IANA, e.g. Europe/Berlin)?", "timezone", "tz"),
    ("quiet_hours", "Quiet hours, when only urgent things may reach you (e.g. 23-7)?", "quiet_hours", "hours"),
    ("holidays", "Public holidays to skip, as country codes (e.g. DE,US), or none?", "holidays", "list"),
    ("owner.work_emails", "Your work email addresses (comma separated), so drafts never mix work and personal?", None, "list"),
    ("mail.gmail_api", "Read Gmail and Google Calendar through the google-workspace skill? (y/n)", "mail.gmail_api", "bool"),
    ("mail.himalaya_accounts", "Other mail accounts set up in himalaya to read (names, comma separated), or none?", None, "list"),
    ("calendar.enabled", "May I put private focus blocks on your calendar for key tasks? (y/n)", "calendar.enabled", "bool"),
    ("slack.enabled", "Do you use Slack through Nous Portal connectors? (y/n)", "slack.enabled", "bool"),
    ("slack.channels", "Slack channels to watch for asks to you (e.g. #team,#product), or none?", None, "list"),
    ("linear.enabled", "Do you use Linear through Nous Portal connectors? (y/n)", "linear.enabled", "bool"),
    ("daily_call.enabled", "Does your team have a daily call with notes or a transcript posted in Slack? (y/n)", "daily_call.enabled", "bool"),
    ("daily_call.timezone", "Timezone the daily call is scheduled in?", "daily_call.timezone", "tz"),
    ("daily_call.poll_start", "When should I start looking for the call notes (HH:MM, that timezone)?", "daily_call.poll_start", "hhmm"),
    ("daily_call.poll_end", "Latest time to keep looking (HH:MM)?", "daily_call.poll_end", "hhmm"),
    ("daily_call.channels", "Channel where the call notes land (e.g. #dev-team)?", None, "list"),
]


def _get(d: dict, path: str):
    for part in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(part)
    return d


def _set(d: dict, path: str, value) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        d = d.setdefault(part, {})
    d[parts[-1]] = value


def parse(kind: str, raw: str):
    raw = (raw or "").strip()
    if kind == "bool":
        return raw.lower() in ("y", "yes", "true", "1", "si", "s\u00ed")
    if kind == "list":
        if raw.lower() in ("", "none", "no", "-"):
            return []
        return [x.strip() for x in raw.split(",") if x.strip()]
    if kind == "hours":
        a, b = raw.replace(" ", "").replace(":00", "").split("-")
        return {"start": int(a), "end": int(b)}
    if kind == "tz":
        from zoneinfo import ZoneInfo
        ZoneInfo(raw)
        return raw
    if kind == "hhmm":
        h, m = raw.split(":")
        return f"{int(h):02d}:{int(m):02d}"
    return raw


def show(kind: str, value) -> str:
    if value is None:
        return ""
    if kind == "bool":
        return "y" if value else "n"
    if kind == "list":
        return ",".join(value)
    if kind == "hours":
        return f'{value["start"]}-{value["end"]}'
    return str(value)


def skip(path: str, cfg: dict) -> bool:
    if path.startswith("slack.channels"):
        return not cfg["slack"].get("enabled")
    if path.startswith("daily_call.") and path != "daily_call.enabled":
        return not (cfg["daily_call"].get("enabled") and cfg["slack"].get("enabled"))
    if path == "daily_call.enabled":
        return not cfg["slack"].get("enabled")
    return False


def base_config(preset: str) -> dict:
    cfg = phconf.merge(phconf.DEFAULTS, {})
    if preset == "juan":
        data = json.loads((PRESETS / "juan.json").read_text())
        data.pop("_about", None)
        cfg = phconf.merge(cfg, data)
    else:
        cfg["preset"] = "custom"
    return cfg


def interview(cfg: dict) -> dict:
    for path, q, default_path, kind in QUESTIONS:
        if skip(path, cfg):
            continue
        cur = show(kind, _get(cfg, default_path or path))
        while True:
            raw = input(f"{q}{f' [{cur}]' if cur else ''} ").strip() or cur
            try:
                _set(cfg, path, parse(kind, raw))
                break
            except Exception as exc:  # bad timezone, bad hours
                print(f"  That did not work ({exc}). Try again.")
    cfg["features"]["slack_sweep"] = bool(cfg["slack"].get("enabled"))
    cfg["features"]["work_recap"] = bool(cfg["slack"].get("enabled") and cfg["daily_call"].get("enabled"))
    if cfg["daily_call"].get("enabled"):
        cfg["schedule"]["work_recap_after_daily"] = recap_cron(cfg)
    return cfg


def recap_cron(cfg: dict) -> str:
    a = int(cfg["daily_call"]["poll_start"].split(":")[0])
    b = int(cfg["daily_call"]["poll_end"].split(":")[0])
    days = ",".join(str((d + 1) % 7) for d in cfg.get("workdays", [0, 1, 2, 3, 4]))
    return f"*/5 {a}-{b} * * {days}"


def apply_answers(cfg: dict, answers: dict) -> dict:
    flat = {}
    for path, _q, _d, kind in QUESTIONS:
        if path in answers:
            v = answers[path]
            flat[path] = parse(kind, v) if isinstance(v, str) and kind != "str" else v
    for path, v in flat.items():
        _set(cfg, path, v)
    asked = {path for path, *_ in QUESTIONS}
    nested = {k: v for k, v in answers.items() if "." not in k and k not in asked}
    cfg = phconf.merge(cfg, nested)
    cfg["features"]["slack_sweep"] = bool(cfg["slack"].get("enabled"))
    cfg["features"]["work_recap"] = bool(cfg["slack"].get("enabled") and cfg["daily_call"].get("enabled"))
    if cfg["daily_call"].get("enabled"):
        cfg["schedule"]["work_recap_after_daily"] = recap_cron(cfg)
    return cfg


def write_config(cfg: dict) -> Path:
    p = phconf.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=1, ensure_ascii=False))
    p.chmod(0o600)
    return p


def setup_parser(parser) -> None:
    sub = parser.add_subparsers(dest="proactive_command", metavar="<command>")
    s = sub.add_parser("setup", help="Set up or update Proactive Hermes")
    s.add_argument("--preset", choices=["juan", "custom"], help="juan = Juan's setup, custom = your own")
    s.add_argument("--answers", help="JSON file of answers (keys like owner.name, timezone, slack.channels)")
    s.add_argument("--dry-run", action="store_true", help="show the jobs, change nothing")
    sub.add_parser("status", help="Show config and the jobs this plugin owns")
    sub.add_parser("remove", help="Remove every job this plugin created")
    sub.add_parser("questions", help="Print the setup questions as JSON (for the setup skill)")


def handle(args) -> int:
    import jobs

    cmd = getattr(args, "proactive_command", None)
    if cmd == "questions":
        print(json.dumps([{"key": p, "question": q, "kind": k} for p, q, _d, k in QUESTIONS], indent=1))
        return 0
    if cmd == "status":
        cfg = phconf.load()
        print(f"Config: {phconf.config_path()} ({'found' if phconf.config_path().exists() else 'not set up yet'})")
        print(f"Preset: {cfg.get('preset')}  Owner: {cfg['owner'].get('name')}  Timezone: {cfg.get('timezone')}")
        for row in jobs.apply(cfg, dry_run=True):
            print(f"  {row['job']}: {row['action'] if row['action'] != 'update' else 'installed'}  {row['schedule']}")
        return 0
    if cmd == "remove":
        gone = jobs.remove_all()
        print(f"Removed {len(gone)} job(s). Config and private state kept in {phconf.data_dir()}.")
        return 0
    if cmd != "setup":
        print(__doc__)
        return 0
    preset = args.preset
    if not preset:
        print("Proactive Hermes setup.\n  1. Juan's setup (proven defaults: briefs, promise tracker, drafts first, work recap)\n  2. My own (start from scratch)")
        preset = "juan" if input("Pick 1 or 2 [1] ").strip() in ("", "1") else "custom"
    cfg = base_config(preset)
    if args.answers:
        cfg = apply_answers(cfg, json.loads(Path(args.answers).read_text()))
    elif sys.stdin.isatty():
        cfg = interview(cfg)
    else:
        print("No terminal and no --answers file: run the setup skill in chat, or pass --answers.")
        return 2
    if args.dry_run:
        print(json.dumps(cfg, indent=1))
        for row in jobs.apply(cfg, dry_run=True):
            print(f"  would {row['action']}: {row['job']}  {row['schedule']}  ({row['timezone']})")
        return 0
    path = write_config(cfg)
    phconf.CFG = cfg
    rows = jobs.apply(cfg)
    print(f"Saved {path}")
    for row in rows:
        if row["action"] != "skip":
            print(f"  {row['action']}: {row['job']}  {row['schedule']}  ({row['timezone']})")
    print("Done. Messages arrive in your chat as normal replies; nothing is ever sent on your behalf.")
    return 0
