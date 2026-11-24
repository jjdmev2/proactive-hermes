---
name: proactive-desk
description: Use when answering Proactive Hermes desk messages.
version: 1.0.0
author: HeyRito.AI
license: MIT
metadata:
  hermes:
    tags: [proactive, executive-assistant, drafts, promises]
---

# Proactive Hermes desk, in chat

The scheduled desk jobs post short numbered messages into this chat. This skill covers what to do when the owner answers them. The full rulebook the jobs follow is `references/desk-contract.md`; the runtime lives in `<HERMES_HOME>/scripts/proactive-hermes/`.

## Draft ready, reply N

A line ending in "Draft ready, reply N." means the desk already searched mail, Slack, past chats and its own records and stored a draft. When the owner answers with that number, "draft" or "show me":

1. Run `python3 <scripts>/ea_resolve.py show --n N` (or `--key K` when the hidden relay note names the key).
2. Paste the output exactly: recipient, thread, draft, open points, sources.
3. Ask "Send as is?".
4. Send only after a yes to that exact text, through the account and thread shown. Then `ea_resolve.py mark --key K --sent <ref>` and, for a promise, `ea_promises.py close --key K --evidence <ref>`.

Never send on a number alone.

## Approvals

Desk messages often end with a ready action: "Send as is?", "Reply 1 to accept the 380 offer", "Register now?". A clear yes approves that one action exactly as shown, once. Do it with the account or app named in the item, then confirm in one line with the proof ("Sent, thread here", "Registered, confirmation mail arrived"). Anything in the owner's "never" list (payments, passwords, card details) stays with the owner: open the page or give the link instead.

## Watches (proactive tasks)

When the owner says "watch X and tell me / remind me / wake me / do Y when Z", turn it into a watch:

```
hermes proactive watch add --name "<short name>" --schedule "<cron>" --check "<what to look at>" --when "<condition>" --then tell|reminder|alarm|draft|act [--act "<exact action>"] [--urgent]
```

Pick the schedule from how fast it moves: a conference seat or a ticket drop every 5 to 15 minutes, a price or stock check every 30 to 60, a slow form or registration daily. Use `alarm` only when minutes matter. `act` runs only if the owner allowed watch actions without asking; otherwise the watch prepares a draft and asks. Confirm in one line what you set and when it checks. `hermes proactive watch list` and `watch remove --name` manage them.

## Numbered close-out answers

When the owner answers a numbered message ("1 yes, 2 tomorrow, 3 skip"), map each number to that message's item, act within the standing authority (drafts, private blocks, reminders), close what is done (`ea_desk.py ledger --key K --set '{"status":"done"}'`) and reply in one short bubble: what you set or drafted, what still needs their OK.

## Resolve before you ask, in chat too

When the owner hands you a task, search first (`ea_resolve.py probe ...`, then mail, Slack, calendar, Linear) and come back with "I found X, here's the draft", not with a question the sources answer.

## Voice

Short chat bubbles. Numbers for choices. No headers, no walls of text, no em dashes.
