"""Proactive Hermes: a quiet, proactive executive-assistant desk for Hermes Agent.

Registers the `hermes proactive` CLI (setup, status, remove, questions), the `/proactive` slash command,
and two bundled skills: `proactive-setup` (the guided interview) and `proactive-desk` (how to work
the desk in chat: drafts, close-outs, promises). The scheduled jobs and runtime scripts are installed by
`hermes proactive setup`; this module only registers.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
SKILLS = {
    "proactive-setup": "Use when setting up Proactive Hermes. Interview, then apply.",
    "proactive-desk": "Use when answering Proactive Hermes desk messages.",
}


def _slash(raw_args: str = "", **_):
    arg = (raw_args or "").strip().lower()
    if arg in ("", "setup"):
        return ("Load the proactive-setup skill (skill_view name='proactive-hermes:proactive-setup') and run the "
                "setup interview with me now.")
    if arg == "status":
        return "Run `hermes proactive status` in the terminal and summarise it for me in 3 short lines."
    return "Usage: /proactive [setup|status]"


def register(ctx) -> None:
    from . import cli
    ctx.register_cli_command(
        name="proactive", help="Proactive Hermes: set up, status, remove",
        setup_fn=cli.setup_parser, handler_fn=cli.handle,
        description="Set up the proactive desk with Juan's setup or your own answers: `hermes proactive setup`.")
    ctx.register_command("proactive", _slash, description="Set up or check Proactive Hermes",
                         args_hint="[setup|status]")
    for name, desc in SKILLS.items():
        try:
            ctx.register_skill(name=name, path=HERE / "skills" / name / "SKILL.md", description=desc)
        except Exception as exc:
            logger.warning("proactive-hermes: could not register skill %s (%s)", name, exc)
