# Proactive Hermes is installed

**First, check delivery:** `hermes proactive doctor`

Desk messages are real replies in this profile's **Bot Chat** (Bot Mode, on by default in the Hermes desktop app: open the Bots tab and click your Bot once). Keep the desktop app or `hermes gateway start` running, or nothing reaches you. `hermes proactive doctor --send-test` posts a test message so you can see it on your phone.

**Next step: set up your desk.** Pick one:

- **In a chat (recommended):** open this Bot's Bot Chat and type `/proactive setup`. Hermes interviews you, one short question at a time.
- **In this terminal:** `hermes proactive setup`

You choose **Juan's setup** (proven defaults, you only fill in your details) or **My own** (Hermes builds it with you). You see every job and time before anything is created.

**Recommended for Juan's setup:** an OpenAI API key with a few dollars of credit (`OPENAI_API_KEY` in your Hermes `.env`). It powers the fast decision filter: noise out, promises spotted, about 3 cents per 1,000 emails. Without it everything still works.

Optional: `pip install holidays` to skip public holidays.

**Get the most out of it:** install the Hermes mobile app and share Reminders, Alarms, Location and Calendar, and sign in once (in the browser Hermes uses) to the shops, marketplaces and event sites you want watched.

Approval first: Hermes does on its own only what you allow in setup. Sending, buying and registering wait for your yes; payments and passwords always stay with you.
