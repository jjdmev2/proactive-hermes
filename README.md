<div align="center">
  <a href="https://heyrito.ai"><img src="docs/assets/rito-logo.png" alt="Rito" width="112" height="112" /></a>
  <h1>Proactive Hermes</h1>
  <p><strong>It watches your mail, calendar, work tools and the things you care about, and acts before you have to ask.</strong></p>
  <p>A proactive executive-assistant desk for <a href="https://hermes-agent.nousresearch.com">Hermes Agent</a>.<br />Built by <a href="https://heyrito.ai">HeyRito.AI</a>.</p>
  <p>
    <img src="https://img.shields.io/badge/hermes-%3E%3D0.23.4-0f766e" alt="Hermes 0.23.4 or newer" />
    <img src="https://img.shields.io/badge/type-plugin-334155" alt="Hermes plugin" />
    <img src="https://img.shields.io/badge/actions-approval--first-0f766e" alt="Approval-first actions" />
    <img src="https://img.shields.io/badge/decisions-OpenAI%20gpt--6--luna-0f766e" alt="OpenAI Decisions API" />
    <img src="https://img.shields.io/badge/status-beta-d97706" alt="Beta" />
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-8b5cf6" alt="MIT license" /></a>
  </p>
  <p>
    <a href="#install"><strong>Install</strong></a> · <a href="#set-it-up"><strong>Set it up</strong></a> · <a href="#how-it-works"><strong>How it works</strong></a> · <a href="#real-examples">Real examples</a> · <a href="#the-decision-model-highly-recommended">Decision model</a> · <a href="#what-it-does">Features</a> · <a href="#privacy-and-safety">Privacy</a> · <a href="#built-by-heyritoai">About</a>
  </p>
</div>

---

