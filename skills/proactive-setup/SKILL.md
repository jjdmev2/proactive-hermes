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

## 2. The interview

Get the exact questions and their keys:

```
hermes proactive questions
```

Ask them in that order, skipping ones that no longer apply (no Slack means no channels and no daily call). Check what you can instead of asking: the timezone from the system clock, whether the google-workspace skill is authenticated, whether Slack or Linear connector tools exist (tool_search "slack search messages", "linear issues"). Say what you found and ask them to confirm.

For "My own", also ask:
- When should the morning brief arrive, and the evening close-out?
- How often to check mail (every 15, 30 or 60 minutes)?
- Track promises you make in mail and Slack? Weekly review on which day?

## 3. Apply

Write the answers to a JSON file in the scratch dir, using the question keys (for example `"owner.name"`, `"timezone"`, `"quiet_hours": "22-7"`, `"slack.channels": "#team,#product"`). Extra choices from "My own" go in as nested keys, for example `{"schedule": {"morning_brief": "30 7 * * 1-5"}, "features": {"weekly_review": false}}`. Then run:

```
hermes proactive setup --preset juan|custom --answers <file> --dry-run
```

Show the job list in plain words (what arrives when). On their OK run the same command without `--dry-run`, then `hermes proactive status`.

## Rules

- Never ask for passwords, tokens or card numbers. Accounts are connected through Hermes itself (google-workspace skill, Nous Portal connectors, himalaya).
- The desk is read-only: it never sends, posts, books or pays. Say this once.
- Nothing personal goes into the plugin folder. Config lives in `plugin-data/proactive-hermes/config.json` on their machine only.
