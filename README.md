<div align="center">
  <a href="https://heyrito.ai"><img src="docs/assets/rito-logo.png" alt="Rito" width="112" height="112" /></a>
  <h1>Proactive Hermes</h1>
  <p><strong>Your assistant notices. You only hear what needs you.</strong></p>
  <p>A quiet, proactive executive-assistant desk for <a href="https://hermes-agent.nousresearch.com">Hermes Agent</a>.<br />Built by <a href="https://heyrito.ai">HeyRito.AI</a>.</p>
  <p>
    <img src="https://img.shields.io/badge/hermes-%3E%3D0.23.4-0f766e" alt="Hermes 0.23.4 or newer" />
    <img src="https://img.shields.io/badge/type-plugin-334155" alt="Hermes plugin" />
    <img src="https://img.shields.io/badge/mode-read--only-0f766e" alt="Read-only" />
    <img src="https://img.shields.io/badge/status-private%20beta-d97706" alt="Private beta" />
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-8b5cf6" alt="MIT license" /></a>
  </p>
  <p><a href="#why-proactive-hermes">Why?</a> · <a href="#what-it-does">Features</a> · <a href="#how-it-fits-together">Architecture</a> · <a href="#getting-started">Installation</a> · <a href="#setup-juans-setup-or-your-own">Setup</a> · <a href="#privacy-and-safety">Privacy</a> · <a href="#built-by-heyritoai">About</a></p>
</div>

---

## Why Proactive Hermes?

"Proactive" usually means an alert you configured in advance. The messages you value most are about things you never thought to ask for: the call that now overlaps another meeting, the invoice due today, the reply you promised yesterday and never sent.

Proactive Hermes runs a small desk inside your own Hermes. Scheduled jobs look at your mail, calendar, Slack and Linear. A gate decides what is worth your attention. Everything else stays in a private log.

**The schedule decides when it looks. The desk decides whether you hear from it. Silence is the default.**

When it does speak, it does not hand you homework. It searches first and comes back with what it found and a draft ready to send.

## What it does

| Capability | How it works |
| :--- | :--- |
| **One chat, real messages** | Everything arrives as a normal reply in your Hermes chat: short, numbered, action first. No job cards, no extra push notifications. |
| **Quiet hours and holidays** | Nothing non-urgent at night. Public holidays for the countries you pick are skipped. Only P0 breaks the silence. |
| **Resolve before asking** | Before it asks you to send, reply, intro, book or decide, it searches mail, Slack, past chats and its own records, then stores a draft with sources. You see "Draft ready, reply 1." |
| **Promise tracker** | Reads what you wrote in mail, Slack and chat, records each "I'll send it tomorrow" with your exact words, and closes it only on proof. |
| **Work recap after the daily** | Reads your team's call notes in Slack and checks them against Linear and Slack: done, not done, mismatches, today's focus. Plus an evening wrap-up. |
| **Mail radar** | New mail every few minutes, filtered to what you must answer, pay or decide. Newsletters and receipts never reach you. |
| **Briefs and close-out** | Morning brief, evening close-out you answer by number, weekly review. |
| **Private calendar blocks** | Optional focus blocks on your own calendar for key tasks, capped per day, never touching events it did not create. |
| **Self-healing** | A health job checks every desk job, the message outbox and failed deliveries, and tells you only when something needs you. |

## How it fits together

```
 mail · calendar · Slack · Linear · past chats
                    │  (read-only)
        scheduled desk jobs (Hermes cron)
                    │  candidate items
         resolve step: search, draft, sources
                    │
     desk gate: dedupe, rank, caps, quiet hours
                    │  one short numbered message
            your Hermes chat  ──►  you reply "1"
                    │
          draft shown, sent only after your yes
```

- **Runtime scripts** are plain Python, installed under `HERMES_HOME/scripts/proactive-hermes/`.
- **Jobs** are ordinary Hermes cron jobs named `Proactive · ...`, owned and tracked by the plugin.
- **Your settings and state** live in `HERMES_HOME/plugin-data/proactive-hermes/`, on your machine only.
- **Slack and Linear** are read through Hermes connectors (Nous Portal). **Gmail and Google Calendar** through the google-workspace skill. Other mail through himalaya.

