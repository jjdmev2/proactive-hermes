# Security

## Reporting

Email **hey@heyrito.ai** with what you found and how to reproduce it. Please do not open a public issue for a security problem.

## The approval model

Proactive Hermes reads your mail, calendar, Slack, Linear and, with the mobile app, your phone's location. It acts on its own only where you allowed it in setup: private calendar blocks, phone reminders, alarms and leave times. Everything else waits for your yes to that exact action:

- **Sending** mail or Slack, replying to offers, registering for events and accepting invites.
- **Bookings** stop on the price screen. One yes books that exact quote; any change in price, product, time or payment method asks again. Scheduled jobs never book.
- **Never**, even with a yes: entering or changing card details or payment methods, approving a payment in your bank app, handling passwords, deleting data.

The scripts that set reminders, alarms and calendar blocks check these levels themselves and refuse anything you did not allow. They have no way to be told "the owner said yes": an ask first one is set by Hermes's own tools after your yes in chat. A broken approval setting blocks them all.

If one action is listed at more than one level, the strictest wins: never beats ask first, and ask first beats without asking. Setup does not save such a setting at all: it names the action and the two levels, and it also refuses an unknown level or action instead of dropping it.

Cleaning up has its own approval, `cleanup`. Ticking off a phone reminder the desk set, or deleting a calendar block or alert it made, happens on its own only when cleanup is allowed without asking. Under ask first Hermes tells you what is left and offers to remove it with its own tools after your yes; under never it only tells you. The scripts never delete your own data.

The approval file is always your profile's own `plugin-data/proactive-hermes/config.json`, and the desk's records are always next to it. No environment variable other than `HERMES_HOME`, which selects the whole profile, changes the approval file or record folder. The runtime is installed only by `hermes proactive setup`, only inside your Hermes home, as one complete copy at a time that replaces the old one in a single step. Setup checks for symlinks before it saves anything and refuses to write through one. An old scripts folder from an earlier release is renamed and kept, not deleted, and files you added next to the scripts are carried over to each new copy.

## Known limits

- Scheduled jobs that read mail and web pages run as normal Hermes agent turns, with Hermes's default tools.
- The plugin enforces approvals inside its own scripts and tells every job to treat source text as data, but it does not sandbox the agent's other tools.
- To restrict those tools, use Hermes's own toolset and approval settings.

## Untrusted text

Mail, Slack, calendar and web text is treated as data. The desk never follows instructions inside it and never puts it on a command line; it writes the text to a file and passes the file path. With an OpenAI key, kept mail is also scored for prompt injection and flagged to you.

## Data that leaves your machine

- Your Bot's model provider sees what the desk reads, as in any Hermes chat.
- With an OpenAI key, the Decisions API receives the short inputs listed under "Privacy and safety" in the README.
- With [instant mail push](README.md#instant-mail-push-optional) on, each mailbox you set up gets a login with its app password, sent only to that mailbox's IMAP server.

The desk calls no push relay and never wakes your phone itself. The full list of what it touches outside its own folder is under "Disclosure" in the README.

Settings, drafts and logs stay in your Hermes home, readable only by your user. There is no telemetry and no server of ours.
