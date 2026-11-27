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

Every desk message is a real reply in this profile's **Bot Chat** (Hermes Bot Mode, on by default in the desktop app). The job "Proactive · say" hands it to the Bot through Hermes's own scheduled Bot Chat delivery, so this profile's gateway must be running. Run:

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

**Instant mail push (optional, after the mailboxes are chosen).** Say it plainly, once: "By default I check your mail every 15 minutes. If you want, a mailbox can wake me within 1 to 3 minutes of new mail. That needs an app password for that mailbox, which you paste into Hermes's settings, never into this chat. Want it for any mailbox?" If yes, per mailbox:

1. Make an app password (a separate password just for Hermes, which they can revoke any time):
   - Gmail: open myaccount.google.com/apppasswords. 2-Step Verification must be on. Work or school accounts, Advanced Protection and security key only accounts may not offer app passwords.
   - iCloud: account.apple.com > Sign-In and Security > App-Specific Passwords. Two-factor authentication must be on.
   - Outlook, Hotmail, Live and Microsoft 365: not possible. Microsoft allows only its own sign-in for mail, so an app password is refused. These mailboxes stay on the 15 minute checks.
   - Fastmail, Yahoo and others: the provider's own app password page. If the provider has none, that mailbox stays on the 15 minute checks.
2. Open **Settings → Plugins → Proactive Hermes** in the Hermes desktop app (or run `hermes plugins config proactive-hermes` in a terminal) and fill **Mailbox N address** and **Mailbox N app password** (N is 1 to 6). If the address does not end in gmail.com, icloud.com, me.com, mac.com, fastmail.com or a yahoo domain, also fill **Mailbox N IMAP server**. For a work Gmail on the company's domain that is imap.gmail.com. For anything else, take it from the provider's help page.
3. After setup is applied, restart Hermes (`hermes gateway restart`, or quit and reopen the desktop app), then run `hermes proactive doctor`. Under `mail_push` each mailbox shows `instant push`, or `15 min checks` with the reason and the next step.

Never ask for, accept or repeat a password in chat. If they paste one anyway, do not use it or repeat it: tell them to revoke that app password now, make a new one, and put the new one only in the settings field. A mailbox they skip simply stays on the 15 minute checks.

If `hermes proactive doctor` does not show the `decisions` check as ok, say once: "Juan's setup is built for OpenAI's decision model. With a key and a few dollars of credit it filters noise and spots promises for about 3 cents per 1,000 emails. Want to add one now, or go without?" If yes: open **Settings → Plugins → Proactive Hermes** and fill **OpenAI key for the decision filter** (or run `hermes plugins config proactive-hermes`). It can be the same key as `OPENAI_API_KEY`, but it must be set there: Hermes hides `OPENAI_API_KEY` from scheduled jobs, so a key that is only in `OPENAI_API_KEY` never reaches the mail radar. Never take the key in chat. Run `hermes proactive doctor` again to confirm.

## 3. What it may do, the phone, and first watches

Ask these three in plain words, after the details:

1. **Approval levels.** "Which of these may I do without asking: private calendar blocks, reminders, alarms for urgent things, leave-time planning for trips, cleanup (removing my own blocks and alerts and ticking off my own reminders once an item is closed)? Everything else I prepare and ask first. Payment approvals and passwords stay with you." Juan's setup allows all five.
2. **The Hermes mobile app.** Recommend it strongly: reminders, alarms, location (for leave times) and calendar make the desk useful on the go. Ask them to install it, sign in to the same Hermes, and share Reminders, Alarms, Location and Calendar in its settings. Check with the mobile device tools if they are available.
3. **First watches.** Offer two or three from their life, for example:
   - selling apps (Vinted, eBay, Marketplace): "tell me when a buyer makes an offer, with a suggested reply"
   - a store or drop (a local hardware store, Amazon, a card or sneaker release): "alarm me when it is in stock or preorders open"
   - events (Luma, Eventbrite, a conference, a concert): "tell me when registration opens, draft the form"
   - work tools: "remind me when a PR waits on me for more than a day"
   For accounts the watch needs (a shop login, Luma, a marketplace), tell them to sign in once in the browser Hermes uses, never to share passwords in chat.

**Phone checklist** (say it once, plainly). Phone reminders and alarms land only when the phone wakes the Hermes app, so urgent items also become calendar alerts the phone fires by itself. For that to work on an iPhone:
- Add the work Google account (the connected calendar) to the iPhone Calendar app (Settings > Apps > Calendar > Calendar Accounts on iOS 18 and later; older iOS: Settings > Calendar > Accounts) and turn on its notifications (Settings > Notifications > Calendar).
- Turn on Background App Refresh for Hermes (Settings > General > Background App Refresh).
- Low Power Mode pauses background refresh, so reminders and alarms from Hermes arrive late while it is on. Calendar alerts still fire.

## 4. Apply

Write the answers to a JSON file in the scratch dir, using the question keys (for example `"owner.name"`, `"timezone"`, `"quiet_hours": "22-7"`, `"slack.channels": "#team,#product"`). Extra choices from "My own" go in as nested keys, for example `{"schedule": {"morning_brief": "30 7 * * 1-5"}, "features": {"weekly_review": false}}`. Approval levels go in as `"authority.auto"`, `"authority.ask_first"` and `"authority.never"` (lists of action names). Each action belongs to one level only; setup refuses an action in two levels, an unknown action and any other dotted key that is not a question key, and says which one, so fix that answer and run it again. Then run:

```
hermes proactive setup --preset juan|custom --answers <file> --dry-run
```

Show the job list in plain words (what arrives when). On their OK run the same command without `--dry-run`, then `hermes proactive status`, then `hermes proactive doctor --send-test` and ask: "Did the test message show up here and on your phone?" If not, walk through the doctor's fixes.

## If `hermes proactive` is not found

Under `plugins.isolation: host` the CLI command is not registered. Use the same commands through the plugin file, with the Python that runs Hermes: `~/.hermes/hermes-agent/venv/bin/python <HERMES_HOME>/plugins/proactive-hermes/cli.py questions|setup|status|remove ...` with the same flags. A plain `python3` cannot load Hermes's cron API, so setup refuses there and changes nothing.

## Rules

- Never ask for passwords, tokens or card numbers, and never accept one in chat. Accounts are connected through Hermes itself (google-workspace skill, Nous Portal connectors, himalaya), and mail push app passwords go only into Settings → Plugins → Proactive Hermes or `hermes plugins config proactive-hermes`.
- Explain approvals once: what they allowed happens on its own; everything else waits for their yes. Payment approvals and passwords stay with them. Scheduled jobs are instructed not to book or pay and have no booking or payment authority by default.
- Nothing personal goes into the plugin folder. Config lives in `plugin-data/proactive-hermes/config.json` on their machine only.
