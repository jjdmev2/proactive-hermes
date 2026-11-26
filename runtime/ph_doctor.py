#!/usr/bin/env python3
"""Pre-flight checks: can the desk reach the owner's Bot Chat, and is the phone set up?

    ph_doctor.py            JSON report: gateway, bot_chat, say_job, mobile, decisions, mail, authority, runtime, and
                            mail_push (per mailbox: "instant push", or "15 min checks" with the reason
                            and the next step; never an address or a password)
    ph_doctor.py --send-test   also sends one test message to Bot Chat through the say job and waits up to a
                               minute for the Bot's reply (proves live delivery)

Every desk message is delivered as a real Bot reply in this profile's Bot Chat (Hermes Bot Mode): the plugin's
no-agent job "Proactive · say" hands it over with Hermes's public cron Bot Chat delivery (`--deliver bot-chat`),
which needs this profile's running gateway. Only public Hermes surfaces are used: `hermes gateway status`,
`hermes cron list`, cron.plugin_api.delivery_platforms and the read-only persistence reader.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import phconf  # noqa: E402

FIX = {
    "gateway": "Start Hermes for this profile: open the Hermes desktop app (or run `hermes gateway start`). "
               "Desk messages are delivered through the running gateway.",
    "bot_chat": "Turn on Bot Mode: in the Hermes desktop app open the Bots tab (on by default) and click this "
                "profile's Bot once. Its Bot Chat is where every desk message lands. It is also created on the first delivery.",
    "say_job": "Run `hermes proactive setup`: it installs the \"Proactive · say\" job that hands desk messages to Bot Chat. "
               "If it is paused, resume it with `hermes cron resume <id>`. If it delivers anywhere but Bot Chat, set it "
               "back with `hermes cron edit <id> --deliver bot-chat` (the health check also does this).",
    "mobile": "Install the Hermes mobile app, sign in to the same Hermes, open this Bot's chat once, and share "
              "Reminders, Alarms, Location and Calendar in the app settings. Notifications for desk messages come from the app.",
    "decisions": "Set PROACTIVE_OPENAI_API_KEY: Settings -> Plugins -> Proactive Hermes -> OpenAI key for the decision "
                 "filter, or `hermes plugins config proactive-hermes`. It can be the same key as OPENAI_API_KEY; "
                 "Hermes hides OPENAI_API_KEY from scheduled scripts. Put a few dollars of credit on platform.openai.com.",
    "mail": "Set up the google-workspace skill (Gmail) or a himalaya mail account.",
    "authority": "authority config malformed: run `hermes proactive setup` to write the approval levels again. "
                 "Until then every reminder, alarm and calendar block is refused.",
    "runtime": "Run `hermes proactive setup`: it installs or updates the runtime scripts and the desk contract.",
}


def check_authority() -> tuple[bool, str]:
    problem = phconf.authority_problem()
    return (False, f"authority config malformed: {problem}") if problem else (True, "")


def plugin_root() -> Path | None:
    """The plugin checkout this doctor runs from; None when it runs from the installed copy."""
    root = HERE.parent
    return root if (root / "plugin.yaml").is_file() and HERE.resolve() != phconf.scripts_dir().resolve() else None


def check_runtime() -> tuple[bool, str]:
    """Is the installed runtime the one this plugin ships? An upgrade without a new setup leaves old scripts (only
    setup installs them). When setup would refuse to install here, the note names why (e.g. the symlink)."""
    state, note = phconf.runtime_status(plugin_root())
    problem = phconf.runtime_destination_problem()
    if problem:
        return False, f"{note}; setup would refuse: {problem}"
    return state in ("ok", "unknown"), note


def _agent_path() -> None:
    root = phconf.agent_root()   # derived from the active Hermes home, never from an environment variable
    if root.exists() and str(root) not in sys.path:
        sys.path.append(str(root))


def _hermes(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    exe = shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")
    return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                          env={**os.environ, "HERMES_HOME": str(phconf.hermes_home()), "NO_COLOR": "1"})


_RUNNING = re.compile(r"\bgateway (?:service |process )?is running\b", re.IGNORECASE)


def gateway_running(output: str) -> tuple[bool, str]:
    """Read the whole `hermes gateway status` output: a manual run prints "Gateway is running (PID ...)", a
    multiplexed profile "Gateway is running via the default-profile multiplexer", a systemd or launchd service
    prints its status block first and then "User gateway service is running"."""
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    hit = next((line for line in lines if _RUNNING.search(line)), None)
    if hit:
        return True, "running"
    return False, (next((line for line in lines if "not running" in line.lower()), None)
                   or (lines[0] if lines else "gateway status unreadable"))[:160]


def check_gateway(home: Path) -> tuple[bool, str]:
    """`hermes gateway status` (public CLI), every line of it."""
    try:
        proc = _hermes("gateway", "status")
    except Exception as exc:
        return False, f"{type(exc).__name__}"[:80]
    ok, note = gateway_running(proc.stdout or "")
    if not ok and not (proc.stdout or "").strip():
        note = ((proc.stderr or "").strip() or note)[:160]
    return ok, note


def check_bot_chat(home: Path) -> tuple[bool, str]:
    """Read only, through Hermes's public persistence reader."""
    _agent_path()
    try:
        from persistence_assembly import open_persistence_reader, profile_scope, store_exists
        if not store_exists(profile_scope(home=home)):
            return False, "this profile has no session store yet"
        with open_persistence_reader(home) as store:
            row = store.sessions.get_by_title("Bot Chat")
    except Exception as exc:
        return False, f"{type(exc).__name__}"[:80]
    return (True, "found") if row else (False, "not created yet (it is created on the first delivery)")


