#!/usr/bin/env python3
"""Fast yes/no decisions for the desk through OpenAI's Decisions API (gpt-6-luna).

Used as a cheap first filter before any agent wakes up: "does this mail need the owner?",
"is this sent message a promise?", "is this the same matter?". About 0.35 s and $0.00003 per call.

Optional and fail-open: no OPENAI_API_KEY, a network error or a bad reply returns None and the
caller keeps today's behaviour (nothing is dropped). Sends sender, subject and the first 600 characters of the body.

  ea_decide.py ask --input TEXT --predicate "statement"          -> {"probability": 0.87}
  ea_decide.py ask --input TEXT --choice "question" --options a,b,c
  ea_decide.py status                                             -> key found? model?
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import phconf  # noqa: E402

URL = "https://api.openai.com/v1/decisions"
MODEL = phconf.CFG.get("decisions", {}).get("model") or "gpt-6-luna"
TIMEOUT = 12
MAX_QUESTIONS = 400
SNIPPET_CHARS = int(phconf.CFG.get("decisions", {}).get("body_chars", 600))  # first part of the body, enough to catch the ask in "Re: hi"
DROP_BELOW = float(phconf.CFG.get("decisions", {}).get("drop_below", 0.10))  # mail the owner almost certainly does not need; everything else is kept
HERE = Path(__file__).resolve().parent
STATE = phconf.state_dir()
LOG = STATE / "decisions_log.jsonl"

MAIL_CONTEXT = (
    "You triage email headers for a busy professional's executive-assistant desk. "
    "Needs the owner: a real person writing to them, a client or partner asking something, a bill or payment "
    "problem, a deadline, a booking or travel change, a security or account alert, legal or tax matters. "
    "Each message has sender, subject and, when available, the start of the body (start). "
    "Does not need the owner: newsletters, marketing, promotions, social notifications, automated receipts "
    "that need no action, digests."
)


def api_key() -> str:
    if not phconf.CFG.get("decisions", {}).get("enabled", True):
        return ""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key
    homes = [str(phconf.hermes_home()), os.environ.get("HERMES_HOME"), str(Path.home() / ".hermes")]
    for home in filter(None, homes):
        env = Path(home) / ".env"
        try:
            for line in env.read_text().splitlines():
                if line.startswith("OPENAI_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def _post(body: dict, key: str, timeout: float = TIMEOUT) -> dict:
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https URL)
        return json.loads(resp.read())


def decide(input_text, questions: list[dict], *, transport=None, key: str | None = None) -> list[dict] | None:
    """Return the API's output list, or None when unavailable (caller must fail open)."""
    key = key if key is not None else api_key()
    if not key or not questions:
        return None
    body = {"model": MODEL, "input": input_text, "questions": questions[:MAX_QUESTIONS]}
    try:
        data = (transport or _post)(body, key)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    out = (data.get("answers") or data.get("output")) if isinstance(data, dict) else None
    return out if isinstance(out, list) else None


def _log(row: dict) -> None:
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **row},
                                ensure_ascii=False) + "\n")
    except OSError:
        pass


OWN = phconf.work_emails() | {e.lower() for e in phconf.CFG["owner"].get("personal_emails", [])}


# Replies in a thread and calendar responses are never dropped: a human is involved.
REPLY_OR_CALENDAR = ("re:", "re :", "aw:", "sv:", "vs:", "fw:", "fwd:", "rv:", "accepted:", "declined:",
                     "tentative:", "aceptada:", "rechazada:", "tentativa:", "new time proposed", "invitation:",
                     "invitación:", "updated invitation", "invitación actualizada")


def triage_mail(messages: list[dict], *, transport=None, key: str | None = None,
                drop_below: float = DROP_BELOW, own: set | None = None) -> tuple[list[dict], list[dict], dict]:
    """Split messages into (keep, dropped, info). Fails open: on any problem keep all.

    Never drops: the owner's own mail (the promise tracker needs it) or any reply in a thread ("Re:").
    """
    if not messages:
        return [], [], {"used": False}
    own = {e.lower() for e in (OWN if own is None else own)}
    exempt = [m for m in messages if (m.get("from") or "").lower() in own
              or (m.get("subject") or "").lower().startswith(REPLY_OR_CALENDAR)]
    rest = [m for m in messages if m not in exempt]
    if not rest:
        return [{k: v for k, v in m.items() if k != "snippet"} for m in messages], [], {"used": False, "reason": "all exempt"}
    keep, dropped, info = _triage(rest, transport=transport, key=key, drop_below=drop_below)
    keep = [{k: v for k, v in m.items() if k != "snippet"} for m in exempt] + keep
    if info.get("used"):
        info["kept"], info["exempt"] = len(keep), len(exempt)
    return keep, dropped, info


def _triage(messages, *, transport, key, drop_below):
    listing = [{"n": i + 1, "from": m.get("from", ""), "subject": m.get("subject", ""),
                **({"start": m["snippet"][:SNIPPET_CHARS]} if m.get("snippet") else {})}
               for i, m in enumerate(messages[:MAX_QUESTIONS])]
    questions = [{"type": "predicate", "name": f"m{row['n']}",
                  "instructions": f"Message {row['n']} needs the owner personally to act on or decide about it soon."}
                 for row in listing]
    out = decide(MAIL_CONTEXT + "\n\nMessages:\n" + json.dumps(listing, ensure_ascii=False), questions,
                 transport=transport, key=key)
    if out is None:
        return list(messages), [], {"used": False, "reason": "unavailable"}
    probs = {r.get("name"): r.get("probability") for r in out if isinstance(r, dict)}
    keep, dropped = [], []
    for i, m in enumerate(messages):
        p = probs.get(f"m{i + 1}")
        if not isinstance(p, (int, float)):  # missing answer: keep
            keep.append(m)
            continue
        row = {**m, "p_owner": round(float(p), 2)}
        (dropped if p < drop_below else keep).append(row)
    keep = [{k: v for k, v in m.items() if k != "snippet"} for m in keep]  # the agent reads mail itself
    _log({"kind": "mail", "kept": len(keep), "dropped": [{"from": d.get("from"), "subject": d.get("subject"),
                                                          "p": d.get("p_owner")} for d in dropped]})
    return keep, dropped, {"used": True, "model": MODEL, "kept": len(keep), "dropped": len(dropped)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask")
    a.add_argument("--input", required=True)
    a.add_argument("--predicate")
    a.add_argument("--choice")
    a.add_argument("--options", default="")
    sub.add_parser("status")
    args = p.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps({"key_found": bool(api_key()), "model": MODEL}))
        return 0
    if args.predicate:
        q = {"type": "predicate", "name": "q", "instructions": args.predicate}
    elif args.choice:
        opts = [o.strip() for o in args.options.split(",") if o.strip()]
        q = {"type": "choice", "name": "q", "instructions": args.choice, "choices": [{"value": o} for o in opts]}
    else:
        p.error("--predicate or --choice is required")
    out = decide(args.input, [q])
    print(json.dumps(out[0] if out else {"unavailable": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
