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

## 0. Check delivery first

Every desk message is a real reply in this profile's **Bot Chat** (Hermes Bot Mode, on by default in the desktop app), delivered through this profile's running gateway. Run:

```
hermes proactive doctor
```

If `gateway` is not ok, stop and help them fix it first (open the Hermes desktop app or `hermes gateway start`); without it nothing reaches them. If `mobile` is not ok, strongly recommend the Hermes mobile app: that is where the notifications come from. Install the plugin in the profile whose Bot Chat they want the desk to talk in (their main assistant Bot).

## 1. Pick the starting point

Ask exactly this, as two choices:

1. **Juan's setup**: the proven defaults. Morning brief 08:00, mail radar every 15 min, Slack sweep hourly on workdays, promise tracker 3 times a day, work recap after the daily call, evening close-out 21:30, weekly review Friday 15:00, quiet hours 23 to 7, drafts first. You only fill in your own details.
2. **My own**: start blank and choose everything.

Both paths include meeting prep: one short prep line about 15 to 75 minutes before a meeting with other people, and "prep tonight" items in the evening brief for tomorrow's meetings; it is on whenever a calendar is connected (off with `{"features": {"meeting_prep": false}}`).

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

## 3. What it may do, the phone, and first watches

Ask these three in plain words, after the details:

1. **Approval levels.** "Which of these may I do without asking: private calendar blocks, reminders, alarms for urgent things, leave-time planning for trips? Everything else I prepare and ask first. Payments and passwords always stay with you." Juan's setup allows all four.
2. **The Hermes mobile app.** Recommend it strongly: reminders, alarms, location (for leave times) and calendar make the desk useful on the go. Ask them to install it, sign in to the same Hermes, and share Reminders, Alarms, Location and Calendar in its settings. Check with the mobile device tools if they are available.
3. **First watches.** Offer two or three from their life, for example:
   - selling apps (Vinted, eBay, Marketplace): "tell me when a buyer makes an offer, with a suggested reply"
   - a store or drop (a local hardware store, Amazon, a card or sneaker release): "alarm me when it is in stock or preorders open"
   - events (Luma, Eventbrite, a conference, a concert): "tell me when registration opens, draft the form"
   - work tools: "remind me when a PR waits on me for more than a day"
   For accounts the watch needs (a shop login, Luma, a marketplace), tell them to sign in once in the browser Hermes uses, never to share passwords in chat.

## 4. Apply

Write the answers to a JSON file in the scratch dir, using the question keys (for example `"owner.name"`, `"timezone"`, `"quiet_hours": "22-7"`, `"slack.channels": "#team,#product"`). Extra choices from "My own" go in as nested keys, for example `{"schedule": {"morning_brief": "30 7 * * 1-5"}, "features": {"weekly_review": false}}`. Then run:

```
hermes proactive setup --preset juan|custom --answers <file> --dry-run
```

Show the job list in plain words (what arrives when). On their OK run the same command without `--dry-run`, then `hermes proactive status`, then `hermes proactive doctor --send-test` and ask: "Did the test message show up here and on your phone?" If not, walk through the doctor's fixes.

## If `hermes proactive` is not found

Under `plugins.isolation: host` the CLI command is not registered. Use the same commands through the plugin file: `python3 <HERMES_HOME>/plugins/proactive-hermes/cli.py questions|setup|status|remove ...` with the same flags.

## Rules

- Never ask for passwords, tokens or card numbers. Accounts are connected through Hermes itself (google-workspace skill, Nous Portal connectors, himalaya).
- Say once how approval works: what they allowed happens on its own, everything else waits for their yes, payments and passwords always stay with them.
- Nothing personal goes into the plugin folder. Config lives in `plugin-data/proactive-hermes/config.json` on their machine only.
