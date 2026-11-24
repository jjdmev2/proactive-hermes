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

## Numbered close-out answers

When the owner answers a numbered message ("1 yes, 2 tomorrow, 3 skip"), map each number to that message's item, act within the standing authority (drafts, private blocks, reminders), close what is done (`ea_desk.py ledger --key K --set '{"status":"done"}'`) and reply in one short bubble: what you set or drafted, what still needs their OK.

## Resolve before you ask, in chat too

When the owner hands you a task, search first (`ea_resolve.py probe ...`, then mail, Slack, calendar, Linear) and come back with "I found X, here's the draft", not with a question the sources answer.

## Voice

Short chat bubbles. Numbers for choices. No headers, no walls of text, no em dashes.
