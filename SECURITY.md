# Security

## Reporting

Email **hey@heyrito.ai** with what you found and how to reproduce it. Please do not open a public issue for a security problem.

## The approval model

Proactive Hermes reads your mail, calendar, Slack, Linear and, with the mobile app, your phone's location. It acts on its own only where you allowed it in setup: private calendar blocks, phone reminders, alarms and leave times. Everything else waits for your yes to that exact action:

- **Sending** mail or Slack, replying to offers, registering for events and accepting invites.
- **Bookings** stop on the price screen. One yes books that exact quote; any change in price, product, time or payment method asks again. Scheduled jobs are instructed not to book or pay and have no booking or payment authority by default.
- **Never**, even with a yes: entering or changing card details or payment methods, approving a payment in your bank app, handling passwords, deleting data.

The scripts that set reminders, alarms and calendar blocks check these levels themselves and refuse anything you did not allow. They have no way to be told "the owner said yes": an ask first one is set by Hermes's own tools after your yes in chat. A broken approval setting blocks them all.

If one action is listed at more than one level, the strictest wins: never beats ask first, and ask first beats without asking. Setup does not save such a setting at all: it names the action and the two levels, and it also refuses an unknown level or action instead of dropping it.

Cleaning up has its own approval, `cleanup`. Ticking off a phone reminder the desk set, or deleting a calendar block or alert it made, happens on its own only when cleanup is allowed without asking. Under ask first Hermes tells you what is left and offers to remove it with its own tools after your yes; under never it only tells you. The scripts never delete your own data.

The approval file is always your profile's own `plugin-data/proactive-hermes/config.json`, and the desk's records are always next to it. No environment variable other than `HERMES_HOME`, which selects the whole profile, changes the approval file or record folder. The runtime is installed only by `hermes proactive setup`, only inside your Hermes home, as one complete copy at a time that replaces the old one in a single step. Setup checks for symlinks before it saves anything and refuses to write through one. An old scripts folder from an earlier release is renamed and kept, not deleted, and files you added next to the scripts are carried over to each new copy.

## Known limits

- Scheduled jobs that read mail and web pages run as normal Hermes agent turns. Each asks for only the toolsets it needs: terminal, file and skills for every job, past chats for the promise tracker, web search for the look-ahead, web plus the browser only for watches, and `connections` (the Slack and Linear connectors) for every agent job when Slack, Linear or the daily call is set up.
- These lists are a narrowing request, not a sandbox. Hermes applies your `agent.disabled_toolsets` on top of them, so a job never gets a toolset you disabled there. A job's own list replaces `platform_toolsets.cron` for that job. Setup reads that setting through Hermes's read-only config loader and prints one warning line when a job asks for toolsets beyond it; it does not narrow a job by it. A bundle entry such as `hermes-cron` is not compared.
- Hermes also adds every enabled local MCP server to each job's tools, because none of these lists names one. Disable a server you do not want a scheduled job to reach in Hermes's own MCP settings.
- The terminal is still a terminal. The plugin enforces approvals inside its own scripts and tells every job to treat source text as data, but it does not sandbox what the agent could run there.
- To restrict further, use Hermes's own toolset and approval settings. Run `hermes proactive setup` again after a plugin update so older jobs get their toolsets.
- Desk messages reach Bot Chat as a scheduled turn your Bot answers. That turn is a normal Bot Chat turn with that Bot's tools. The desk keeps each message short, puts it between start and end markers, and asks the Bot to give the notice word for word as the chat summary, not an assertion of its own or instructions for it to act on. Every run of three or more `<` or `>` in the message becomes the same number of `‹` or `›`, even mid-line or with invisible characters between the brackets. This keeps text from closing the block early without adding backslashes. Hermes does not yet offer a tool-free or display-only delivery.
- `handed` means claimed by the plugin worker, not accepted by Hermes or seen by the owner. Definite no-sends identified by the matching public cron execution and its last-run status can retry up to three total attempts. A host-queued or accepted result is never resent by the plugin. If the public status is missing, overwritten, timed out or ambiguous, the plugin does not retry. A matching Bot reply is stronger evidence than cron status; its persistent ID confirms at most one notice. `hermes proactive doctor` and the health job's local failure output show unresolved deliveries even if the Bot Chat route is down. After 24 hours without a matching reply the notice is marked unverified, not automatically replayed.
- The intended recipient is the Bot Chat of the profile whose owner ran setup. Before producing output, the worker checks that profile's registered job ID, exact `bot-chat` token (not a named profile or combined target), and installed no-agent script. Hermes does not publicly expose a specific Bot Chat identity or the immutable firing route to a standalone cron script. The check cannot fence an administrator editing or reusing the script during a run; do not run this script from other jobs.
- Setup journals new or replacement jobs before arming them, and refuses to remove a replaced job if any saved run fails size and SHA-256 verification. Agent jobs at the public 500-attempt listing cap stay paused and journaled when history cannot be proven complete. Their replacement does not wait. Script-only say, health and tick outputs are not archived. Setup only reads Hermes's `cron/output`; its copies stay in private plugin data. Current Hermes does not expose agent prompts or `enabled_toolsets` in its public cron listing, so setup can reconcile observed live drift but cannot detect independent edits to those unlisted fields. Sessions are not auto-archived: Hermes's public `--title` selector is a substring filter, not an ownership check.

## Untrusted text

Jobs must write their own short synopsis, never paste a mail body, full subject, Slack quote or web prose into a notice, and pass source data in files rather than command arguments. The composer discards raw source fields, replaces recognizable pasted or verbatim source text with a generic pointer, strips recognized URLs, and caps the notice at 700 characters. A synopsis supplied as the only text field is not independently authenticated as a paraphrase. Marker escaping and prompt guidance do not prevent an agent from following embedded instructions. Hermes does not offer display-only or tool-free Bot Chat relay turns. With an OpenAI key, the mail filter can also flag suspected prompt injection.

## Data that leaves your machine

- Your Bot's model provider sees what the desk reads, as in any Hermes chat.
- With an OpenAI key, the Decisions API receives the short inputs listed under "Privacy and safety" in the README.
- With [instant mail push](README.md#instant-mail-push-optional) on, each mailbox you set up gets a login with its app password, sent only to that mailbox's IMAP server.

The desk calls no push relay and never wakes your phone itself. The full list of what it touches outside its own folder is under "Disclosure" in the README.

Settings, drafts and logs stay in your Hermes home, readable only by your user. There is no telemetry and no server of ours.
