"""`hermes proactive ...`: set up, inspect and remove Proactive Hermes.

    hermes proactive setup                      interactive: pick "Juan's setup" or "My own", answer a few questions
    hermes proactive setup --preset juan --answers answers.json   non-interactive (the setup skill uses this)
    hermes proactive setup --dry-run            show what would be created, change nothing
    hermes proactive status                     config summary and the jobs this plugin owns
    hermes proactive remove                     remove every job this plugin created (config and state stay)
    hermes proactive watch {add,list,remove}     manage scheduled watches

When plugin CLI registration is disabled, run the same subcommands with `python3 cli.py ...`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

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
    ("decisions.enabled", "Use OpenAI's fast decision model to filter noise and spot promises? Needs OPENAI_API_KEY, about 3 cents per 1,000 emails. (y/n)", "decisions.enabled", "bool"),
    ("mail.gmail_api", "Read Gmail and Google Calendar through the google-workspace skill? (y/n)", "mail.gmail_api", "bool"),
    ("mail.himalaya_accounts", "Other mail accounts set up in himalaya to read (names, comma separated), or none?", None, "list"),
    ("calendar.enabled", "May I put private focus blocks on your calendar for key tasks? (y/n)", "calendar.enabled", "bool"),
    ("authority.auto", "Which may I do without asking? calendar_blocks, reminders, alarms (urgent only), leave_time (block + 'leave now' reminder for trips). Comma separated, or none", "authority.auto", "list"),
    ("mobile.enabled", "Do you use the Hermes mobile app with reminders, alarms and location shared? (y/n)", "mobile.enabled", "bool"),
    ("slack.enabled", "Do you use Slack through Nous Portal connectors? (y/n)", "slack.enabled", "bool"),
    ("slack.channels", "Slack channels to watch for asks to you (e.g. #team,#product), or none?", None, "list"),
    ("linear.enabled", "Do you use Linear through Nous Portal connectors? (y/n)", "linear.enabled", "bool"),
    ("daily_call.enabled", "Does your team have a daily call with notes or a transcript posted in Slack? (y/n)", "daily_call.enabled", "bool"),
    ("daily_call.timezone", "Timezone the daily call is scheduled in?", "daily_call.timezone", "tz"),
    ("daily_call.poll_start", "When should I start looking for the call notes (HH:MM, that timezone)?", "daily_call.poll_start", "hhmm"),
    ("daily_call.poll_end", "Latest time to keep looking (HH:MM)?", "daily_call.poll_end", "hhmm"),
    ("daily_call.channels", "Channel where the call notes land (e.g. #dev-team)?", None, "list"),
]


def detect() -> dict:
    """What we can know without asking: owner name, timezone, mail accounts, connected services."""
    import os
    import pwd
    import shutil
    import subprocess
    found: dict = {}
    home = phconf.hermes_home()
    for f in (home / "memories" / "USER.md", home / "USER.md"):
        try:
            for line in f.read_text().splitlines():
                low = line.lower()
                for tag in ("name:", "user's name is", "name is"):
                    if tag in low:
                        name = line[low.index(tag) + len(tag):].strip(" .*").split(",")[0].split(" (")[0]
                        if 0 < len(name) < 40:
                            found.setdefault("owner.name", name)
        except OSError:
            pass
    if "owner.name" not in found:
        try:
            gecos = pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0].strip()
            if gecos:
                found["owner.name"] = gecos.split()[0]
        except Exception:
            pass
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            found["timezone"] = link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    if os.environ.get("TZ") and "/" in os.environ["TZ"]:
        found["timezone"] = os.environ["TZ"]
    if shutil.which("himalaya"):
        try:
            out = subprocess.run(["himalaya", "account", "list", "-o", "json"], capture_output=True, text=True, timeout=15)
            names = [a.get("name") for a in json.loads(out.stdout or "[]") if a.get("name")]
            if names:
                found["mail.himalaya_accounts"] = names
        except Exception:
            pass
    if phconf.google_api().exists():
        found["mail.gmail_api"] = True
    sys.path.insert(0, str(HERE / "runtime"))
    try:
        import ea_decide
        found["decisions.enabled"] = bool(ea_decide.api_key())
    except Exception:
        pass
    return found


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


def parse_answer(path: str, kind: str, value):
    if isinstance(value, str) and kind != "str":
        value = parse(kind, value)
    if path == "authority.auto":
        if not isinstance(value, list):
            raise ValueError("authority.auto expects a comma-separated list or a JSON array")
        invalid = [repr(action) for action in value if not isinstance(action, str) or action not in phconf.AUTO_ACTIONS]
        if invalid:
            valid = ", ".join(sorted(phconf.AUTO_ACTIONS))
            raise ValueError(f"authority.auto: invalid action(s) {', '.join(invalid)}; choose from {valid}")
        value = list(dict.fromkeys(value))
    if path == "mobile.enabled" and not isinstance(value, bool):
        raise ValueError("mobile.enabled expects yes or no")
    return value


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
    known = detect()
    if known:
        print("Here is what I already know (press Enter to keep each one):")
        for k, v in known.items():
            print(f"  {k}: {', '.join(v) if isinstance(v, list) else v}")
            _set(cfg, k, v)
    for path, q, default_path, kind in QUESTIONS:
        if skip(path, cfg):
            continue
        cur = show(kind, _get(cfg, default_path or path))
        while True:
            raw = input(f"{q}{f' [{cur}]' if cur else ''} ").strip() or cur
            try:
                _set(cfg, path, parse_answer(path, kind, raw))
                break
            except Exception as exc:  # bad timezone, hours or authority
                print(f"  That did not work ({exc}). Try again.")
    return finish_setup(cfg)


def recap_cron(cfg: dict) -> str:
    a = int(cfg["daily_call"]["poll_start"].split(":")[0])
    b = int(cfg["daily_call"]["poll_end"].split(":")[0])
    days = ",".join(str((d + 1) % 7) for d in cfg.get("workdays", [0, 1, 2, 3, 4]))
    return f"*/5 {a}-{b} * * {days}"


def apply_answers(cfg: dict, answers: dict) -> dict:
    flat = {}
    for path, _q, _d, kind in QUESTIONS:
        if path in answers:
            flat[path] = parse_answer(path, kind, answers[path])
    for path, v in flat.items():
        _set(cfg, path, v)
    asked = {path for path, *_ in QUESTIONS}
    nested = {k: v for k, v in answers.items() if "." not in k and k not in asked}
    return finish_setup(phconf.merge(cfg, nested))


def finish_setup(cfg: dict) -> dict:
    auto = parse_answer("authority.auto", "list", _get(cfg, "authority.auto"))
    mobile = parse_answer("mobile.enabled", "bool", _get(cfg, "mobile.enabled"))
    cfg["authority"]["auto"] = auto if mobile else [action for action in auto if action not in phconf.MOBILE_ACTIONS]
    cfg["mobile"]["enabled"] = mobile
    cfg["reminders"]["enabled"] = mobile
    cfg["features"]["slack_sweep"] = bool(cfg["slack"].get("enabled"))
    cfg["features"]["work_recap"] = bool(cfg["slack"].get("enabled") and cfg["daily_call"].get("enabled"))
    if cfg["daily_call"].get("enabled"):
        cfg["schedule"]["work_recap_after_daily"] = recap_cron(cfg)
    return cfg


def decisions_note(cfg: dict) -> str:
    d = cfg.get("decisions", {})
    if not d.get("enabled"):
        return ""
    import ea_decide
    if ea_decide.api_key():
        return "Fast decisions: OpenAI key found, noise filter and promise checks are on."
    msg = (f"No OPENAI_API_KEY found. Add one to {phconf.hermes_home() / '.env'} (a few dollars of credit at "
           "platform.openai.com lasts months). Until then the desk works without the fast filter.")
    return ("Juan's setup is tuned for OpenAI's Decisions API. " + msg) if d.get("required") else msg


def write_config(cfg: dict) -> Path:
    p = phconf.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(cfg, indent=1, ensure_ascii=False)
    if not p.is_file() or p.read_text() != text:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=p.parent, prefix=".config-", delete=False) as f:
            tmp = Path(f.name)
            try:
                f.write(text)
                f.close()
                tmp.replace(p)
            finally:
                tmp.unlink(missing_ok=True)
    if p.stat().st_mode & 0o777 != 0o600:
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
    watch = sub.add_parser("watch", help="Manage user-defined scheduled watches")
    watch_sub = watch.add_subparsers(dest="watch_command", required=True)
    add = watch_sub.add_parser("add", help="Add a scheduled watch")
    add.add_argument("--name", required=True)
    add.add_argument("--schedule", required=True, help="Five-field cron expression")
    add.add_argument("--check", required=True)
    add.add_argument("--when", required=True)
    add.add_argument("--then", required=True, choices=("tell", "reminder", "alarm", "draft", "act"))
    add.add_argument("--act", help="Exact action, required with --then act")
    add.add_argument("--urgent", action="store_true")
    watch_sub.add_parser("list", help="List watches and their conditions")
    watch_sub.add_parser("remove", help="Remove a watch and its managed job").add_argument("--name", required=True)


def handle_watch(args, jobs) -> int:
    cfg = phconf.load()
    watches = cfg["watches"]
    if args.watch_command == "list":
        print(json.dumps(watches, ensure_ascii=False, indent=2))
        return 0
    name = args.name
    try:
        if not name.strip() or not name.isprintable():
            raise ValueError("--name must be a nonblank, single-line name")
        existing = next((watch for watch in watches if watch["name"] == name), None)
        if args.watch_command == "add":
            if not args.check.strip() or not args.when.strip():
                raise ValueError("--check and --when must not be blank")
            if args.then == "act" and not (args.act or "").strip():
                raise ValueError("--then act requires an exact action in --act")
            if args.then != "act" and args.act is not None:
                raise ValueError("--act is only valid with --then act")
            jobs.validate_watch_schedule(args.schedule)
            watch = {"name": name, "schedule": args.schedule, "check": args.check,
                     "when": args.when, "then": args.then, "urgent": args.urgent}
            if args.act is not None:
                watch["act"] = args.act
            if existing is not None and existing != watch:
                raise ValueError(f"watch {name!r} already exists; remove it before changing it")
            if existing is None:
                if any(jobs.watch_id(w["name"]) == jobs.watch_id(name) for w in watches):
                    raise ValueError("another watch name has the same slug; choose a distinct name")
                watches.append(watch)
        elif existing is not None:
            watches.remove(existing)
    except ValueError as exc:
        print(f"Watch: {exc}", file=sys.stderr)
        return 2
    write_config(cfg)
    try:
        jobs.apply(cfg)
    except (OSError, RuntimeError) as exc:
        print(f"Watch config saved, but cron reconciliation failed: {exc}", file=sys.stderr)
        return 1
    print(f"Watch {name!r}: {'installed' if args.watch_command == 'add' else 'removed'}.")
    return 0


def handle(args) -> int:
    if __package__:
        from . import jobs
    else:
        import jobs

    cmd = getattr(args, "proactive_command", None)
    if cmd == "watch":
        return handle_watch(args, jobs)
    if cmd == "questions":
        print(json.dumps({"known": detect(),
                          "questions": [{"key": p, "question": q, "kind": k} for p, q, _d, k in QUESTIONS]}, indent=1))
        return 0
    if cmd == "status":
        cfg = phconf.load()
        print(f"Config: {phconf.config_path()} ({'found' if phconf.config_path().exists() else 'not set up yet'})")
        print(f"Preset: {cfg.get('preset')}  Owner: {cfg['owner'].get('name')}  Timezone: {cfg.get('timezone')}")
        for row in jobs.apply(cfg, dry_run=True):
            action = "installed" if row["action"] == "keep" else row["action"]
            print(f"  {row['job']}: {action}  {row['schedule']}")
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
    # Watches are user-defined, not preset defaults. Keep them across setup updates.
    cfg["watches"] = phconf.load()["watches"]
    if args.answers:
        try:
            cfg = apply_answers(cfg, json.loads(Path(args.answers).read_text()))
        except (OSError, ValueError, KeyError) as exc:
            print(f"Setup answers: {exc}", file=sys.stderr)
            return 2
    elif sys.stdin.isatty():
        cfg = interview(cfg)
    else:
        print("No terminal and no --answers file: run the setup skill in chat, or pass --answers.")
        return 2
    if args.dry_run:
        print(json.dumps(cfg, indent=1))
        for row in jobs.apply(cfg, dry_run=True):
            if row["action"] not in ("keep", "skip"):
                print(f"  would {row['action']}: {row['job']}  {row['schedule']}  ({row['timezone']})")
        return 0
    key_note = decisions_note(cfg)
    if key_note:
        print(key_note)
    path = write_config(cfg)
    phconf.CFG = cfg
    rows = jobs.apply(cfg)
    print(f"Config: {path}")
    actions = [row["action"] for row in rows]
    for row in rows:
        if row["action"] not in ("keep", "skip"):
            print(f"  {row['action']}: {row['job']}  {row['schedule']}  ({row['timezone']})")
    print(f"Jobs: {actions.count('create')} created, {actions.count('update')} updated, {actions.count('remove')} removed.")
    print("Done. Desk messages arrive in your chat as normal replies.")
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Set up, inspect or remove Proactive Hermes.")
    setup_parser(parser)
    sys.exit(handle(parser.parse_args()))
