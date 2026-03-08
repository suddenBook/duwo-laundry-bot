# DUWO Laundry Bot

Telegram bot for monitoring DUWO laundry machine availability, checking booking slots, creating and cancelling bookings, generating QR codes, and creating pay links.

## Features

- Monitors current machine status from DUWO
- Sends washer alerts only when the free-machine threshold is reached
- Supports `/status`, `/slots`, `/slots_dryer`, `/book`, `/book_dryer`, `/bookings`, `/cancel`, `/balance`, `/qr`, `/topup`
- Verifies booking and cancellation results instead of trusting a single response page
- Distinguishes backend failures from real "no data" cases in Telegram replies

## Environment

Copy `.env.example` to `.env` and fill in real values.

Required variables:

- `DUWO_EMAIL`: DUWO account login email
- `DUWO_PASSWORD`: DUWO account password
- `TELEGRAM_BOT_TOKEN`: Telegram bot token from BotFather
- `TELEGRAM_CHAT_ID`: target Telegram chat ID that is allowed to control the bot

Optional variables:

- `CHECK_INTERVAL`: polling interval in seconds, default `60`
- `LOW_BALANCE_THRESHOLD`: low-balance alert threshold in EUR, default `3.0`
- `WASHER_NOTIFY_THRESHOLD`: send a washer availability alert when current free washers reaches this number, default `3`
- `DRYER_NOTIFY_THRESHOLD`: send a dryer availability alert when current free dryers reaches this number, default `1`

## Local Run

```bash
uv sync
set -a && source .env && set +a
uv run python duwo_monitor.py
```

## Docker

Build:

```bash
docker build -t duwo-laundry-bot .
```

Run:

```bash
docker run -d \
  --name duwo-laundry-bot \
  --restart unless-stopped \
  --env-file .env \
  duwo-laundry-bot
```

Or with Compose:

```bash
docker compose up -d --build
```

## Notes

- `/balance` depends on what the DUWO site actually renders for the current account. If DUWO hides the balance block because of risk control, the bot will report that balance is unavailable.
- `/qr` intentionally generates a new QR code each time.
- `/topup` accepts positive values with up to 2 decimals and preserves exact cents when creating the payment link.

## GitHub Safety Checklist

This repo is publishable only if you do all of the following:

- Do not commit `.env`
- Do not commit old logs, screenshots, exported chat transcripts, or payment links
- Do not commit any real Telegram chat IDs, bot tokens, emails, or DUWO credentials
- Check git history as well, not just the current files
- If any real secret was ever committed before, rotate it before publishing

In the current workspace, `.env` still contains real secrets and must stay private.
