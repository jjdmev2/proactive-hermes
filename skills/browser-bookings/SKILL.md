---
name: browser-bookings
description: Use when booking a ride, ticket or food via the browser.
version: 1.0.0
author: HeyRito.AI
license: MIT
metadata:
  hermes:
    tags: [booking, rides, tickets, food, browser, approval]
    related_skills: [proactive-desk]
---

# Browser bookings: rides, train tickets, food (approval-gated)

## When to use

The owner answers a look-ahead question with a ride, ticket or food choice ("taxi", "book the 09:09"), asks you to book a ride, a train or bus ticket or a delivery, or asks whether something can be booked for them.

You book through the websites, in the browser the owner is already logged in to. Nothing is ever requested, bought or paid without the owner's explicit yes to that exact quote.

## Authority first

Run `cd <HERMES_HOME>/scripts/proactive-hermes && python3 -c "import phconf; print(phconf.allowed('bookings'))"`. `never`: do not book; open the page or give the link and let them finish. `ask` (the default) or `auto`: you still need a yes to every single quote (a booking is never automatic). `payments`, card details and passwords stay in the owner's "never" list whatever this prints.

## Browser

Use the logged-in browser the owner set up in Hermes: the browser tool with `local=true` (a copy of their own browser profile, logins included), or the persistent GUI Chrome profile they run, if any. A fresh or cloud browser has no logins: never use it for these sites. Stay on the one shared screen and profile; do not start another browser.

## Sites

The owner picks them; ask once and keep the list in the desk state directory (`<HERMES_HOME>/plugin-data/proactive-hermes/state/bookings.md`). Typical choices:
- Ride hailing on the web (for example Uber's mobile site, which takes a typed pickup and drop-off).
- The national or regional rail or bus operator's site.
- A food delivery site.

Prefer the website. App-only flows (some ride apps, local transit tickets, wallets) cannot be booked here: say so and offer the closest web option, never fake it. Never propose an emulator for ride or payment apps: they fail integrity checks and risk flagging the owner's real accounts. Payment step-ups (bank code, Face ID) stay on the owner's own phone. Scooters unlock on site, so they are a suggestion, not a booking.

## Connect (once per site, the owner does it)

1. Open the site in the logged-in browser.
2. Tell the owner: "<Site> is open on my screen. Log in there yourself (phone code included). Tell me when done." Never type, read back or store a password or code.
3. Verify: the page no longer shows `Login` or `Sign up`, or a same-origin fetch returns 200. Add a line `connected: <site>, <date>` to `bookings.md`.

## Book (every time)

1. **Prepare:** fill pickup, drop-off and time (or train, date, seat class; or basket). Pickup = the owner's home or the place the look-ahead used; ask if unsure. Never reach for the final button.
2. **Quote:** read the exact price, product, pickup time and place, and the payment method shown. Take a screenshot of the quote area and save it under `<state>/bookings/`.
3. **Ask:** one chat message: "Taxi to the station, pickup 08:40 at home, 12.40 (card ending 4242). Book it? Reply yes" plus the screenshot path. Key `book:<site>:<item key>`.
4. **Only on the owner's yes to that message:** re-read the quote. If the price rose more than 10% or the time moved, ask again. Otherwise click Request/Buy once.
5. **Confirm:** read the confirmation (driver and ETA, ticket number, order id), send one line, and record it in the look-ahead (`python3 <scripts>/ea_lookahead.py answer --key K --mode uber|taxi|train|bus --note "<ref>"`) and in `bookings.md`.
6. **Payment step-up** (3-D Secure, bank app, Face ID): tell the owner it is waiting on their phone. Never try to bypass it.

## Hard rules

- One yes = one booking. A yes for "taxi Saturday" is not a yes for the return trip.
- Never add, change or remove payment methods, promo codes, tips, or account settings.
- Never cancel a booking without a yes. Cancellation fees count as money.
- If a page changed and the flow is unclear, stop and say what you see. Do not guess clicks.
- Many of these sites' terms (ride hailing especially) forbid automated access. The owner accepts that risk for their own account, with a human approval on every booking. Keep the volume human: no polling of prices, no repeated quotes.
- Prices are "as shown at HH:MM". Surge and fares can change them before the yes.

## New site

Dry run first: the owner logs in, you stop at the quote. Then one real booking on their yes. Only then treat the site as connected.
