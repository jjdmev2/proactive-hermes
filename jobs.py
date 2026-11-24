"""Proactive Hermes job templates and the idempotent installer behind `hermes proactive setup`.

Each job is owned by the plugin: its id is recorded in <plugin-data>/jobs.json and its name starts
with "Proactive ·". `apply()` installs the runtime scripts under HERMES_HOME/scripts/proactive-hermes
(cron gate scripts must live under HERMES_HOME/scripts), renders the prompts from config.json, and
creates, updates or removes jobs so the set always matches the enabled features. Running it twice
changes nothing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "runtime"
sys.path.insert(0, str(RUNTIME))
import phconf  # noqa: E402

PREFIX = "Proactive \u00b7 "

HEAD = ("{{NAME}}. FIRST read the desk contract in full: `cat {{SCRIPTS}}/desk-contract.md` and follow it exactly. "
        "Owner: {{OWNER}}. All times {{TIMEZONE}}. Read-only everywhere: never send, reply, post, react, book or pay.\n\n")
TAIL = ("\n\nWrite the items to {{STATE}}/desk_items.json (an empty list [] is fine) and run:\n"
        "   python3 {{SCRIPTS}}/ea_desk.py compose --job {{JOB}} {{COMPOSE_FLAGS}}--file {{STATE}}/desk_items.json\n"
        "If compose prints [RESOLVE-FIRST] <keys>, resolve those keys per the contract and run the same command again.\n"
        "Final answer = [SILENT] (compose posts the message itself).")

JOBS: list[dict] = [
    {"key": "morning_brief", "name": "morning brief", "job": "morning", "flags": "--brief --take-held ",
     "body": "Morning brief. Read: today's and tomorrow's calendar ({{CALENDAR_TOOLS}}), mail since yesterday evening "
             "({{MAIL_TOOLS}}), open promises (`python3 {{SCRIPTS}}/ea_promises.py list --open`) and anything held overnight. "
             "Pick at most 5 items {{OWNER}} must do or decide today: conflicts, deadlines, people waiting, promises due. "
             "Every open promise that is overdue or due by tomorrow becomes an action item (key = the promise key, with act/who/to), "
             "resolved per the contract. Header for compose: --header \"Morning {{OWNER}}, <N> things:\" (add it to the compose command)."},
    {"key": "mail_radar", "name": "mail radar", "job": "radar", "flags": "--take-pending ", "script": "ea_radar_gate.py",
     "body": "Mail radar. The gate context lists new mail since the last run. Keep only mail {{OWNER}} must answer, pay or "
             "decide on, or a real deadline. Check Sent first: if they already answered, drop it. A mail that needs an answer "
             "gets act/who/to and the contract's resolve step (max 1 per run). Newsletters, receipts, notifications: leave out."},
    {"key": "slack_sweep", "name": "Slack and calendar sweep", "job": "sweep", "flags": "",
     "body": "Slack and calendar sweep. Read Slack since the last sweep: DMs to {{OWNER}} and these channels: {{SLACK_CHANNELS}}. "
             "Keep only asks waiting on {{OWNER}} (a question, a review, an approval) where they have not answered yet. "
             "A DM from a teammate since the last sweep is always a candidate. Check today's calendar for new conflicts. "
             "An ask that needs a written answer gets act reply or decide, who, to = the Slack channel id, and the resolve step (max 1)."},
    {"key": "promises", "name": "promise tracker", "job": "promises", "flags": "",
     "body": "Promise tracker. Goal: know what {{OWNER}} said they would do, to whom, by when, and whether they did it.\n"
             "1. Read what {{OWNER}} wrote in the last 36h: Sent mail ({{MAIL_TOOLS}}), their Slack messages, and what they told you in chat. "
             "A promise = a specific action for a specific person (send, reply, intro, call, review, pay, \"I'll get back to you\"). "
             "Record each new one: `python3 {{SCRIPTS}}/ea_promises.py add --to \"<person>\" --what \"<action>\" [--due ISO] --made ISO "
             "--source \"<where>\" --quote \"<their words>\" --channel mail|slack|chat`.\n"
             "2. Close open promises only on proof: `ea_promises.py close --key K --evidence \"<ref>\"`. Never on a guess.\n"
             "3. Items only for promises due within 24h or overdue (`ea_promises.py list --open --due-within 24`): kind action, "
             "key = the promise key, act/who/to set, resolved per the contract first, urgent false."},
    {"key": "work_recap_after_daily", "name": "work recap after the daily", "job": "work-am", "flags": "--brief ",
     "script": "work_recap_gate.py", "tz": "daily",
     "body": "Work recap after the daily call. The gate wakes you every 5 minutes inside the poll window; its context has "
             "attempt and final_attempt. Step 1 is ONLY the search for today's call notes or transcript in Slack "
             "(channels: {{DAILY_CHANNELS}}; Slack huddle notes are a canvas file, read it with the Slack download file tool). "
             "Not there yet and final_attempt false: stop, answer [SILENT]. Found: compare what was discussed with Linear "
             "(issues that changed state) and Slack messages. Lines: \"Done: <thing> (<who>)\", \"Not done: <thing>, still <state>\", "
             "\"Mismatch: <Linear says X, Slack says Y>\", last 1 or 2: \"Focus today: ...\". Never claim done without evidence. "
             "Then run `python3 {{SCRIPTS}}/work_recap_gate.py done --mode am`. If final_attempt and still nothing: recap from "
             "Linear and Slack only, first line \"No call notes yet.\", then the same done command. Header: --header \"Work today:\"."},
    {"key": "work_recap_evening", "name": "work wrap-up", "job": "work-pm", "flags": "--brief ", "script": "work_recap_gate_pm.py",
     "body": "Evening work wrap-up. Compare today's call notes (Slack: {{DAILY_CHANNELS}}) with Linear and Slack activity today. "
             "Lines: \"Shipped today: ...\", \"Slipped: ...\", \"Mismatch: ...\", last: \"Tomorrow: ...\". Evidence only. "
             "Nothing moved: write []. Header: --header \"Work wrap-up:\"."},
    {"key": "evening_brief", "name": "evening brief", "job": "evening", "flags": "--brief ",
     "body": "Evening close-out. Collect what is still open today: decisions, promises overdue or due tomorrow (resolved per the "
             "contract), replies people are waiting for. Ask once, numbered, so {{OWNER}} can answer \"1 yes, 2 tomorrow\". "
             "Header: --header \"Before you log off, <N> open:\"."},
    {"key": "weekly_review", "name": "weekly review", "job": "weekly", "flags": "--brief ",
     "body": "Weekly review. Look back 7 days: promises kept and missed (`ea_promises.py list`), decisions still open, "
             "drafts shown vs sent (`python3 {{SCRIPTS}}/ea_resolve.py list`). At most 5 items, the ones that need {{OWNER}} next week. "
             "Header: --header \"Your week, <N> things:\"."},
    {"key": "health", "name": "health", "script": "ea_health.py", "no_agent": True,
     "body": "Proactive Hermes health watchdog (no-agent script)."},
    {"key": "tick", "name": "tick", "script": "desk_tick.py", "no_agent": True,
     "body": "Proactive Hermes outbox and reminder retry (no-agent script)."},
]

FEATURE = {"work_recap_after_daily": "work_recap", "work_recap_evening": "work_recap", "tick": "health"}


def enabled(cfg: dict, job: dict) -> bool:
    feats = cfg.get("features", {})
    if job["key"] == "slack_sweep":
        return bool(feats.get("slack_sweep") and cfg["slack"].get("enabled"))
    if job["key"].startswith("work_recap"):
        return bool(feats.get("work_recap") and cfg["daily_call"].get("enabled"))
    return bool(feats.get(FEATURE.get(job["key"], job["key"]), False))


def tools(cfg: dict) -> dict:
    g = str(phconf.google_api())
    mail = []
    if cfg["mail"].get("gmail_api"):
        mail.append(f'`python3 {g} gmail search "<query>"` and `gmail get <id>`')
    for acc in cfg["mail"].get("himalaya_accounts", []):
        name = acc.get("account") if isinstance(acc, dict) else acc
        mail.append(f"himalaya account `{name}`")
    cal = (f'`python3 {g} calendar list --start ISO --end ISO --max 40` (calendar {cfg["calendar"].get("id", "primary")})'
           if cfg["mail"].get("gmail_api") or cfg["calendar"].get("enabled") else "no calendar connected, skip calendar checks")
    return {"MAIL_TOOLS": "; ".join(mail) or "no mail connected, skip mail checks", "CALENDAR_TOOLS": cal}


def variables(cfg: dict) -> dict:
    q = cfg["quiet_hours"]
    state = os.environ.get("EA_STATE_DIR", str(phconf.data_dir() / "state"))
    v = {
        "OWNER": cfg["owner"].get("name") or "there",
        "SCRIPTS": str(phconf.scripts_dir()),
        "STATE": state,
        "TIMEZONE": cfg.get("timezone", "UTC"),
        "QUIET": f'{int(q["start"]):02d}:00 to {int(q["end"]):02d}:00',
        "CHAT": cfg["chat"].get("label") or "the chat",
        "SLACK_CHANNELS": ", ".join(cfg["slack"].get("channels") or []) or "none listed, DMs only",
        "DAILY_CHANNELS": ", ".join(cfg["daily_call"].get("channels") or cfg["slack"].get("channels") or []) or "any channel",
    }
    v.update(tools(cfg))
    return v


def render(text: str, v: dict) -> str:
    for k, val in v.items():
        text = text.replace("{{" + k + "}}", val)
    return text


def prompt_for(job: dict, cfg: dict) -> str:
    v = dict(variables(cfg), NAME=PREFIX + job["name"], JOB=job.get("job", job["key"]), COMPOSE_FLAGS=job.get("flags", ""))
    if job.get("no_agent"):
        return job["body"]
    return render(HEAD + job["body"] + TAIL, v)


def install_runtime(cfg: dict) -> Path:
    dest = phconf.scripts_dir()
    dest.mkdir(parents=True, exist_ok=True)
    phconf.state_dir()
    for f in RUNTIME.glob("*.py"):
        target = dest / f.name
        if not target.is_file() or target.read_bytes() != f.read_bytes():
            shutil.copy2(f, target)
    contract = (HERE / "skills" / "proactive-desk" / "references" / "desk-contract.md").read_text()
    text = render(contract, variables(cfg))
    target = dest / "desk-contract.md"
    if not target.is_file() or target.read_text() != text:
        target.write_text(text)
    return dest


def _registry() -> Path:
    return phconf.data_dir() / "jobs.json"


def _load_registry() -> dict:
    try:
        return json.loads(_registry().read_text())
    except FileNotFoundError:
        return {}


def _save_registry(reg: dict) -> None:
    path = _registry()
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", dir=path.parent, prefix=".jobs-", delete=False) as f:
        tmp = Path(f.name)
        try:
            json.dump(reg, f, indent=1)
            f.close()
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


# `hermes cron` currently has no JSON mode. Only parse its labeled result and
# the indented ID/state header lines, never names, prompts or scheduler warnings.
_JOB_ID = r"[0-9a-f]{12}"


def _cron(run, *args: str) -> str:
    proc = run(["hermes", "cron", *args], capture_output=True, text=True, timeout=30,
               env={**os.environ, "HERMES_HOME": str(phconf.hermes_home()), "NO_COLOR": "1"})
    if proc.returncode:
        raise RuntimeError(f"hermes cron {args[0]} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


def _created_job_id(output: str) -> str:
    ids = re.findall(rf"^Created job: ({_JOB_ID})[ \t]*$", output, re.MULTILINE)
    if len(ids) != 1:
        raise RuntimeError("Could not read the created job ID from `hermes cron create`.")
    return ids[0]


def _list_job_ids(run) -> set[str]:
    output = _cron(run, "list", "--all")
    ids = set(re.findall(rf"^[ \t]+({_JOB_ID}) \[[^\]\n]+\]", output, re.MULTILINE))
    if not ids and not re.search(r"^No scheduled jobs\b", output, re.MULTILINE):
        raise RuntimeError("Could not read job IDs from `hermes cron list --all`.")
    return ids


def _remove(run, jid: str) -> None:
    output = _cron(run, "remove", jid)
    if not re.search(rf"^Removed job: .*\({re.escape(jid)}\)[ \t]*$", output, re.MULTILINE):
        raise RuntimeError(f"`hermes cron remove` did not confirm removal of {jid}.")


def _options(fields: dict, *, edit: bool) -> list[str]:
    argv = ["--name", fields["name"], "--deliver", fields["deliver"],
            "--failure-deliver", fields["failure_deliver"], "--no-wrap-response", "--catch-up", "off"]
    if fields["timezone"] or edit:
        argv.extend(["--timezone", fields["timezone"] or ""])
    if fields["script"] or edit:
        argv.extend(["--script", fields["script"] or ""])
    if fields["no_agent"]:
        argv.append("--no-agent")
    elif edit:
        argv.append("--agent")
    return argv


def _job_id(entry) -> str | None:
    # Earlier releases stored bare IDs. Reconcile once to learn their spec.
    return entry.get("id") if isinstance(entry, dict) else entry


def apply(cfg: dict, dry_run: bool = False, *, run=subprocess.run) -> list[dict]:
    reg = _load_registry()
    live = _list_job_ids(run) if reg else set()
    out = []
    if not dry_run:
        install_runtime(cfg)
    for job in JOBS:
        key = job["key"]
        entry = reg.get(key)
        jid = _job_id(entry)
        tz = cfg["daily_call"].get("timezone") if job.get("tz") == "daily" else cfg.get("timezone")
        fields = {
            "prompt": prompt_for(job, cfg),
            "schedule": cfg["schedule"][key],
            "name": PREFIX + job["name"],
            "deliver": "local",
            "failure_deliver": "local",
            "script": f"proactive-hermes/{job['script']}" if job.get("script") else None,
            "no_agent": bool(job.get("no_agent")),
            "timezone": tz,
            "wrap_response": False,
            "catch_up": False,
        }
        # The CLI list omits settings like prompt and catch-up. Cache the last
        # applied spec (not the private prompt text) for no-op reapplication.
        spec = sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
        if enabled(cfg, job):
            if jid not in live:
                action = "create"
                if not dry_run:
                    output = _cron(run, "create", *_options(fields, edit=False), "--",
                                   fields["schedule"], fields["prompt"])
                    reg[key] = {"id": _created_job_id(output), "spec": spec}
                    _save_registry(reg)
            elif isinstance(entry, dict) and entry.get("spec") == spec:
                action = "keep"
            else:
                action = "update"
                if not dry_run:
                    output = _cron(run, "edit", jid, "--schedule", fields["schedule"], "--prompt",
                                   fields["prompt"], *_options(fields, edit=True))
                    if not re.search(rf"^Updated job: {re.escape(jid)}[ \t]*$", output, re.MULTILINE):
                        raise RuntimeError(f"`hermes cron edit` did not confirm updating {jid}.")
                    reg[key] = {"id": jid, "spec": spec}
                    _save_registry(reg)
        else:
            action = "remove" if jid in live else "skip"
            if not dry_run and key in reg:
                if jid in live:
                    _remove(run, jid)
                del reg[key]
                _save_registry(reg)
        out.append({"job": fields["name"], "action": action, "schedule": fields["schedule"], "timezone": tz})
    return out


def remove_all(*, run=subprocess.run) -> list[str]:
    reg = _load_registry()
    live = _list_job_ids(run) if reg else set()
    gone = []
    for key, entry in list(reg.items()):
        jid = _job_id(entry)
        if jid in live:
            _remove(run, jid)
            gone.append(key)
        del reg[key]
        _save_registry(reg)
    return gone
