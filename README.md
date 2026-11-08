# DUWO Laundry Bot

Telegram bot for the DUWO laundry system: estimates when your own machine is ready, and answers questions about availability, slots, bookings and balance on demand. Readiness is a timer estimate, not a door sensor reading.

## Why it exists

DUWO releases a machine on a **fixed timer** measured from the moment you start it at the terminal — 35 minutes for a washer, 40 for a dryer. The real programme runs longer, so DUWO reports "free" well before the door will open. Measured on this account:

| | DUWO says finished | Door actually opened | Gap |
|---|---|---|---|
| washer, 12-09 | +35 min | +53.5 min | +18 min |
| washer, 14-09 | +35 min | +57 min | +22 min |

So the bot ignores DUWO's idea of "finished". It anchors on the machine **start** (which DUWO reports to the minute in its activity log) and runs its own clock, which `/done` calibrates against reality over time.

## Features

- Messages you when one of your machines starts, a few minutes before its estimated finish, and when the estimated cycle time has elapsed
- Learns your real cycle length from `/done` feedback (EWMA, persisted to disk)
- Timers survive a container restart
- **Quiet by default** — availability and low-balance alerts are opt-in; connection problems and DUWO blocks can still trigger an alert
- Reserves **any** time window of up to 2 hours, not just the whole hours DUWO's calendar offers: `/book_at washer 23:17 23:57`
- `/status` shows free machines, your balance, your own bookings, and how full the booking calendar is for the coming hours — all in one message
- `/status`, `/cycles`, `/done`, `/slots`, `/slots_dryer`, `/book`, `/book_dryer`, `/book_at`, `/bookings`, `/cancel`, `/balance`, `/qr`
- Verifies booking and cancellation against fresh personal reservations, including the machine type and full time window
- Reports unavailable data instead of claiming there are no bookings
- Continues saved cycle timers during a DUWO outage, retries failed notifications, and persists Telegram command offsets to prevent replay after a restart
- `/book N` finds the earliest single window today or tomorrow with enough places for all N machines (DUWO normally allows 2 open bookings); partial success is reported explicitly, and every reply says by when the machine must be started
- Recognises DUWO's block notices (error code 101), pauses every request until the announced time and tells you once why, and never sends a character DUWO's attack filter rejects
- Shares the account with you: DUWO allows one login session per account, so after you sign in on your phone the bot leaves the session to you for a while instead of signing you straight back out

## Environment

Copy `.env.example` to `.env` and fill in real values.

Required:

- `DUWO_EMAIL`, `DUWO_PASSWORD` — DUWO account login
- `TELEGRAM_BOT_TOKEN` — token from BotFather
- `TELEGRAM_CHAT_ID` — the only chat allowed to control the bot

Cycle tracking:

- `WASHER_CYCLE_MINUTES` (default `55`) — real washer duration, start to door open; the only value `/done` calibrates. Changing it restarts the learning from the new value
- `DRYER_CYCLE_MINUTES` (default `40`) — dryer duration; accurate as advertised, so it is never adjusted by `/done`
- `CYCLE_HEADSUP_MINUTES` (default `5`) — warning before ready, so you can walk down
- `CYCLE_LOOKBACK_HOURS` (default `6`) — how far back to look for machine starts
- `STATE_PATH` (default `/data/state.json`) — where timers, learned durations and the discovered room id are kept; must be on a volume to survive a restart

Other:

- `CHECK_INTERVAL` (default `60`) — seconds between DUWO polls, minimum 30
- `SESSION_YIELD_MINUTES` (default `15`) — DUWO keeps one login session per account, so signing in on your phone ends the bot's session and the bot signing back in ends yours. After losing its session the bot leaves the account to you this long before its background checks sign in again; commands you send still sign in at once. `0` signs straight back in
- `DUWO_LOCATION_ID` (default: auto-detect) — the laundry room id that booking values start with. It is read off the booking calendar the first time one is loaded and then kept in the state file, so this is only an escape hatch for if DUWO stops printing it
- `NOTIFY_AVAILABILITY` (default `false`) — push when free machines cross a threshold
- `NOTIFY_LOW_BALANCE` (default `false`) — push when the balance is low
- `LOW_BALANCE_THRESHOLD`, `WASHER_NOTIFY_THRESHOLD`, `DRYER_NOTIFY_THRESHOLD` — only used when the matching alert above is enabled

## Deploy (Docker Compose)

