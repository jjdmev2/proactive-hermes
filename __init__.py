"""Proactive Hermes: a quiet, proactive executive-assistant desk for Hermes Agent.

Registers the `hermes proactive` CLI (setup, status, remove, questions), the `/proactive` slash command,
and three bundled skills: `proactive-setup` (the guided interview), `proactive-desk` (how to work
the desk in chat: drafts, close-outs, promises) and `browser-bookings` (approval-gated rides, tickets,
food). The scheduled jobs and runtime scripts are installed only by `hermes proactive setup`; loading the plugin
never writes them. After a plugin update, `hermes proactive doctor` reports `runtime out of date: run hermes
proactive setup` until setup runs again.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
SKILLS = {
    "proactive-setup": "Use when setting up Proactive Hermes. Interview, then apply.",
    "proactive-desk": "Use when answering Proactive Hermes desk messages.",
    "browser-bookings": "Use when booking a ride, ticket or food via the browser.",
}


def _slash(raw_args: str = "", **_):
    arg = (raw_args or "").strip().lower()
    if arg in ("", "setup"):
        return ("Load the proactive-setup skill (skill_view name='proactive-hermes:proactive-setup') and run the "
                "setup interview with me now.")
    if arg == "status":
        return "Run `hermes proactive status` in the terminal and summarise it for me in 3 short lines."
    return "Usage: /proactive [setup|status]"


SKIP_PLATFORMS = {"cron", "subagent", "delegation", "batch", "oneshot", "webhook"}
OFFER = ("[Proactive Hermes was just installed and is not set up yet. Answer the user's message as usual, then add "
         "one short line offering setup, e.g. \"By the way, Proactive Hermes is installed. Want me to set up your "
         "proactive desk? It takes about 3 minutes.\" If they say yes, load the skill "
         "proactive-hermes:proactive-setup and follow it. Offer it only this once.]")


def _offer_setup(session_id: str = "", user_message: str = "", conversation_history=None,
                 is_first_turn: bool = False, model: str = "", platform: str = "", **_):
    """Once per profile, in a real chat, while not set up: nudge the model to offer setup. Never runs setup."""
    try:
        import sys
        sys.path.insert(0, str(HERE / "runtime"))
        import phconf
        if (platform or "").lower() in SKIP_PLATFORMS or phconf.config_path().exists():
            return None
        flag = phconf.data_dir() / "setup_offered"
        if flag.exists():
            return None
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(platform or "chat")
        return {"context": OFFER}
    except Exception:
        return None


def _start_mail_push(ctx) -> None:
    """Mail push (IMAP IDLE pokes the mail radar) runs only inside the gateway; CLI loads skip it."""
    try:
        import sys
        sys.path.insert(0, str(HERE / "runtime"))
        import mail_idle
        outcome = mail_idle.start_in_gateway(ctx)
        logger.debug("proactive-hermes: mail push %s", outcome)
    except Exception as exc:
        logger.warning("proactive-hermes: mail push not started (%s)", type(exc).__name__)


def register(ctx) -> None:
    from . import cli
    ctx.register_cli_command(
        name="proactive", help="Proactive Hermes: set up, status, remove",
        setup_fn=cli.setup_parser, handler_fn=cli.handle,
        description="Set up the proactive desk with Juan's setup or your own answers: `hermes proactive setup`.")
    ctx.register_command("proactive", _slash, description="Set up or check Proactive Hermes",
                         args_hint="[setup|status]")
    ctx.register_hook("pre_llm_call", _offer_setup)
    _start_mail_push(ctx)
    for name, desc in SKILLS.items():
        try:
            ctx.register_skill(name=name, path=HERE / "skills" / name / "SKILL.md", description=desc)
        except Exception as exc:
            logger.warning("proactive-hermes: could not register skill %s (%s)", name, exc)
