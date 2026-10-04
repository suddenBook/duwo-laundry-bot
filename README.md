# DUWO Laundry Bot

Telegram bot for the DUWO laundry system: estimates when your own machine is
ready, and answers questions about availability, slots, bookings and balance on
demand. Readiness is a timer estimate, not a door sensor reading.

## Why it exists

DUWO releases a machine on a **fixed timer** measured from the moment you start
it at the terminal — 35 minutes for a washer, 40 for a dryer. The real programme
runs longer, so DUWO reports "free" well before the door will open. Measured on
this account:

| | DUWO says finished | Door actually opened | Gap |
|---|---|---|---|
| washer, 12-09 | +35 min | +53.5 min | +18 min |
| washer, 14-09 | +35 min | +57 min | +22 min |

So the bot ignores DUWO's idea of "finished". It anchors on the machine **start**
(which DUWO does report accurately, to the second, in its activity log) and runs
its own clock, which `/done` calibrates against reality over time.

## Features

- Messages you when one of your machines starts, a few minutes before its
  estimated finish, and when the estimated cycle time has elapsed
- Learns your real cycle length from `/done` feedback (EWMA, persisted to disk)
- Timers survive a container restart
- **Quiet by default** — availability and low-balance alerts are opt-in;
  connection problems can still trigger an alert
- Reserves **any** time window, not just the whole hours DUWO's calendar
  offers: `/book_at washer 23:17 23:57`
- `/status` shows free machines, your balance, your own bookings, and what
  the rest of the room has reserved ahead — all in one message
- `/status`, `/cycles`, `/done`, `/slots`, `/slots_dryer`, `/book`,
  `/book_dryer`, `/book_at`, `/bookings`, `/cancel`, `/balance`, `/qr`
- Verifies booking and cancellation against fresh personal reservations,
  including the machine type and full time window
- Keeps personal and room schedules separate, and reports unavailable data
  instead of claiming there are no bookings
- Continues saved cycle timers during a DUWO outage, retries failed notifications,
  and persists Telegram command offsets to prevent replay after a restart
- `/book N` finds a single window with enough capacity for all N machines;
  partial success is reported explicitly

## Environment

Copy `.env.example` to `.env` and fill in real values.

Required:

- `DUWO_EMAIL`, `DUWO_PASSWORD` — DUWO account login
- `TELEGRAM_BOT_TOKEN` — token from BotFather
- `TELEGRAM_CHAT_ID` — the only chat allowed to control the bot

Cycle tracking:

- `WASHER_CYCLE_MINUTES` (default `55`) — real washer duration, start to door open;
  the only value `/done` calibrates
- `DRYER_CYCLE_MINUTES` (default `40`) — dryer duration; accurate as advertised,
  so it is never adjusted by `/done`
- `CYCLE_HEADSUP_MINUTES` (default `5`) — warning before ready, so you can walk down
- `CYCLE_LOOKBACK_HOURS` (default `6`) — how far back to look for machine starts
- `STATE_PATH` (default `/data/state.json`) — where timers, learned
  durations and the discovered room id are kept; must be on a volume to
  survive a restart

Other:

- `CHECK_INTERVAL` (default `60`) — seconds between DUWO polls, minimum 30
- `DUWO_LOCATION_ID` (default: auto-detect) — the laundry room id that booking
  values start with. It is read off the booking calendar the first time one is
  loaded and then kept in the state file, so this is only an escape hatch for
  if DUWO stops printing it
- `NOTIFY_AVAILABILITY` (default `false`) — push when free machines cross a threshold
- `NOTIFY_LOW_BALANCE` (default `false`) — push when the balance is low
- `LOW_BALANCE_THRESHOLD`, `WASHER_NOTIFY_THRESHOLD`, `DRYER_NOTIFY_THRESHOLD`
  — only used when the matching alert above is enabled

## Deploy (Docker Compose)

```bash
docker compose up -d --build
```

State lives in `./data`, which is bind-mounted to `/data` in the container, so
running timers and learned cycle lengths survive `docker compose up` and
restarts.

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

- `/done` closes out washers started within 15 minutes of the most recent
  washer, which is how loads are actually run — two or three back to back. It
  calibrates from the *last* machine to start, since that is the one that
  decides when the batch can be emptied. Only the washer is learned; the dryer
  really does take its advertised 40 minutes, so a finished dryer closes itself
  out and never asks for `/done`.
- `/book_at` books a window DUWO's own UI cannot express. `CreateBooking.php`
  can accept it and then return HTTP 500 while trying to draw the result into an
  hour grid — *after* committing — so the status code is ignored and the
  booking is confirmed against the personal `BookingOverview.php` instead.
  Timeouts are verified the same way, without repeating the creation request.
  Such a booking can be invisible on the hour calendar; `/bookings` can still
  list it. `/book_at` returns the cancellation ID; `/cancel` also accepts the
  separate BookingNR from the activity log for compatibility.
- On first run, past notifications are suppressed. Machines still running
  retain their upcoming reminders. State migration preserves existing timers
  and learned durations.
- `/balance` explicitly selects `main.php?page=user.php`. DUWO sometimes renders
  the previous page once, so the account page is checked with at most two reads.
  Missing balance data is marked unavailable or last-known; recharge history
  and booking previews are never mistaken for the current balance.
- `/qr` deliberately generates a new QR code each time, so a code someone
  photographed over your shoulder stops working.
- Machine-start detection reads `UserLog.php`, which is always scoped to the
  logged-in account. `BookingOverview.php` is *not*: once `LocNR` is set in the
  PHP session it lists the whole location's bookings instead, so it must not be
  used for this. Personal booking reads restore account scope before parsing
  any cancellation IDs. The shared overview only shows the rows DUWO exposes;
  it is not a guarantee that no other reservations exist.
- Reservation changes require a verified preview. A session expiry during the
  prepared operation aborts it; the bot never resumes a half-prepared change
  in a fresh session. If the final outcome cannot be verified, check `/bookings`
  before retrying.
- Telegram command offsets are saved before executing each command. A process
  interruption can leave that command unanswered; it will not be automatically
  executed again. Pending later commands remain queued.
- Network errors on the NAS can still delay fresh website data. Saved timers
  continue, and the status output distinguishes DUWO's free count from your
  estimated physical completion time.

## Tests and dependency updates

The regression suite uses a simulated stateful DUWO session and mocked Telegram
responses; it never creates real reservations or sends Telegram messages.

```bash
uv sync --locked
uv run python -m unittest discover -s tests -v
```

The Docker build runs the same suite and installs the versions and hashes in
`requirements.txt`, exported from `uv.lock`. After changing dependencies:

```bash
uv lock
uv export --format requirements-txt --no-dev --no-emit-project -o requirements.txt
docker compose build
```
