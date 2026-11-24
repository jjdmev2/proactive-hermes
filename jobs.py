"""Proactive Hermes job templates and the idempotent installer behind `hermes proactive setup`.

Each job is owned by the plugin: its id is recorded in <plugin-data>/jobs.json and its name starts
with "Proactive ·". `apply()` installs the runtime scripts under HERMES_HOME/scripts/proactive-hermes
(cron gate scripts must live under HERMES_HOME/scripts), renders the prompts from config.json, and
creates, updates or removes jobs so the set always matches the enabled features. Running it twice
changes nothing.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

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
    v = {
        "OWNER": cfg["owner"].get("name") or "there",
        "SCRIPTS": str(phconf.scripts_dir()),
        "STATE": str(phconf.state_dir()),
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
    for f in RUNTIME.glob("*.py"):
        shutil.copy2(f, dest / f.name)
    contract = (HERE / "skills" / "proactive-desk" / "references" / "desk-contract.md").read_text()
    (dest / "desk-contract.md").write_text(render(contract, variables(cfg)))
    return dest


def _registry() -> Path:
    return phconf.data_dir() / "jobs.json"


def _load_registry() -> dict:
    try:
        return json.loads(_registry().read_text())
    except (FileNotFoundError, ValueError):
        return {}


def apply(cfg: dict, dry_run: bool = False) -> list[dict]:
    from cron import jobs as J  # Hermes core

    reg = _load_registry()
    out = []
    if not dry_run:
        install_runtime(cfg)
    for job in JOBS:
        want = enabled(cfg, job)
        jid = reg.get(job["key"])
        existing = J.get_job(jid) if jid else None
        tz = cfg["daily_call"].get("timezone") if job.get("tz") == "daily" else cfg.get("timezone")
        fields = {
            "prompt": prompt_for(job, cfg),
            "schedule": cfg["schedule"][job["key"]],
            "name": PREFIX + job["name"],
            "deliver": "local",
            "failure_deliver": "local",
            "script": f"proactive-hermes/{job['script']}" if job.get("script") else None,
            "no_agent": bool(job.get("no_agent")),
            "timezone": tz,
            "wrap_response": False,
            "catch_up": False,
        }
        if want and not existing:
            action = "create"
            if not dry_run:
                created = J.create_job(**fields)
                reg[job["key"]] = created["id"]
        elif want and existing:
            action = "update"
            if not dry_run:
                upd = {k: v for k, v in fields.items() if k != "schedule"}
                J.update_job(jid, upd)
                if existing.get("schedule", {}).get("expr") != fields["schedule"] or existing.get("timezone") != tz:
                    J.remove_job(jid)
                    reg[job["key"]] = J.create_job(**fields)["id"]
        elif not want and existing:
            action = "remove"
            if not dry_run:
                J.remove_job(jid)
                reg.pop(job["key"], None)
        else:
            action = "skip"
            reg.pop(job["key"], None)
        out.append({"job": PREFIX + job["name"], "action": action, "schedule": fields["schedule"], "timezone": tz})
    if not dry_run:
        _registry().write_text(json.dumps(reg, indent=1))
    return out


def remove_all() -> list[str]:
    from cron import jobs as J

    reg = _load_registry()
    gone = []
    for key, jid in reg.items():
        if J.get_job(jid):
            J.remove_job(jid)
            gone.append(key)
    _registry().write_text("{}")
    return gone
