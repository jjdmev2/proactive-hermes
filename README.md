<div align="center">
  <a href="https://heyrito.ai"><img src="docs/assets/rito-logo.png" alt="Rito" width="112" height="112" /></a>
  <h1>Proactive Hermes</h1>
  <p><strong>Your assistant reads everything. You only hear what needs you.</strong></p>
  <p>A proactive executive-assistant desk for <a href="https://hermes-agent.nousresearch.com">Hermes Agent</a>.<br />Built by <a href="https://heyrito.ai">HeyRito.AI</a>.</p>
  <p>
    <img src="https://img.shields.io/badge/hermes-%3E%3D0.23.4-0f766e" alt="Hermes 0.23.4 or newer" />
    <img src="https://img.shields.io/badge/type-plugin-334155" alt="Hermes plugin" />
    <img src="https://img.shields.io/badge/mode-read--only-0f766e" alt="Read-only" />
    <img src="https://img.shields.io/badge/decisions-OpenAI%20gpt--6--luna-0f766e" alt="OpenAI Decisions API" />
    <img src="https://img.shields.io/badge/status-private%20beta-d97706" alt="Private beta" />
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-8b5cf6" alt="MIT license" /></a>
  </p>
  <p>
    <a href="#install"><strong>Install</strong></a> · <a href="#set-it-up"><strong>Set it up</strong></a> · <a href="#how-it-works"><strong>How it works</strong></a> · <a href="#the-decision-model-highly-recommended">Decision model</a> · <a href="#what-it-does">Features</a> · <a href="#privacy-and-safety">Privacy</a> · <a href="#built-by-heyritoai">About</a>
  </p>
</div>

---