def check_say_job() -> tuple[bool, str]:
    """The say job is recorded by setup, listed by `hermes cron list --all` as active, and delivers to Bot Chat
    and nowhere else (cron.plugin_api.delivery_platforms; the job takes nothing otherwise)."""
    import desk_say
    jid = desk_say.say_job_id()
    if not jid:
        return False, "not installed"
    try:
        out = _hermes("cron", "list", "--all").stdout or ""
    except Exception as exc:
        return False, f"{type(exc).__name__}"[:80]
    m = re.search(rf"^[ \t]+{re.escape(jid)} \[([^\]\n]+)\]", out, re.MULTILINE)
    if not m:
        return False, f"job {jid} not found"
    if m.group(1) != "active":
        return False, f"job {jid} is {m.group(1)}"
    problem = desk_say.route_problem(jid)
    return (False, f"job {jid}: {problem}") if problem else (True, jid)


def check_mobile(home: Path) -> tuple[bool, str]:
    plugin = any((home / "plugins" / n).exists() for n in ("hermes-mobile-device-access", "hermes-mobile-push"))
    try:
        from contextlib import closing
        import ea_reminders
        with closing(ea_reminders.bridge(read_only=True)) as db:   # the one mobile bridge access point
            paired = bool(db.execute("SELECT count(*) FROM devices").fetchone()[0])
    except Exception:
        paired = False
    if paired:
        return True, "phone paired"
    return (False, "mobile plugins found, no phone paired yet") if plugin else (False, "Hermes mobile app not connected")


def report(send_test: bool = False) -> dict:
    home = phconf.hermes_home()
    out: dict = {"profile": phconf.profile_name(home), "checks": {}}
    gw = check_gateway(home)
    bc = check_bot_chat(home)
    mob = check_mobile(home)
    try:
        import ea_decide
        dec = ea_decide.key_status()   # what the scheduled jobs will see, not just this command
    except Exception:
        dec = (False, "unavailable")
    mail = (phconf.google_api().exists() or bool(phconf.CFG["mail"].get("himalaya_accounts")), "")
    try:
        say = check_say_job()
    except Exception as exc:
        say = (False, f"{type(exc).__name__}"[:80])
    checks = {"gateway": gw, "bot_chat": bc, "say_job": say, "mobile": mob, "decisions": dec, "mail": mail,
              "authority": check_authority(), "runtime": check_runtime()}
    for name, (ok, note) in checks.items():
        row = {"ok": ok}
        if note:
            row["note"] = note
        if not ok:
            row["fix"] = FIX[name]
        out["checks"][name] = row
    # One row per mailbox: instant push (IMAP IDLE), or the radar schedule with the reason and the next step.
    try:
        import mail_idle
        out["mail_push"] = mail_idle.push_report()
    except Exception as exc:
        out["mail_push"] = {"all": {"mode": "15 min checks", "reason": f"push status unreadable ({type(exc).__name__})",
                                    "next": "Run `hermes proactive doctor` again; if it stays, report it."}}
    # Bot Chat may be missing until the first delivery; the gateway and the say job are the hard requirements.
    out["ready"] = gw[0] and say[0]
    if send_test:
        if not out["ready"]:
            out["test_message"] = {"status": "skipped",
                                   "reason": "gateway not running" if not gw[0] else "say job not ready"}
        else:
            out["test_message"] = send_test_message()
    return out


def send_test_message(wait_s: float = 60) -> dict:
    """Queue one test message, kick the say job, then wait for the Bot's reply in Bot Chat (read only)."""
    import desk_say
    owner = phconf.owner()
    text = (f"Proactive Hermes is connected, {owner}. Desk messages will land here, like this one. "
            f"({time.strftime('%H:%M')})")   # a new text each run, so a second test is not a duplicate
    res = desk_say.deliver(text, key="proactive-hermes-test", job="setup", urgent=True)
    row = {"status": res.get("status"), "id": res.get("id")}
    if res.get("status") != "handed":
        row["reason"] = res.get("kick") or res.get("reason") or ""
        return row
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        desk_say.confirm()   # the say job's own run may have confirmed it already: check the id either way
        if res.get("id") in desk_say.delivered_ids():
            row["status"] = "delivered"
            return row
        time.sleep(3)
    row["reason"] = "handed to Bot Chat; the Bot's reply did not appear within a minute"
    return row


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--send-test", action="store_true")
    a = p.parse_args(argv)
    print(json.dumps(report(a.send_test), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