> **Quick start:** turn on Bot Mode in the Hermes desktop app (on by default), run `hermes plugins install jjdmev2/proactive-hermes --enable`, then open your Bot Chat and type **`/proactive setup`**. Add an OpenAI API key for the [decision model](#the-decision-model-highly-recommended).

## Why Proactive Hermes?

Most "proactive" assistants send alerts you configured in advance. The messages worth getting are about things you never set up an alert for: the client call that now overlaps your dentist, the invoice due today, the reply you promised yesterday and never sent.

Proactive Hermes runs a small desk inside your own Hermes. Scheduled jobs read your mail, calendar, Slack and Linear, and watches keep an eye on the shops, marketplaces and event pages you choose. A gate decides what reaches you, and the rest goes to a private log. Most runs end with no message at all.

When it does write, it brings the work with it. Small things are already done (the calendar block is in, the reminder is set), and the rest is prepared so a "yes" finishes it:

> "Train to Tampere Fri 07:12: blocked, leave at 06:35 (10 min buffer)."
> "Alex: the pricing questions are in, your reply is 2 days late. Draft ready, reply 1."
> "Buyer offers 356 for the graphics card (listed 400). Counter at 380? Reply 1."

## Real examples

These all happened on our own desk.

| Situation | What Hermes did |
| :--- | :--- |
| **A train ticket home** | The booking mail arrived. Hermes blocked the trip on the calendar and sent the time to leave, based on your phone's location, with a 10 minute buffer (Hermes mobile app with location access required). |
| **A conference spot opened** | A watch was checking the registration page every few minutes. When a seat freed up it set a phone alarm that rings through Focus, so it could not be missed, and had the registration ready. |
| **A buyer made an offer** | A marketplace offer came in below the asking price. Hermes reported it with a suggested counter, and on "yes" answered the buyer in the app. |
| **A promise to a partner** | The owner had promised a list of questions "over the weekend". A teammate posted the list in Slack on Monday. Hermes connected the two, drafted the email in the right thread, and sent it after "yes". |
| **A meeting moved** | A client proposed a new time. Hermes checked the calendar, said it was free, and accepted on "yes". |
| **A bill about to cut service** | An overdue notice said service stops in 3 days. Hermes set a reminder for the next morning with the amount and due date. |
| **A daily call** | After the team's daily, Hermes read the call notes in Slack, checked each point against Linear, and sent the owner what was done, what was not, and today's focus. Nothing was posted in Slack. |

Things you can set up the same way: a new card set or sneaker drop ("alarm me when preorders open"), concert or Luma event registration ("tell me the minute it opens, with the form filled"), stock at your local hardware store or on Amazon, replies to offers on Vinted, eBay or Marketplace, and "suggest a reply to every mail from X".

## Install

### Before you install

| Requirement | Why | How |
| :--- | :--- | :--- |
| **Hermes Agent 0.23.4 or newer** | The plugin uses Bot Mode delivery and the cron scheduler. | `hermes update` |
| **Bot Mode** | Every desk message is a real reply in your Bot's **Bot Chat**, the one permanent conversation each Bot has. It arrives like a text from a person, with the app's normal notification and nothing extra. | Bot Mode is on by default in the Hermes desktop app. Open the **Bots** tab and click your Bot once so its Bot Chat exists. You read it in the desktop app or the Hermes mobile app. |
| **The gateway running** | Messages are delivered through your profile's running gateway. If it is off, nothing reaches you. | Keep the Hermes desktop app open, or run `hermes gateway start`. |
| **The right profile** | The desk talks in the Bot Chat of the profile (Bot) you install it into. | Use your main assistant Bot. If that is not your default profile, add `-p <profile name>` to every command below, for example `hermes -p assistant plugins install ...`. `hermes profile list` shows the names. |
| **Hermes mobile app** (highly recommended) | Desk messages buzz your phone like a text; reminders, alarms and leave times need it. Tested on iPhone. | See [Give Hermes access](#give-hermes-access). The plugin works without it, and `doctor` tells you it is missing. |

### Install

```bash
hermes plugins install jjdmev2/proactive-hermes --enable
hermes proactive doctor
```

While the repository is private, ask us for access first. `hermes proactive doctor` checks the gateway, the Bot Chat, the phone, the OpenAI key and your mail, and tells you how to fix anything missing. `hermes proactive doctor --send-test` posts one test message into your Bot Chat, so you can see it land on your phone.

### Give Hermes access

The desk only sees what you connect. Everything here is optional except the gateway and Bot Mode above.

| Access | What it unlocks | How |
| :--- | :--- | :--- |
| **Hermes mobile app** (highly recommended) | Reminders and alarms on your phone, leave times based on where you are, your phone calendar and contacts. | Install the Hermes app, sign in to the same Hermes, and share **Reminders, Alarms, Location, Calendar and Contacts** in its settings. |
| **OpenAI API key** (highly recommended) | The [decision model](#the-decision-model-highly-recommended): drops noise before any agent runs and spots your promises. About 3 cents per 1,000 emails. | Add `OPENAI_API_KEY=...` to your Hermes `.env` and put a few dollars of credit on [platform.openai.com](https://platform.openai.com). |
| **Mail and calendar** | Mail radar, promise tracker, trips, calendar blocks and conflicts. | The `google-workspace` skill for Gmail and Google Calendar; [himalaya](https://github.com/pimalaya/himalaya) for any other mailbox (IMAP). |
| **Slack and Linear** | Asks waiting on you, the work recap, promises made in Slack. | Hermes connectors (Nous Portal). |
| **Your apps and sites** | Watches on marketplaces (Vinted, eBay), shops (Amazon, your local hardware store), event pages (Luma, Eventbrite, conferences), ticket drops. | Sign in once in Hermes's browser (the browser tool; with `local` it uses a copy of your own Chrome profile). Never paste passwords into chat. |
| **Public holidays** | Skip holidays in the countries you pick. | `pip install holidays` |

## Set it up

Setup is a short chat in your Bot Chat. Two ways to start it:

| Where | How |
| :--- | :--- |
| **In your Bot Chat** (recommended) | Type `/proactive setup`. If you just start chatting after installing, Hermes offers it once by itself. |
| **In a terminal** | `hermes proactive setup` |

First you pick a starting point:

| Option | What you get |
| :--- | :--- |
| **1. Juan's setup** | The defaults Juan Oliva (who built this at HeyRito.AI) runs every day: morning brief 08:00, mail radar every 15 minutes, Slack sweep hourly on workdays, promise tracker 3 times a day, work recap after the daily call, evening close-out 21:30, weekly review Friday 15:00, quiet hours 23:00 to 07:00, drafts before questions. Built for the decision model. You only fill in your own details. |
| **2. My own** | Start blank. Hermes asks about your day and builds the desk around it. |

Hermes does not ask what it already knows. It starts with one message listing what it found (your name from its memory, your timezone, connected mail, calendar, Slack, Linear and the OpenAI key) and asks if that is right. Then it asks only what is missing, one question at a time, each with a short reason: quiet hours, holiday countries, which channels to watch, when your team's daily call happens.

Setup also asks what Hermes may do without asking you (see [approval levels](#approval-levels)), whether you use the Hermes mobile app, and offers two or three first watches from your life.

You see every job and its times before anything is created. At the end Hermes posts a test message and asks if it reached your phone. Run setup again any time; it updates the same jobs.

### Approval levels

| Level | Juan's setup | You can change it to |
| :--- | :--- | :--- |
| **Without asking** | Private calendar blocks, phone reminders, alarms for urgent things, leave times for trips. | Any of these, or none. Watch actions too, if you want a watch to act on its own. |
| **Ask first** | Sending email or Slack, replying to offers, registering for events, purchases and bookings (Hermes fills the form or cart, you confirm and pay), accepting invites. | Everything not allowed above lands here. Hermes prepares it fully and asks in one line; a "yes" approves that one action. |
| **Never** | Payments, card details, passwords, deleting data. | These always stay with you. |

### Add your own watches

Tell Hermes in chat ("watch the Luma page for the AI meetup and tell me when registration opens") and it sets one up, or add it yourself:

```bash
hermes proactive watch add --name "Card drop" --schedule "*/15 * * * *" \
  --check "the shop page for the new card set" --when "preorders open" --then alarm --urgent
```

`--then` is what happens when the condition is true: `tell` (a line in chat), `reminder`, `alarm` (urgent only), `draft` (prepared and waiting for your yes) or `act` (done on its own, only if you allowed watch actions; a watch never pays or enters card details, and purchases still wait for your yes). `hermes proactive watch list` and `watch remove --name` manage them.

| Command | What it does |
| :--- | :--- |
| `hermes proactive setup` | Set up or change your answers |
| `hermes proactive setup --dry-run` | Show the jobs, change nothing |
| `hermes proactive status` | Your settings and the jobs the plugin owns |
| `hermes proactive doctor [--send-test]` | Check delivery to your Bot Chat and phone |
| `hermes proactive watch add ...` | Add a watch (see above) |
| `hermes proactive remove` | Remove every job the plugin created (settings and history stay) |

To uninstall completely: `hermes proactive remove`, then `hermes plugins remove proactive-hermes`, then delete `HERMES_HOME/plugin-data/proactive-hermes/` and `HERMES_HOME/scripts/proactive-hermes/`.

## How it works

```
 mail · calendar · Slack · Linear · watches · phone
                    │
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
   your Bot Chat (and phone)  ──►  you reply "1"
                    │
     6. small things done, the rest after your yes
```

1. **Wake up.** Each job runs on its schedule. A small script runs first and checks whether anything changed since last time (new mail, call notes posted). Nothing new means no agent run.
2. **Filter.** With an OpenAI key, the decision model scores each new mail from its sender, subject and the start of the body. Clear noise is dropped and logged, so the agent never spends time on it.
3. **Read and pick.** The desk agent reads what is left and writes candidate items: a decision you must make, an action you must take, or a digest line. Anything else is left out.
4. **Resolve.** If an item needs you to send, reply, intro, book or decide, the agent searches mail, Slack, past chats and its own records first. It stores a draft with a source for every fact.
5. **Gate.** The gate ranks the items, removes anything you heard recently, caps the message at 3 items (5 in a brief) and holds non-urgent items during quiet hours. It sends one short numbered message as a normal chat reply.
6. **Act.** What your approval levels allow happens right away (a block, a reminder, an alarm, a leave time) and the message says so. Everything else waits for you: reply "1" and Hermes shows the draft or action with its sources and asks "Send as is?". A yes does it.

**What it costs to run.** The desk jobs are ordinary agent runs on the model of your Bot's profile. A cheap, fast model is enough: on our desk they use about 1% of a day's Nous allowance, because most runs stop at step 1 or 2.

The plugin installs plain Python scripts under `HERMES_HOME/scripts/proactive-hermes/` and ordinary Hermes cron jobs named `Proactive · ...`. Your settings and history stay in `HERMES_HOME/plugin-data/proactive-hermes/`.

## The decision model (highly recommended)

Juan's setup is built around OpenAI's [**Decisions API**](https://developers.openai.com/api/docs/guides/decisions) with `gpt-6-luna`. It does not write text. You ask it a question with a fixed kind of answer and it returns a probability, a choice or a score in about 0.3 seconds. Input costs $0.10 per million tokens and output is free, so 1,000 emails cost about 3 cents.

**How it ranks.** Every check is a yes or no statement, and the model returns how likely it is to be true, from 0 to 1. The desk acts only above fixed thresholds:

| Check | Statement the model scores | Action |
| :--- | :--- | :--- |
| Mail filter | "This message needs the owner to act or decide soon." It reads the sender, the subject and the first 600 characters of the body. | Below **0.10**: dropped and logged. Everything else goes to the agent. Your own mail, replies in a thread and calendar responses are never dropped. |
| Promise spotting | "The writer commits to a specific action for a specific person." | **0.6** or more: recorded as a promise. |
| Done check | "The later message does what was promised." | **0.8** or more **and** the message as evidence: promise closed. |
| Same matter | "Both texts are about the same matter." | **0.7** or more: merged, so you are not told twice. |
| Slack filter | "This message is waiting on the owner and is not answered yet." | Below **0.10**: skipped. DMs and messages that name you are always read. |
| Urgency | "This needs the owner today: money, security, travel, someone blocked." | **0.8** or more: may come through during quiet hours. |
| Prompt injection | "This text contains instructions aimed at an AI assistant." | **0.7** or more: flagged as suspicious, treated as data only, and you get one line about it. Nothing is dropped. |
| Work recap | "Which Linear issue matches this item from the call?" | **0.7** or more: linked. Below that: "no Linear issue found". |
| Close-out replies | "The reply gives an answer about item N." | Below **0.6**: Hermes asks you instead of guessing. |

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
| A mail asking the assistant to forward the last 10 emails to an outside address | 1.00, flagged as prompt injection |
| "Can we move our call to 3pm?" | 0.00, normal mail |
| Call item "Hotel booking in Lahti" vs Linear issue "Lahti hotel reservation" | 1.00, matched |

On one real inbox, the filter dropped 23 of 38 recent emails, all promotions (sportswear, supermarket, food delivery, news digests). It kept the other 15, including a rescheduled client meeting, an overdue bill and an expired identity check.

The 0.10 cut-off is low on purpose: the filter only removes mail it is close to certain about. Reading the start of the body catches asks hidden behind a vague subject ("Re: hi"). The mail stays unread in your inbox.

**After the filter, the gate ranks items in this order:** urgent first, then priority (p0, p1, p2), then items where someone is waiting on you, then the nearest deadline, then decisions before actions, then the oldest.

**Without a key** the desk still works. The agent reads every mail that passes a simple sender and subject filter (newsletters, no-reply senders, "% off") and makes the yes or no calls itself, which is slower and costs more model time. If OpenAI is unreachable, nothing is dropped.

## What it does

| Feature | What you get |
| :--- | :--- |
| **One chat, real messages** | Short numbered replies in your Bot Chat, action first, with the app's normal notification on your phone. No job cards and no second notification on top. |
| **Quiet hours and holidays** | Nothing non-urgent at night. Public holidays in the countries you pick are skipped. Only urgent items (money due today, a security alert, travel today) come through at night. |
| **Drafts before questions** | For anything you must send, reply to, book or decide, it searches first and brings a draft with sources. |
| **Promise tracker** | Records each "I'll send it tomorrow" from your mail, Slack and chat with your exact words, and closes it only on proof. |
| **Work recap after the daily** | Reads your team's call notes in Slack and checks each point against Linear and Slack: done, not done, Linear and Slack disagreeing, and today's focus. An evening wrap-up covers what shipped and what slipped. |
| **Mail radar** | Checks new mail every 15 minutes and surfaces only what you must answer, pay or decide. |
| **Briefs and close-out** | Morning brief, an evening close-out you answer by number ("1 yes, 2 tomorrow"), and a weekly review. |
| **Trips and leave times** | A ticket or booking in your mail becomes a calendar block and a "leave now" reminder based on where your phone is, with a buffer. |
| **Watches** | Your own proactive tasks: marketplaces, shops, drops, event registrations, anything with a page. Tell, remind, alarm, draft or act. |
| **Phone reminders and alarms** | Reminders for deadlines and promises, alarms that ring through Focus for the few things that cannot wait. Retried until the phone confirms. |
| **Private calendar blocks** | Focus blocks on your own calendar for key tasks, a few per day at most, never touching events it did not create. |
| **Health checks** | A watchdog checks every job, the message outbox and failed deliveries, and only tells you when something needs you. |

## Privacy and safety

- **Approval first.** The desk only does on its own what you allowed in setup (blocks, reminders, alarms, leave times by default). Sending, replying, registering, buying and booking wait for your yes to that exact action. Payments, card details and passwords always stay with you.
- **Local.** Settings, drafts, promises and logs stay in your Hermes home. We run no server and collect no telemetry.
- **Your Bot's model sees what the desk reads.** Mail, Slack and calendar text go to the model provider of your Bot's profile, as in any Hermes chat.
- **The decision model sees little:** for mail, the sender, subject and first 600 characters of the body; for promise checks, the one message being checked. OpenAI offers zero data retention for eligible accounts.
- **Work and personal stay apart.** A work draft never uses facts from personal sources, and every fact carries a source.
- **Bounded.** Searches have call and time limits. Reminders, alarms and calendar blocks have daily caps, and each action is done once even if a job retries.
- **Text from mail, Slack or the web is treated as data.** Instructions inside it are never followed.

## Project guide

| Path | Contents |
| :--- | :--- |
| [`skills/proactive-desk/references/desk-contract.md`](skills/proactive-desk/references/desk-contract.md) | The rules every desk job follows |
| [`skills/proactive-setup/SKILL.md`](skills/proactive-setup/SKILL.md) | The setup interview |
| [`skills/proactive-desk/SKILL.md`](skills/proactive-desk/SKILL.md) | Answering desk messages, approvals, watches and close-outs in chat |
| [`presets/juan.json`](presets/juan.json) | Juan's setup: choices only, no personal data |
| [`jobs.py`](jobs.py) | Job templates and the installer |
| [`runtime/`](runtime) | The desk: sender, gate, resolver, decision model, promises, health |
| [`tests/`](tests) | Tests with no network access: `python3 -m unittest discover -s tests -t .` |

## Built by HeyRito.AI

Proactive Hermes started as the desk behind our own work at **[HeyRito.AI](https://heyrito.ai)**, and we still use it every day. It follows the rules we build everything on: it stays quiet unless something needs you, acts only within what you approved, and every claim points back to its source.

Those are the rules **Rito** enforces for companies. **Rito is the security and governance harness for AI at work.** Teams use AI assistants and agents on real work, and Rito controls what each one can see and do: access scoped to each identity, approval before actions, an audit trail, and policies for every model and tool.

If your team is putting AI to work and needs it safe and governed, talk to us at **[heyrito.ai](https://heyrito.ai)**.

---

<div align="center">
  <strong>Proactive Hermes</strong><br />
  Built by <a href="https://heyrito.ai">HeyRito.AI</a> · Rito, the security and governance harness for AI at work.
</div>
