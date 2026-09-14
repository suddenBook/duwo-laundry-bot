# DUWO Laundry Bot

Telegram bot for the DUWO laundry system: tells you when your own machine is
*actually* ready, and answers questions about machines, slots and bookings on
demand.

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

- Messages you when one of your machines starts, a few minutes before it is
  ready, and when it is really ready to open
- Learns your real cycle length from `/done` feedback (EWMA, persisted to disk)
- Timers survive a container restart
- **Quiet by default** — nothing else is ever pushed; ask with `/status`
- `/status`, `/cycles`, `/done`, `/slots`, `/slots_dryer`, `/book`,
  `/book_dryer`, `/bookings`, `/cancel`, `/balance`, `/qr`
- Verifies booking and cancellation results instead of trusting a single
  response page

## Environment

Copy `.env.example` to `.env` and fill in real values.

Required:

- `DUWO_EMAIL`, `DUWO_PASSWORD` — DUWO account login
- `TELEGRAM_BOT_TOKEN` — token from BotFather
- `TELEGRAM_CHAT_ID` — the only chat allowed to control the bot

Cycle tracking:

- `WASHER_CYCLE_MINUTES` (default `55`) — real washer duration, start to door open
- `DRYER_CYCLE_MINUTES` (default `44`) — real dryer duration
- `CYCLE_HEADSUP_MINUTES` (default `5`) — warning before ready, so you can walk down
- `CYCLE_LOOKBACK_HOURS` (default `6`) — how far back to look for machine starts
- `STATE_PATH` (default `/data/state.json`) — where timers and learned
  durations are kept; must be on a volume to survive a restart

Other:

- `CHECK_INTERVAL` (default `60`) — seconds between DUWO polls, minimum 30
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

- `/done` closes out every machine started within 15 minutes of the most recent
  one, which is how loads are actually run — two or three back to back. It
  calibrates from the *last* machine to start, since that is the one that
  decides when the batch can be emptied.
- On first run there is no state file, so machines already in the DUWO log are
  recorded silently rather than firing a burst of stale notifications.
- `/balance` depends on what the DUWO site renders for the account. If DUWO
  hides the balance block, the bot reports that balance is unavailable.
- `/qr` deliberately generates a new QR code each time, so a code someone
  photographed over your shoulder stops working.
- Machine-start detection reads `UserLog.php`, which is always scoped to the
  logged-in account. `BookingOverview.php` is *not*: once `LocNR` is set in the
  PHP session it lists the whole location's bookings instead, so it must not be
  used for this.