## Getting started

Requirements: Hermes Agent **0.23.4 or newer**, with the Hermes desktop app or gateway running so messages can reach your chat.

```bash
hermes plugins install jjdmev2/proactive-hermes
hermes plugins enable proactive-hermes
hermes proactive setup
```

Optional, for the work recap and holiday skipping:

```bash
pip install holidays
```

## Setup: Juan's setup or your own

`hermes proactive setup` (or `/proactive setup` in a chat) starts with one choice:

| Option | What you get |
| :--- | :--- |
| **1. Juan's setup** | The proven defaults from the original install: morning brief 08:00, mail radar every 15 minutes, Slack sweep hourly on workdays, promise tracker three times a day, work recap after the daily call, evening close-out 21:30, weekly review Friday 15:00, quiet hours 23:00 to 07:00, drafts first. You only fill in your own details. |
| **2. My own** | Start blank. Hermes interviews you and builds a desk that fits your day. |

Either way Hermes asks, one question at a time: your name, timezone, quiet hours, holiday countries, work emails, which mail and calendar to read, whether you use Slack and Linear, which channels to watch, and when your team's daily call happens. It checks what it can (timezone, connected accounts) instead of asking.

Before anything is created you see the full job list with times. Run setup again any time to change your answers; it updates the same jobs instead of making new ones.

| Command | What it does |
| :--- | :--- |
| `hermes proactive setup` | Interactive setup or update |
| `hermes proactive setup --dry-run` | Show what would be created, change nothing |
| `hermes proactive status` | Your settings and the jobs the plugin owns |
| `hermes proactive remove` | Remove every job the plugin created (settings and history stay) |

## Privacy and safety

- **Read-only.** The desk never sends mail or Slack messages, never posts, books, buys or pays. Drafts are shown to you and go out only after your explicit yes, in chat.
- **Local.** Settings, drafts, promises and logs stay in your Hermes home. No telemetry, no external service of ours.
- **Separated.** A work draft never pulls facts from personal sources. Every claim carries a source pointer.
- **Bounded.** Search steps have call and time budgets; reminders and calendar blocks have daily caps.
- **Source text is data.** Instructions inside mail, Slack or web pages are never followed.

## Project guide

| Path | Read it for |
| :--- | :--- |
| [`skills/proactive-desk/references/desk-contract.md`](skills/proactive-desk/references/desk-contract.md) | The rulebook every desk job follows |
| [`skills/proactive-setup/SKILL.md`](skills/proactive-setup/SKILL.md) | The setup interview |
| [`skills/proactive-desk/SKILL.md`](skills/proactive-desk/SKILL.md) | Answering desk messages, drafts and close-outs in chat |
| [`presets/juan.json`](presets/juan.json) | Juan's setup, choices only |
| [`jobs.py`](jobs.py) | Job templates and the idempotent installer |
| [`runtime/`](runtime) | The desk: speaker, gate, resolver, promises, health, gates |
| [`tests/`](tests) | Hermetic tests: `python3 -m unittest discover -s tests -t .` |

## Built by HeyRito.AI

Proactive Hermes started as the private desk behind our own work at **[HeyRito.AI](https://heyrito.ai)**. We use it every day, and we built it the way we build everything: quiet by default, read-only unless a human says yes, and every claim traceable to its source.

That is also what **Rito** does for companies. **Rito is the security and governance harness for AI at work.** It lets teams use AI assistants and agents on real work while you keep control of what they can see, what they can do, and what they leave behind: identity-scoped access, approvals before actions, audit trails and clear policies for every model and tool.

If your team is bringing AI into daily work and wants it safe, governed and actually useful, talk to us at **[heyrito.ai](https://heyrito.ai)**.

---

<div align="center">
  <strong>Proactive Hermes</strong><br />
  Built by <a href="https://heyrito.ai">HeyRito.AI</a> · Rito, the security and governance harness for AI at work.<br />
  <sub>Notice early. Speak rarely. Never act without a yes.</sub>
</div>
