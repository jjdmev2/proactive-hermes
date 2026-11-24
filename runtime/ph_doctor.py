#!/usr/bin/env python3
"""Pre-flight checks: can the desk reach the owner's Bot Chat, and is the phone set up?

    ph_doctor.py            JSON report: gateway, bot_chat, mobile, decisions, mail, calendar
    ph_doctor.py --send-test   also posts one test message into Bot Chat (proves live delivery)

Every desk message is delivered as a real reply in this profile's Bot Chat (Hermes Bot Mode),
through this profile's running gateway. No gateway or no Bot Mode means no messages.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import phconf  # noqa: E402

FIX = {
    "gateway": "Start Hermes for this profile: open the Hermes desktop app (or run `hermes gateway start`). "
               "Desk messages are delivered through the running gateway.",
    "bot_chat": "Turn on Bot Mode: in the Hermes desktop app open the Bots tab (on by default) and click this "
                "profile's Bot once. Its Bot Chat is where every desk message lands. It is also created on the first delivery.",
    "mobile": "Install the Hermes mobile app, sign in to the same Hermes, open this Bot's chat once, and share "
              "Reminders, Alarms, Location and Calendar in the app settings. Notifications for desk messages come from the app.",
    "decisions": "Add OPENAI_API_KEY to this profile's .env and a few dollars of credit at platform.openai.com.",
    "mail": "Set up the google-workspace skill (Gmail) or a himalaya mail account.",
}


def _agent_path() -> None:
    root = Path(os.environ.get("HERMES_AGENT_ROOT", Path.home() / ".hermes" / "hermes-agent"))
    if root.exists() and str(root) not in sys.path:
        sys.path.insert(0, str(root))


def check_gateway(home: Path) -> tuple[bool, str]:
    _agent_path()
    try:
        from tools.bot_live_delivery import find_canonical_live_owner
        owner = find_canonical_live_owner(home)
    except ValueError as exc:
        return False, str(exc)
    except Exception as exc:  # older core or no store
        return False, f"{type(exc).__name__}: {exc}"[:160]
    if owner is None:
        return False, "this profile has no session store yet"
    return True, "running"


def check_bot_chat(home: Path) -> tuple[bool, str]:
    _agent_path()
    try:
        from tools.bot_live_delivery import _bot_chat_tip
        tip = _bot_chat_tip(home)
    except Exception as exc:
        return False, f"{type(exc).__name__}"[:80]
    return (True, "found") if tip else (False, "not created yet (it is created on the first delivery)")


def check_mobile(home: Path) -> tuple[bool, str]:
    db = home / "plugin-data" / "hermes-mobile-device-access" / "bridge.db"
    plugin = any((home / "plugins" / n).exists() for n in ("hermes-mobile-device-access", "hermes-mobile-push"))
    if db.exists():
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
        dec = (bool(ea_decide.api_key()), "OpenAI key found" if ea_decide.api_key() else "no OPENAI_API_KEY")
    except Exception:
        dec = (False, "unavailable")
    mail = (phconf.google_api().exists() or bool(phconf.CFG["mail"].get("himalaya_accounts")), "")
    for name, (ok, note) in {"gateway": gw, "bot_chat": bc, "mobile": mob, "decisions": dec, "mail": mail}.items():
        row = {"ok": ok}
        if note:
            row["note"] = note
        if not ok:
            row["fix"] = FIX[name]
        out["checks"][name] = row
    # Bot Chat may be missing until the first delivery; the gateway is the hard requirement.
    out["ready"] = gw[0]
    if send_test:
        if not gw[0]:
            out["test_message"] = {"status": "skipped", "reason": "gateway not running"}
        else:
            import desk_say
            owner = phconf.owner()
            res = desk_say.deliver(f"Proactive Hermes is connected, {owner}. Desk messages will land here, like this one.",
                                   key="proactive-hermes-test", job="setup", urgent=True)
            out["test_message"] = {"status": res.get("status"), "id": res.get("id")}
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--send-test", action="store_true")
    a = p.parse_args(argv)
    print(json.dumps(report(a.send_test), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
