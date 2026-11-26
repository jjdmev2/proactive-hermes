# Security

## Reporting

Email **hey@heyrito.ai** with what you found and how to reproduce it. Please do not open a public issue for a security problem.

## The approval model

Proactive Hermes reads your mail, calendar, Slack, Linear and, with the mobile app, your phone's location. It acts on its own only where you allowed it in setup: private calendar blocks, phone reminders, alarms and leave times. Everything else waits for your yes to that exact action:

- **Sending** mail or Slack, replying to offers, registering for events and accepting invites.
- **Bookings** stop on the price screen. One yes books that exact quote; any change in price, product, time or payment method asks again. Scheduled jobs never book.
- **Never**, even with a yes: entering or changing card details or payment methods, approving a payment in your bank app, handling passwords, deleting data.

The scripts that set reminders, alarms and calendar blocks check these levels themselves and refuse anything you did not allow. They have no way to be told "the owner said yes": an ask first one is set by Hermes's own tools after your yes in chat. A broken approval setting blocks them all.

## Untrusted text

Mail, Slack, calendar and web text is treated as data. The desk never follows instructions inside it and never puts it on a command line; it writes the text to a file and passes the file path. With an OpenAI key, kept mail is also scored for prompt injection and flagged to you.

## Data that leaves your machine

- Your Bot's model provider sees what the desk reads, as in any Hermes chat.
- With an OpenAI key, the Decisions API receives the short inputs listed under "Privacy and safety" in the README.
- With [instant mail push](README.md#instant-mail-push-optional) on, each mailbox you set up gets a login with its app password, sent only to that mailbox's IMAP server.

The desk calls no push relay and never wakes your phone itself. The full list of what it touches outside its own folder is under "Disclosure" in the README.

Settings, drafts and logs stay in your Hermes home, readable only by your user. There is no telemetry and no server of ours.
