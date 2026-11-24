---
name: proactive-setup
description: Use when setting up Proactive Hermes. Interview, then apply.
version: 1.0.0
author: HeyRito.AI
license: MIT
metadata:
  hermes:
    tags: [proactive, setup, executive-assistant]
---

# Proactive Hermes setup

You are setting up the proactive desk for the person you are chatting with. Keep it a friendly chat, one question at a time, short.

## 1. Pick the starting point

Ask exactly this, as two choices:

1. **Juan's setup**: the proven defaults. Morning brief 08:00, mail radar every 15 min, Slack sweep hourly on workdays, promise tracker 3 times a day, work recap after the daily call, evening close-out 21:30, weekly review Friday 15:00, quiet hours 23 to 7, drafts first. You only fill in your own details.
2. **My own**: start blank and choose everything.

Both paths use the same questions below; with Juan's setup, offer the preset value as the default and accept "ok" to keep it.

## 2. Say what you already know, then ask only what is missing

Never ask what Hermes already knows. Before any question:

```
hermes proactive questions
```

Its `known` block has what the system found (name, timezone, mail accounts, Google Workspace, OpenAI key). Add what you know yourself: the user's name and preferences from your memory and user profile, connected Slack or Linear (tool_search "slack search messages", "linear issues"), work emails from their mail accounts. Send it as ONE message, for example:

> "Here is what I have: Alex, Europe/Berlin, Gmail and Calendar connected, Slack connected, OpenAI key found. All correct?"

Then ask only the questions in `questions` that are still open, one at a time, skipping ones that no longer apply (no Slack means no channels and no daily call). Give each a short reason, for example "Quiet hours? (so nothing buzzes you at night)".

For "My own", also ask:
- When should the morning brief arrive, and the evening close-out?
- How often to check mail (every 15, 30 or 60 minutes)?
- Track promises you make in mail and Slack? Weekly review on which day?

If no OpenAI key was found, say once: "Juan's setup is built for OpenAI's decision model. With a key and a few dollars of credit it filters noise and spots promises for about 3 cents per 1,000 emails. Want to add one now, or go without?"

## 3. Apply

Write the answers to a JSON file in the scratch dir, using the question keys (for example `"owner.name"`, `"timezone"`, `"quiet_hours": "22-7"`, `"slack.channels": "#team,#product"`). Extra choices from "My own" go in as nested keys, for example `{"schedule": {"morning_brief": "30 7 * * 1-5"}, "features": {"weekly_review": false}}`. Then run:

```
hermes proactive setup --preset juan|custom --answers <file> --dry-run
```

Show the job list in plain words (what arrives when). On their OK run the same command without `--dry-run`, then `hermes proactive status`.

## If `hermes proactive` is not found

Under `plugins.isolation: host` the CLI command is not registered. Use the same commands through the plugin file: `python3 <HERMES_HOME>/plugins/proactive-hermes/cli.py questions|setup|status|remove ...` with the same flags.

## Rules

- Never ask for passwords, tokens or card numbers. Accounts are connected through Hermes itself (google-workspace skill, Nous Portal connectors, himalaya).
- The desk is read-only: it never sends, posts, books or pays. Say this once.
- Nothing personal goes into the plugin folder. Config lives in `plugin-data/proactive-hermes/config.json` on their machine only.