```bash
docker compose up -d --build
```

State lives in `./data`, which is bind-mounted to `/data` in the container, so running timers and learned cycle lengths survive `docker compose up` and restarts.

To update after changing the code:

```bash
docker compose up -d --build && docker compose logs -f
```

## Local Run

```bash
uv sync
set -a && source .env && set +a
STATE_PATH=./data/state.json uv run python duwo_monitor.py
```

## Notes

- How DUWO bookings work: a booking reserves a place for a time window, not a particular machine. Booking is free; you pay when you start a machine at the terminal. DUWO cancels a booking that is not started within 15 minutes of its start, offers bookings for only about half of the machines, and normally allows 2 open bookings at a time.
- `/done` closes the washer whose estimated finish is nearest — the one the last "should be ready" message was about — together with washers started before it and any due within 5 minutes, so loads run back to back are emptied with one `/done` while a load started much later keeps running. Only that washer teaches the estimate, and only when its time is within 0.6–1.5× the current estimate. The dryer really does take its advertised 40 minutes, so a finished dryer closes itself out and never asks for `/done`.
- `/book_at` books a window DUWO's own UI cannot express. `CreateBooking.php` has been seen to accept such a window and then return HTTP 500 *after* committing, so its status code is ignored and the booking is confirmed against the personal `BookingOverview.php` instead. Timeouts are verified the same way, without repeating the creation request. On the hour calendar such a booking only shows as a taken hour; `/bookings` lists it and `/status` marks the hour as yours. `/book_at` returns the cancellation ID; `/cancel` also accepts the separate BookingNR from the activity log.
- On first run, past notifications are suppressed. Machines still running retain their upcoming reminders. A machine first seen more than 30 minutes after its estimated finish, for example after downtime, is closed without a message. State migration preserves existing timers and learned durations.
- `/balance` reads the dashboard selected by `main.php?page=home`, the site's own Home button; a second read covers DUWO rendering the previous page once more. Missing balance data is marked unavailable or last-known; recharge history and booking previews are never mistaken for the current balance.
- `/qr` deliberately generates a new QR code each time, so a code someone photographed over your shoulder stops working.
- Machine-start detection reads `UserLog.php`, the account's activity log, which lists entries per day to the minute. A start is matched to its machine by the payment row carrying the exact booking number (EUR 2.00 washer, EUR 1.00 dryer), or else by the machine named on the debit just before it. If the log's layout cannot be read, the bot says so once rather than mistaking it for a quiet laundry room.
- DUWO shows each resident only their own bookings, so `BookingOverview.php` is the account's own list whichever room the session has selected. The room's schedule in `/status` therefore comes from the booking calendar: for each coming hour, how many bookable places are left, `full`, or `yours`.
- DUWO blocks a whole internet connection for a few hours (error code 101) when a request carries a character its attack filter rejects; another device on the same connection can trigger it too. It can also block an account (error code 101-<number>) until an administrator lifts it, or lock the login after too many attempts. The bot cleans the e-mail address and password exactly as the login page does, refuses to send any other unexpected value, and on a block notice pauses every request until the announced time, messaging you once with the reason and once more when DUWO answers again.
- Reservation changes require a verified preview. A session expiry during the prepared operation aborts it; the bot never resumes a half-prepared change in a fresh session. If the final outcome cannot be verified, check `/bookings` before retrying.
- Telegram command offsets are saved before executing each command. A process interruption can leave that command unanswered; it will not be automatically executed again. Pending later commands remain queued, but one that changes something at DUWO (`/book`, `/book_dryer`, `/book_at`, `/cancel`, `/qr`) is skipped with a note when it arrives more than 10 minutes after it was sent, and `/done` counts from the moment it was sent. In a group chat, commands addressed `/status@ThisBot` work and commands for other bots are ignored.
- Network errors on the NAS can still delay fresh website data. Saved timers continue, and the status output distinguishes DUWO's free count from your estimated physical completion time.

## Tests and dependency updates

The regression suite uses a simulated stateful DUWO session and mocked Telegram responses; it never creates real reservations or sends Telegram messages.

```bash
uv sync --locked
uv run python -m unittest discover -s tests -v
```

The Docker build runs the same suite and installs the versions and hashes in `requirements.txt`, exported from `uv.lock`. After changing dependencies:

```bash
uv lock
uv export --format requirements-txt --no-dev --no-emit-project -o requirements.txt
docker compose build
```