> **Quick start:** `hermes plugins install jjdmev2/proactive-hermes --enable`, then open any Hermes chat and type **`/proactive setup`**. Add an OpenAI API key for the [decision model](#the-decision-model-highly-recommended).

## Why Proactive Hermes?

Most "proactive" assistants send alerts you configured in advance. The messages worth getting are about things you never set up an alert for: the client call that now overlaps your dentist, the invoice due today, the reply you promised yesterday and never sent.

Proactive Hermes runs a small desk inside your own Hermes. Scheduled jobs read your mail, calendar, Slack and Linear. A gate decides what reaches you, and the rest goes to a private log. Most runs end with no message at all.

When it does write, it brings the work with it. Before asking you to answer Alex, it reads the thread, finds the numbers Alex asked for and drafts the reply. You get one line: "Alex: the pricing questions are in, your reply is 2 days late. Draft ready, reply 1."

## Install

You need Hermes Agent **0.23.4 or newer**, with the desktop app or the gateway running so messages can reach your chat.

```bash
hermes plugins install jjdmev2/proactive-hermes --enable
```

Recommended extras:

| Extra | Why | How |
| :--- | :--- | :--- |
| **OpenAI API key** (highly recommended) | Powers the [decision model](#the-decision-model-highly-recommended): drops noise before any agent runs and spots your promises. About 3 cents per 1,000 emails. | Add `OPENAI_API_KEY=...` to your Hermes `.env` and put a few dollars of credit on [platform.openai.com](https://platform.openai.com). |
| **Google Workspace skill** | Gmail and Google Calendar. | Set up the `google-workspace` skill in Hermes. |
| **Slack and Linear** | Slack sweep, promise tracking in Slack, work recap. | Connect them in Hermes (Nous Portal connectors). |
| **holidays** | Skip public holidays. | `pip install holidays` |

## Set it up

Setup is a short chat. Two ways to start it:

| Where | How |
| :--- | :--- |
| **In a chat** (recommended) | Open any Hermes chat and type `/proactive setup`. If you just start chatting after installing, Hermes offers it once by itself. |
| **In a terminal** | `hermes proactive setup` |

First you pick a starting point:

| Option | What you get |
| :--- | :--- |
| **1. Juan's setup** | The defaults from the original install: morning brief 08:00, mail radar every 15 minutes, Slack sweep hourly on workdays, promise tracker 3 times a day, work recap after the daily call, evening close-out 21:30, weekly review Friday 15:00, quiet hours 23:00 to 07:00, drafts first. Built for the decision model. You only fill in your own details. |
| **2. My own** | Start blank. Hermes asks about your day and builds the desk around it. |

Hermes does not ask what it already knows. It starts with one message listing what it found (your name from its memory, your timezone, connected mail, calendar, Slack, Linear and the OpenAI key) and asks if that is right. Then it asks only what is missing, one question at a time, each with a short reason: quiet hours, holiday countries, which channels to watch, when your team's daily call happens.

You see every job and its times before anything is created. Run setup again any time; it updates the same jobs.

| Command | What it does |
| :--- | :--- |
| `hermes proactive setup` | Set up or change your answers |
| `hermes proactive setup --dry-run` | Show the jobs, change nothing |
| `hermes proactive status` | Your settings and the jobs the plugin owns |
| `hermes proactive remove` | Remove every job the plugin created (settings and history stay) |

## How it works

```
 mail · calendar · Slack · Linear · past chats
                    │  read-only
     1. scheduled jobs wake up (Hermes cron)
                    │
     2. decision model filters (OpenAI, ~0.3 s)
                    │  only what may need you
     3. desk agent reads and picks candidates
                    │
     4. resolve: search sources, draft, cite
                    │
     5. gate: dedupe, rank, cap, quiet hours
                    │  one short numbered message
          your Hermes chat  ──►  you reply "1"
                    │
     6. draft shown, sent only after your yes
```

1. **Wake up.** Each job runs on its schedule. A small script runs first and checks whether anything changed since last time (new mail, call notes posted). Nothing new means no agent run.
2. **Filter.** With an OpenAI key, the decision model scores each new mail by sender and subject. Clear noise is dropped and logged, so the agent never spends time on it.
3. **Read and pick.** The desk agent reads what is left and writes candidate items: a decision you must make, an action you must take, or a digest line. Anything else is left out.
4. **Resolve.** If an item needs you to send, reply, intro, book or decide, the agent searches mail, Slack, past chats and its own records first. It stores a draft with a source for every fact.
5. **Gate.** The gate ranks the items, removes anything you heard recently, caps the message at 3 items (5 in a brief) and holds non-urgent items during quiet hours. It sends one short numbered message as a normal chat reply.
6. **You answer.** Reply "1" and Hermes shows the draft with its sources and asks "Send as is?". Nothing goes out without that yes.

The plugin installs plain Python scripts under `HERMES_HOME/scripts/proactive-hermes/` and ordinary Hermes cron jobs named `Proactive · ...`. Your settings and history stay in `HERMES_HOME/plugin-data/proactive-hermes/`.

## The decision model (highly recommended)

Juan's setup is built around OpenAI's **Decisions API** with `gpt-6-luna`. It does not write text. You ask it a question with a fixed kind of answer and it returns a probability, a choice or a score in about 0.3 seconds. Input costs $0.10 per million tokens and output is free, so 1,000 emails cost about 3 cents.

**How it ranks.** Every check is a yes or no statement, and the model returns how likely it is to be true, from 0 to 1. The desk acts only above fixed thresholds:

| Check | Statement the model scores | Action |
| :--- | :--- | :--- |
| Mail filter | "This message needs the owner to act or decide soon." It reads the sender, the subject and the first 600 characters of the body. | Below **0.10**: dropped and logged. Everything else goes to the agent. Your own mail, replies in a thread and calendar responses are never dropped. |
| Promise spotting | "The writer commits to a specific action for a specific person." | **0.6** or more: recorded as a promise. |
| Done check | "The later message does what was promised." | **0.8** or more **and** the message as evidence: promise closed. |
| Same matter | "Both texts are about the same matter." | **0.7** or more: merged, so you are not told twice. |

Real scores from testing:

| Input | Score |
| :--- | :--- |
| "Black Friday 40% off" from a shop newsletter | 0.02, dropped |
| "You appeared in 12 searches" from LinkedIn | 0.02, dropped |
| "Re: proposal, need your A or B by tomorrow" from a client | 0.98, kept |
| "Invoice overdue, service suspends in 3 days" | 0.96, kept |
| "New sign-in to your account" from a bank | 0.90, kept |
| "I'll send you the deck and two dates by Friday." | 1.00, a promise |
| "Great meeting you, let's catch up sometime." | 0.15, not a promise |

On one real inbox, 24 of 38 recent emails were promotions (sportswear, supermarket, food delivery, news digests). The filter dropped all 24 and kept all 14 that mattered.

The 0.10 cut-off is low on purpose: the filter only removes mail it is close to certain about. Reading the start of the body catches asks hidden behind a vague subject ("Re: hi"). The mail stays unread in your inbox.

**After the filter, the gate ranks items in this order:** urgent first, then priority (p0, p1, p2), then items where someone is waiting on you, then the nearest deadline, then decisions before actions, then the oldest.

**Without a key** the desk still works. The agent reads every mail that passes the keyword filter and makes the yes or no calls itself, which is slower and costs more model time. If OpenAI is unreachable, nothing is dropped.

## What it does

| Feature | What you get |
| :--- | :--- |
| **One chat, real messages** | Short numbered replies in your Hermes chat, action first. No job cards, no extra push notifications. |
| **Quiet hours and holidays** | Nothing non-urgent at night. Public holidays in the countries you pick are skipped. Only urgent items (money due today, a security alert, travel today) come through at night. |
| **Drafts before questions** | For anything you must send, reply to, book or decide, it searches first and brings a draft with sources. |
| **Promise tracker** | Records each "I'll send it tomorrow" from your mail, Slack and chat with your exact words, and closes it only on proof. |
| **Work recap after the daily** | Reads your team's call notes in Slack and checks each point against Linear and Slack: done, not done, Linear and Slack disagreeing, and today's focus. An evening wrap-up covers what shipped and what slipped. |
| **Mail radar** | Checks new mail every 15 minutes and surfaces only what you must answer, pay or decide. |
| **Briefs and close-out** | Morning brief, an evening close-out you answer by number ("1 yes, 2 tomorrow"), and a weekly review. |
| **Private calendar blocks** | Optional focus blocks on your own calendar, a few per day at most, never touching events it did not create. |
| **Health checks** | A watchdog checks every job, the message outbox and failed deliveries, and only tells you when something needs you. |

## Privacy and safety

- **Read-only.** The desk never sends mail or Slack messages, posts, books, buys or pays. A draft goes out only after you say yes to that exact text, in chat.
- **Local.** Settings, drafts, promises and logs stay in your Hermes home. We run no server and collect no telemetry.
- **The decision model sees little:** for mail, the sender, subject and first 600 characters of the body; for promise checks, the one message being checked. OpenAI offers zero data retention for eligible accounts.
- **Work and personal stay apart.** A work draft never uses facts from personal sources, and every fact carries a source.
- **Bounded.** Searches have call and time limits. Reminders and calendar blocks have daily caps.
- **Text from mail, Slack or the web is treated as data.** Instructions inside it are never followed.

## Project guide

| Path | Contents |
| :--- | :--- |
| [`skills/proactive-desk/references/desk-contract.md`](skills/proactive-desk/references/desk-contract.md) | The rules every desk job follows |
| [`skills/proactive-setup/SKILL.md`](skills/proactive-setup/SKILL.md) | The setup interview |
| [`skills/proactive-desk/SKILL.md`](skills/proactive-desk/SKILL.md) | Answering desk messages, drafts and close-outs in chat |
| [`presets/juan.json`](presets/juan.json) | Juan's setup: choices only, no personal data |
| [`jobs.py`](jobs.py) | Job templates and the installer |
| [`runtime/`](runtime) | The desk: sender, gate, resolver, decision model, promises, health |
| [`tests/`](tests) | Tests with no network access: `python3 -m unittest discover -s tests -t .` |

## Built by HeyRito.AI

Proactive Hermes started as the desk behind our own work at **[HeyRito.AI](https://heyrito.ai)**, and we still use it every day. It follows the rules we build everything on: it stays quiet unless something needs you, reads but never acts without a yes, and every claim points back to its source.

Those are the rules **Rito** enforces for companies. **Rito is the security and governance harness for AI at work.** Teams use AI assistants and agents on real work, and Rito controls what each one can see and do: access scoped to each identity, approval before actions, an audit trail, and policies for every model and tool.

If your team is putting AI to work and needs it safe and governed, talk to us at **[heyrito.ai](https://heyrito.ai)**.

---

<div align="center">
  <strong>Proactive Hermes</strong><br />
  Built by <a href="https://heyrito.ai">HeyRito.AI</a> · Rito, the security and governance harness for AI at work.
</div>
