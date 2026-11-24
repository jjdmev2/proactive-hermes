#!/usr/bin/env python3
"""Fast yes/no decisions for the desk through OpenAI's Decisions API (gpt-6-luna).

Used as a cheap first filter before any agent wakes up: "does this mail need the owner?",
"is this sent message a promise?", "is this the same matter?". About 0.35 s and $0.00003 per call.

Optional and fail-open: no OPENAI_API_KEY, a network error or a bad reply returns None and the
caller keeps today's behaviour (nothing is dropped). Only headers or short snippets are sent.

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
DROP_BELOW = float(phconf.CFG.get("decisions", {}).get("drop_below", 0.20))  # mail the owner almost certainly does not need; everything else is kept
HERE = Path(__file__).resolve().parent
STATE = phconf.state_dir()
LOG = STATE / "decisions_log.jsonl"

MAIL_CONTEXT = (
    "You triage email headers for a busy professional's executive-assistant desk. "
    "Needs the owner: a real person writing to them, a client or partner asking something, a bill or payment "
    "problem, a deadline, a booking or travel change, a security or account alert, legal or tax matters. "
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


def triage_mail(messages: list[dict], *, transport=None, key: str | None = None,
                drop_below: float = DROP_BELOW) -> tuple[list[dict], list[dict], dict]:
    """Split header-only messages into (keep, dropped, info). Fails open: on any problem keep all."""
    if not messages:
        return [], [], {"used": False}
    listing = [{"n": i + 1, "from": m.get("from", ""), "subject": m.get("subject", "")}
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
