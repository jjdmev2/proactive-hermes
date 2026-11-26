# Proactive Hermes is installed

**First, check delivery:** `hermes proactive doctor`

Desk messages are real replies in this profile's **Bot Chat** (Bot Mode, on by default in the Hermes desktop app: open the Bots tab and click your Bot once). The job "Proactive · say" hands each one to your Bot through Hermes's own scheduled Bot Chat delivery. Keep the desktop app or `hermes gateway start` running, or nothing reaches you. After setup, `hermes proactive doctor --send-test` sends a test message so you can see it on your phone.

**Next step: set up your desk.** Pick one:

- **In a chat (recommended):** open this Bot's Bot Chat and type `/proactive setup`. Hermes interviews you, one short question at a time.
- **In this terminal:** `hermes proactive setup`

You choose **Juan's setup** (proven defaults, you only fill in your details) or **My own** (Hermes builds it with you). You see every job and time before anything is created.

**Recommended for Juan's setup:** an OpenAI API key with a few dollars of credit. Put it in Settings → Plugins → Proactive Hermes → **OpenAI key for the decision filter** (or `hermes plugins config proactive-hermes`). It can be the same key as `OPENAI_API_KEY`; Hermes hides that one from scheduled jobs, so the plugin needs its own copy. It powers the fast decision filter: noise out, promises spotted, about 3 cents per 1,000 emails. Without it everything still works.

**Optional, instant mail push:** by default mail is checked every 15 minutes. To get new mail within 1 to 3 minutes, make an app password for a mailbox (Gmail: open myaccount.google.com/apppasswords, with 2-Step Verification on; iCloud: account.apple.com > Sign-In and Security > App-Specific Passwords), then fill **Mailbox 1 address** and **Mailbox 1 app password** in Settings → Plugins → Proactive Hermes, or with `hermes plugins config proactive-hermes`. Never paste it into a chat. Restart Hermes, then `hermes proactive doctor` shows which mailboxes have instant push.

Public holidays: the plugin declares the `holidays` package, which Hermes installs with it after your yes. Without it the work recap simply does not skip holidays.

**Get the most out of it:** install the Hermes mobile app and share Reminders, Alarms, Location and Calendar, and sign in once (in the browser Hermes uses) to the shops, marketplaces and event sites you want watched.

Approval first: Hermes does on its own only what you allow in setup. Sending, buying and registering wait for your yes; payments and passwords always stay with you.
