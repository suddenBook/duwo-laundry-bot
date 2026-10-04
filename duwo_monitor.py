#!/usr/bin/env python3
"""DUWO Laundry Monitor - Telegram bot that monitors and books laundry machines."""

import html
import io
import math
from collections import Counter
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urljoin, urlparse
import json
import os
import re
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

try:
    import qrcode
except ImportError:
    qrcode = None

# ============== Config ==============

# Load .env file if present (without requiring python-dotenv)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

_REQUIRED_VARS = [
    "DUWO_EMAIL",
    "DUWO_PASSWORD",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]
_missing = [v for v in _REQUIRED_VARS if not os.environ.get(v)]
if _missing:
    print(f"[FATAL] Missing environment variables: {', '.join(_missing)}")
    print("Set them in .env file or docker-compose environment.")
    time.sleep(10)
    raise SystemExit(1)

EMAIL = os.environ["DUWO_EMAIL"]
PASSWORD = os.environ["DUWO_PASSWORD"]
BASE_URL = "https://duwo.multiposs.nl"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[WARN] {name}={raw!r} is not an integer, using {default}")
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw.replace(",", "."))
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        return value
    except ValueError:
        print(f"[WARN] {name}={raw!r} is not a number, using {default}")
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


CHECK_INTERVAL = max(30, _env_int("CHECK_INTERVAL", 60))
LOW_BALANCE_THRESHOLD = _env_float("LOW_BALANCE_THRESHOLD", 3.0)
WASHER_NOTIFY_THRESHOLD = _env_int("WASHER_NOTIFY_THRESHOLD", 3)
DRYER_NOTIFY_THRESHOLD = _env_int("DRYER_NOTIFY_THRESHOLD", 1)

# Unsolicited alerts are OFF by default: ask with /status when you care.
NOTIFY_AVAILABILITY = _env_bool("NOTIFY_AVAILABILITY", False)
NOTIFY_LOW_BALANCE = _env_bool("NOTIFY_LOW_BALANCE", False)

# Own-machine cycle tracking.
#
# DUWO releases a machine on a FIXED timer measured from the terminal start:
# 35 min for a washer, 40 min for a dryer (verified across every booking in
# this account's history).  The real program runs longer -- measured washer
# start -> door actually open was 53.5 min and 56.4 min on two occasions.
# So DUWO's "finished" is useless as a "go collect it" signal and we run our
# own clock instead, refined from /done feedback.
WASHER_CYCLE_MINUTES = max(15, min(150, _env_int("WASHER_CYCLE_MINUTES", 55)))
DRYER_CYCLE_MINUTES = max(15, min(150, _env_int("DRYER_CYCLE_MINUTES", 40)))
CYCLE_HEADSUP_MINUTES = max(0, _env_int("CYCLE_HEADSUP_MINUTES", 5))
CYCLE_LOOKBACK_HOURS = max(3, _env_int("CYCLE_LOOKBACK_HOURS", 6))
STATE_PATH = (os.environ.get("STATE_PATH") or "/data/state.json").strip()

# The laundry room id that every booking value starts with (the 45 in
# "45|2026-09-14|17:00:00|17:59").  Normally discovered from the calendar and
# remembered, so this is only an escape hatch for when DUWO stops printing it.
LOCATION_ID = (os.environ.get("DUWO_LOCATION_ID") or "").strip()

WASHER_TYPE_ID = 93
DRYER_TYPE_ID = 94
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


# ============== Data ==============


@dataclass
class MachineStatus:
    location: str
    machine_type: str
    status: str
    available_count: int | None


@dataclass
class BookingSlot:
    raw_value: str  # e.g. "45|2026-03-08|13:00:00|13:59"
    location_id: str
    date: str
    start_time: str
    end_time: str
    available_count: int
    price: str


@dataclass
class Cycle:
    """One run of one of OUR machines, anchored on DUWO's "Booking Started"."""

    booking_nr: str
    machine: str  # "Washer" or "Dryer"
    started_at: datetime
    notified_start: bool = False
    notified_headsup: bool = False
    notified_done: bool = False
    collected_at: datetime | None = None


# ============== Cycle Tracking ==============


class CycleTracker:
    """Runs our own clock over our own machines, and learns the real duration.

    DUWO frees a machine on a fixed timer measured from the terminal start
    (washer 35 min, dryer 40 min) which expires well before the door does, so
    its "finished" is not a usable "go collect it" signal.  We anchor instead
    on the machine start -- which DUWO does report accurately, to the second --
    and count forward by a per-type duration that /done feedback calibrates.
    """

    EWMA_ALPHA = 0.4
    # The dryer really does run its advertised 40 minutes, so only the washer
    # -- whose door lags DUWO's timer by ~20 min -- is worth learning.
    CALIBRATED = ("Washer",)
    MIN_MINUTES = 15.0
    MAX_MINUTES = 150.0
    BATCH_WINDOW = timedelta(minutes=15)

    def __init__(self, path: str):
        self.path = path
        self.cycles: dict[str, Cycle] = {}
        self.duration = {
            "Washer": float(WASHER_CYCLE_MINUTES),
            "Dryer": float(DRYER_CYCLE_MINUTES),
        }
        # Not a cycle, but this is the one file we already persist: the room id
        # is only ever printed on a *free* calendar slot, so a bot that starts
        # up on a fully booked day could not otherwise find it at all.
        self.location_id: str | None = None
        self.telegram_offset = 0
        self.cycle_baseline_set = False
        self.loaded = self._load()

    def _load(self) -> bool:
        try:
            with open(self.path) as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return False
        except (OSError, ValueError) as exc:
            print(f"[WARN] Could not read state file {self.path}: {exc}")
            return False
        if not isinstance(data, dict):
            print("[WARN] Invalid state file shape; starting with empty state")
            return False
        offset = data.get("telegram_offset", 0)
        self.telegram_offset = offset if type(offset) is int and offset >= 0 else 0
        self.cycle_baseline_set = data.get("cycle_baseline_set", True) is True
        stored_location = str(data.get("location_id") or "").strip()
        if stored_location.isdigit():
            self.location_id = stored_location
        durations = data.get("duration")
        for machine, value in (
            durations if isinstance(durations, dict) else {}
        ).items():
            if machine not in self.CALIBRATED:
                # Not learned, so the configured value wins over whatever an
                # older state file happens to hold.
                continue
            try:
                minutes = float(value)
                if math.isfinite(minutes):
                    self.duration[machine] = self._clamp(minutes)
            except (TypeError, ValueError):
                continue
        raw_cycles = data.get("cycles")
        for raw in raw_cycles if isinstance(raw_cycles, list) else []:
            if not isinstance(raw, dict):
                continue
            try:
                cycle = Cycle(
                    booking_nr=str(raw["booking_nr"]),
                    machine=str(raw["machine"]),
                    started_at=datetime.fromisoformat(raw["started_at"]),
                    notified_start=bool(raw.get("notified_start")),
                    notified_headsup=bool(raw.get("notified_headsup")),
                    notified_done=bool(raw.get("notified_done")),
                    collected_at=(
                        datetime.fromisoformat(raw["collected_at"])
                        if raw.get("collected_at")
                        else None
                    ),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if (
                cycle.machine not in ("Washer", "Dryer")
                or not cycle.booking_nr.isdigit()
            ):
                continue
            if cycle.started_at.tzinfo is not None:
                cycle.started_at = datetime.fromtimestamp(cycle.started_at.timestamp())
            if cycle.collected_at and cycle.collected_at.tzinfo is not None:
                cycle.collected_at = datetime.fromtimestamp(
                    cycle.collected_at.timestamp()
                )
            if cycle.started_at > datetime.now() + timedelta(minutes=1):
                continue
            self.cycles[cycle.booking_nr] = cycle
        return True

    def save(self):
        payload = {
            "telegram_offset": self.telegram_offset,
            "cycle_baseline_set": self.cycle_baseline_set,
            "duration": self.duration,
            "location_id": self.location_id,
            "cycles": [
                {
                    "booking_nr": c.booking_nr,
                    "machine": c.machine,
                    "started_at": c.started_at.isoformat(),
                    "notified_start": c.notified_start,
                    "notified_headsup": c.notified_headsup,
                    "notified_done": c.notified_done,
                    "collected_at": c.collected_at.isoformat()
                    if c.collected_at
                    else None,
                }
                for c in self.cycles.values()
            ],
        }
        directory = os.path.dirname(self.path)
        temp = f"{self.path}.tmp"
        try:
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(temp, "w") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(temp, self.path)
            return True
        except OSError as exc:
            print(f"[WARN] Could not write state file {self.path}: {exc}")
            return False

    def remember_location_id(self, value: str):
        """Record a newly discovered room id, persisting it immediately."""
        if value and value != self.location_id:
            self.location_id = value
            self.save()

    def _clamp(self, minutes: float) -> float:
        return max(self.MIN_MINUTES, min(self.MAX_MINUTES, minutes))

    def minutes_for(self, machine: str) -> float:
        return self.duration.get(machine, float(WASHER_CYCLE_MINUTES))

    def eta(self, cycle: Cycle) -> datetime:
        return cycle.started_at + timedelta(minutes=self.minutes_for(cycle.machine))

    def merge(self, observed: list[Cycle], suppress: bool) -> list[Cycle]:
        """Register cycles we have not seen before; return the new ones.

        On the very first run there is no state file, so every cycle still in
        the log would fire a burst of stale notifications.  `suppress` marks
        them as already announced instead.
        """
        added = []
        now = datetime.now()
        for cycle in observed:
            if cycle.booking_nr in self.cycles:
                continue
            if suppress:
                cycle.notified_start = True
                cycle.notified_headsup = now >= self.eta(cycle) - timedelta(
                    minutes=CYCLE_HEADSUP_MINUTES
                )
                cycle.notified_done = now >= self.eta(cycle)
                if cycle.notified_done and cycle.machine not in self.CALIBRATED:
                    cycle.collected_at = self.eta(cycle)
            self.cycles[cycle.booking_nr] = cycle
            added.append(cycle)
        return added

    def pending(self) -> list[Cycle]:
        return sorted(
            (c for c in self.cycles.values() if c.collected_at is None),
            key=lambda c: c.started_at,
        )

    def record_collected(self, now: datetime):
        """Close out the most recent batch and calibrate from it.

        Washers started within BATCH_WINDOW of each other are one batch --
        that is how they are actually used, two or three loads back to back.
        Dryers close automatically and are not included here.
        Calibration uses the LAST washer to start, because that is the one
        that decides when the whole batch can be emptied.
        """
        self.prune(now)
        pending = [c for c in self.pending() if c.machine in self.CALIBRATED]
        if not pending:
            return [], None, None
        newest = pending[-1]
        batch = [
            c for c in pending if newest.started_at - c.started_at <= self.BATCH_WINDOW
        ]
        observed = (now - newest.started_at).total_seconds() / 60.0
        learned = None
        if observed < self.MIN_MINUTES:
            return [], None, None
        if (
            newest.machine in self.CALIBRATED
            and self.MIN_MINUTES <= observed <= self.MAX_MINUTES
        ):
            previous = self.minutes_for(newest.machine)
            learned = self._clamp(
                self.EWMA_ALPHA * observed + (1 - self.EWMA_ALPHA) * previous
            )
            self.duration[newest.machine] = learned
        for cycle in batch:
            cycle.collected_at = now
        return batch, observed, learned

    def prune(self, now: datetime):
        # A load nobody ever confirmed with /done should not sit in /cycles for
        # ever, and should not be what the next /done closes out.
        changed = False
        stale = now - timedelta(hours=CYCLE_LOOKBACK_HOURS)
        for cycle in self.cycles.values():
            if cycle.collected_at is None and cycle.started_at < stale:
                changed = True
                cycle.collected_at = cycle.started_at + timedelta(
                    minutes=self.minutes_for(cycle.machine)
                )

        cutoff = now - timedelta(hours=max(CYCLE_LOOKBACK_HOURS * 2, 24))
        for key in [k for k, c in self.cycles.items() if c.started_at < cutoff]:
            del self.cycles[key]
            changed = True
        return changed


# ============== Telegram Bot ==============


class TelegramBot:
    """Handles sending messages and receiving commands via Telegram."""

    def __init__(self, store=None):
        self._state_store = store
        self.last_update_id = store.telegram_offset if store else 0

    def _validate_response(self, action: str, resp: requests.Response) -> bool:
        if not resp.ok:
            print(f"[TG] {action} http error: {resp.status_code}")
            return False
        try:
            data = resp.json()
        except ValueError:
            print(f"[TG] {action} invalid JSON response")
            return False
        if not isinstance(data, dict) or not data.get("ok"):
            print(f"[TG] {action} api error")
            return False
        return True

    @staticmethod
    def _chunks(text: str) -> list[str]:
        if len(text.encode("utf-16-le")) // 2 <= 3800:
            return [text]
        # Large HTML messages become escaped text chunks; preserve preformatted
        # tables with a complete wrapper around every chunk.
        pre = text.startswith("<pre>") and text.endswith("</pre>")
        plain = BeautifulSoup(text, "html.parser").get_text()
        chunks, current, length = [], [], 0
        for char in plain:
            escaped = html.escape(char)
            size = len(escaped.encode("utf-16-le")) // 2
            if length + size > 3500:
                chunks.append("".join(current))
                current, length = [], 0
            current.append(escaped)
            length += size
        if current:
            chunks.append("".join(current))
        return [f"<pre>{chunk}</pre>" if pre else chunk for chunk in chunks]

    def send(self, text: str) -> bool:
        for chunk in self._chunks(text):
            if not self._send_text(chunk):
                return False
        return True

    def _send_text(self, text: str) -> bool:
        if not TELEGRAM_CHAT_ID:
            print("[TG] send skipped: missing chat id")
            return False
        try:
            resp = requests.post(
                f"{TG_API}/sendMessage",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
            return self._validate_response("sendMessage", resp)
        except Exception as e:
            print(f"[TG] send error: {type(e).__name__}")
            return False

    def send_photo(self, photo_bytes: bytes, caption: str = "") -> bool:
        if not TELEGRAM_CHAT_ID:
            return False
        try:
            resp = requests.post(
                f"{TG_API}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"photo": ("qrcode.png", photo_bytes, "image/png")},
                timeout=10,
            )
            return self._validate_response("sendPhoto", resp)
        except Exception as e:
            print(f"[TG] send_photo error: {type(e).__name__}")
            return False

    def poll_commands(self) -> list[str]:
        try:
            resp = requests.get(
                f"{TG_API}/getUpdates",
                params={
                    "offset": self.last_update_id + 1,
                    "timeout": 0,
                    "limit": 1,
                    "allowed_updates": json.dumps(["message"]),
                },
                timeout=5,
            )
            if not self._validate_response("getUpdates", resp):
                return []
            data = resp.json()
            commands = []
            for update in data.get("result", []):
                update_id = update["update_id"]
                # Persist before dispatch: an interrupted reservation must never be
                # replayed on restart. Fetch one update so later commands aren't lost.
                if self._state_store:
                    self._state_store.telegram_offset = update_id
                    if not self._state_store.save():
                        return []
                self.last_update_id = update_id
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip()
                if chat_id == TELEGRAM_CHAT_ID and text:
                    commands.append(text)
            return commands
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            print(f"[TG] poll error: {type(exc).__name__}")
            return []

    def set_commands(self) -> bool:
        commands = [
            {"command": "status", "description": "Machine availability"},
            {"command": "cycles", "description": "Your running machines"},
            {"command": "done", "description": "Mark laundry collected"},
            {"command": "slots", "description": "Washer slots"},
            {"command": "slots_dryer", "description": "Dryer slots"},
            {"command": "book", "description": "Book N washers"},
            {"command": "book_dryer", "description": "Book N dryers"},
            {"command": "book_at", "description": "Reserve any time window"},
            {"command": "bookings", "description": "Your bookings"},
            {"command": "cancel", "description": "Cancel a booking"},
            {"command": "balance", "description": "Account balance"},
            {"command": "qr", "description": "Laundry QR code"},
            {"command": "help", "description": "Help"},
        ]
        try:
            resp = requests.post(
                f"{TG_API}/setMyCommands",
                json={"commands": commands},
                timeout=10,
            )
            if not self._validate_response("setMyCommands", resp):
                return False
            print("[OK] Bot command menu set")
            return True
        except Exception as e:
            print(f"[WARN] Failed to set command menu: {type(e).__name__}")
            return False


# ============== DUWO Client ==============


class DUWOClient:
    """Handles all communication with the DUWO laundry website."""

    # Login cooldown: exponential backoff on repeated failures
    LOGIN_COOLDOWN_BASE = 60  # first retry after 60s
    LOGIN_COOLDOWN_MAX = 1800  # cap at 30 minutes
    LOGIN_COOLDOWN_MULTIPLIER = 2

    def __init__(self, notify_callback=None, store=None):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
                ),
            }
        )
        self.logged_in = False
        self.start_url = None
        self.last_known_balance = None
        self.balance_checked_at = None
        self.balance_stale = False  # is last_known_balance a fallback?
        self.last_error = None
        # Login rate-limiting state
        self._login_fail_count = 0
        self._login_cooldown_until = 0.0  # timestamp: don't attempt login before this
        self._locked_until = 0.0  # timestamp: account lockout detected
        self._notify = notify_callback  # optional callable(str) for Telegram alerts
        self._location_set = False  # has LocNR been set in this session?
        self._state_store = store  # CycleTracker, for the room id
        self._room_id = LOCATION_ID or (store.location_id if store else None)

    def _clear_error(self):
        self.last_error = None

    def _set_error(self, message: str):
        self.last_error = message
        print(f"[WARN] {message}")

    def _send_alert(self, message: str):
        """Send a Telegram notification if callback is configured."""
        if self._notify:
            try:
                self._notify(message)
            except Exception:
                pass

    def _is_login_blocked(self) -> bool:
        """Check if we should NOT attempt login due to cooldown or lockout."""
        now = time.time()
        if self._locked_until > now:
            remaining = int(self._locked_until - now)
            mins, secs = divmod(remaining, 60)
            print(f"[LOCK] Account locked, {mins}m{secs}s remaining")
            return True
        if self._login_cooldown_until > now:
            remaining = int(self._login_cooldown_until - now)
            print(f"[WAIT] Login cooldown, {remaining}s remaining")
            return True
        return False

    def _on_login_success(self):
        """Reset cooldown state after successful login."""
        if self._login_fail_count > 0:
            self._send_alert("Login restored successfully.")
        self._login_fail_count = 0
        self._login_cooldown_until = 0.0
        self._locked_until = 0.0

    def _on_login_failure(self):
        """Apply exponential backoff after a login failure."""
        self._login_fail_count += 1
        cooldown = min(
            self.LOGIN_COOLDOWN_BASE
            * (self.LOGIN_COOLDOWN_MULTIPLIER ** min(self._login_fail_count - 1, 10)),
            self.LOGIN_COOLDOWN_MAX,
        )
        self._login_cooldown_until = time.time() + cooldown
        print(
            f"[WAIT] Login failed ({self._login_fail_count}x), next attempt in {int(cooldown)}s"
        )
        if self._login_fail_count == 1:
            self._send_alert(
                f"<b>Login failed</b>\nNext attempt in {int(cooldown)}s.\n"
                f"If this persists, check your credentials."
            )

    def _detect_lockout(self, resp_text: str) -> bool:
        """Check login response for account lockout, set _locked_until if found.

        Known lockout page format from DUWO:
            "Locked after too many login attempts !"
            "Try again @:20:28:19"
            "click here after :20:28:19"
        """
        lower = resp_text.lower()
        # Extract unlock time — formats: "@:HH:MM:SS", "after :HH:MM:SS", etc.
        time_patterns = [
            r"(?:try again|after)\s*@?\s*:?\s*(\d{1,2}:\d{2}(?::\d{2})?)",
            r"click here after\s*:?\s*(\d{1,2}:\d{2}(?::\d{2})?)",
        ]
        for pat in time_patterns:
            m = re.search(pat, lower)
            if m:
                return self._set_lockout_from_time(m.group(1))
        # Generic lockout detection without parseable time
        if any(
            kw in lower
            for kw in (
                "locked after too many",
                "too many login",
                "too many attempts",
                "geblokkeerd",
                "error 101",
            )
        ):
            # Default lockout: 30 minutes
            self._locked_until = time.time() + 1800
            unlock_str = datetime.fromtimestamp(self._locked_until).strftime("%H:%M")
            print(f"[LOCK] Account locked! Estimated unlock at {unlock_str}")
            self._send_alert(
                f"<b>Account locked!</b>\n"
                f"Too many login attempts detected.\n"
                f"Will retry around {unlock_str}."
            )
            return True
        return False

    def _set_lockout_from_time(self, time_str: str) -> bool:
        """Parse an HH:MM or HH:MM:SS lockout time and set _locked_until."""
        try:
            parts = time_str.split(":")
            h, m = int(parts[0]), int(parts[1])
            second = int(parts[2]) if len(parts) > 2 else 0
            now = datetime.now()
            unlock = now.replace(hour=h, minute=m, second=second, microsecond=0)
            if unlock <= now:
                unlock += timedelta(days=1)
            self._locked_until = unlock.timestamp()
            unlock_str = unlock.strftime("%H:%M")
            print(f"[LOCK] Account locked until {unlock_str}")
            self._send_alert(
                f"<b>Account locked!</b>\n"
                f"Too many login attempts.\n"
                f"Auto-retry at {unlock_str}."
            )
            return True
        except (ValueError, IndexError):
            return False

    def _is_auth_redirect(self, resp: requests.Response) -> bool:
        body = resp.text.lower()
        if (
            resp.status_code in (401, 403)
            or "/login/" in urlparse(resp.url).path.lower()
        ):
            return True
        soup = BeautifulSoup(body, "html.parser")
        if soup.select_one('input[name="PwdInput" i]'):
            return True
        if soup.get_text(" ", strip=True).upper() == "NO ACCESS":
            return True
        return bool(
            re.search(
                r"(?:window|document)\.location(?:\.href)?\s*=\s*['\"][^'\"]*(?:index\.(?:html|php)|login/)[^'\"]*['\"]",
                body,
            )
        )

    def _get(
        self,
        url: str,
        *,
        init_location: bool = False,
        allow_retry: bool = True,
        allow_http_error: bool = False,
    ) -> requests.Response:
        """Retry only reads; callers disable retries throughout a prepared mutation."""
        self.ensure_login()
        try:
            if init_location:
                self._init_location()
            resp = self.session.get(url, timeout=20)
            if self._is_auth_redirect(resp):
                raise PermissionError("DUWO session expired")
        except PermissionError:
            self.logged_in = False
            if not allow_retry:
                raise RuntimeError(
                    "DUWO session expired; the operation was not retried."
                )
            self.ensure_login()
            if init_location:
                self._init_location()
            resp = self.session.get(url, timeout=20)
            if self._is_auth_redirect(resp):
                self.logged_in = False
                raise RuntimeError("DUWO rejected the renewed session.")
        if not allow_http_error:
            resp.raise_for_status()
        return resp

    @staticmethod
    def _parse_amount(text: str) -> str | None:
        text = text.replace("\xa0", " ").strip()
        match = re.fullmatch(r"(?:EUR|€)?\s*(-?\s*\d[\d., ]*)\s*(?:EUR|€)?", text, re.I)
        if not match:
            return None
        number = match.group(1).replace(" ", "")
        if "," in number and "." in number:
            decimal = "," if number.rfind(",") > number.rfind(".") else "."
            number = number.replace("." if decimal == "," else ",", "").replace(
                decimal, "."
            )
        elif "," in number:
            number = number.replace(",", ".")
        try:
            value = Decimal(number)
            if not value.is_finite() or value.as_tuple().exponent < -2:
                return None
            return format(value, ".2f")
        except InvalidOperation:
            return None

    def _extract_balance(self, markup: str) -> str | None:
        soup = BeautifulSoup(markup, "html.parser")
        label = soup.select_one("#LblUserCredits")
        if label:
            return self._parse_amount(label.get_text(" ", strip=True))
        # Only an explicit current-balance label qualifies; credit history does not.
        for text in soup.stripped_strings:
            match = re.fullmatch(
                r"(?:Your|Current)\s+balance\s*(?:is|:)\s*(.+)", text, re.I
            )
            if match:
                amount = self._parse_amount(match.group(1))
                if amount is not None:
                    return amount
        return None

    def _machine_type_name(self, machine_type_id: int) -> str:
        return "Washer" if machine_type_id == WASHER_TYPE_ID else "Dryer"

    def login(self) -> bool:
        if self._is_login_blocked():
            return False
        try:
            self.logged_in = False
            self.session.cookies.clear()
            init_resp = self.session.get(f"{BASE_URL}/login/index.php", timeout=20)
            init_resp.raise_for_status()
            if self._detect_lockout(init_resp.text):
                return False
            resp = self.session.post(
                f"{BASE_URL}/login/submit.php",
                data={"UserInput": EMAIL, "PwdInput": PASSWORD},
                allow_redirects=False,
                timeout=20,
            )
            resp.raise_for_status()
            if "StartSite.php" in resp.text:
                match = re.search(r"document\.location\s*=\s*'([^']+)'", resp.text)
                if match:
                    redirect = urljoin(f"{BASE_URL}/login/submit.php", match.group(1))
                    if urlparse(redirect).netloc != urlparse(BASE_URL).netloc:
                        raise RuntimeError("Unexpected login redirect host")
                    self.start_url = redirect
                    landing = self.session.get(redirect, timeout=20)
                    landing.raise_for_status()
                    if self._is_auth_redirect(landing):
                        raise RuntimeError("Login redirect was not authenticated")
                else:
                    raise RuntimeError("DUWO did not provide a login destination")
                # LocNR is deliberately NOT set here: it sticks to the PHP
                # session and flips BookingOverview.php from our own bookings
                # to the whole location.  Calls that need it pass
                # init_location=True.
                self._location_set = False
                self.logged_in = True
                self._on_login_success()
                print("[OK] Login successful")
                return True
            # Check for account lockout in the failed response
            if self._detect_lockout(resp.text):
                return False
            print("[FAIL] Login failed")
            self._on_login_failure()
            return False
        except Exception as e:
            print(f"[ERROR] Login: {e}")
            self._on_login_failure()
            return False

    def _init_location(self):
        if self._location_set:
            return
        resp = self.session.get(f"{BASE_URL}/findmachinetypes.php", timeout=20)
        if self._is_auth_redirect(resp):
            raise PermissionError("DUWO session expired")
        resp.raise_for_status()
        if "findmachinetypes" not in resp.text.lower():
            raise RuntimeError("Could not select the laundry room")
        self._location_set = True

    def ensure_login(self):
        if not self.logged_in and not self.login():
            raise RuntimeError(
                "DUWO login unavailable; retry after the login cooldown."
            )

    def _account_home(self) -> requests.Response:
        # main.php alone renders the last selected PHP page, which may be a calendar.
        # Explicitly selecting user.php restores both the credits and personal scope.
        # DUWO can render the previous page once before applying this selection.
        for _ in range(2):
            resp = self._get(f"{BASE_URL}/main.php?page=user.php")
            if re.search(r"ParentFile\s*=\s*['\"]user\.php['\"]", resp.text, re.I):
                self._location_set = False
                return resp
        raise RuntimeError("DUWO did not return the account page")

    def get_availability(self) -> list[MachineStatus]:
        self._clear_error()
        try:
            resp = self._get(f"{BASE_URL}/MachineAvailability.php")
            soup = BeautifulSoup(resp.text, "html.parser")
            table = soup.find("div", id="MachineAvailabilityTable")
            if not table:
                self._set_error("Failed to load machine availability.")
                return []
            machines = []
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) >= 3:
                    status_text = cells[2].get_text(strip=True)
                    count = None
                    m = re.search(r"\bAvailable\s*:\s*(\d+)", status_text, re.I)
                    if m:
                        count = int(m.group(1))
                    elif re.fullmatch(r"Not\s+Available", status_text, re.I):
                        count = 0
                    machines.append(
                        MachineStatus(
                            location=cells[0].get_text(strip=True),
                            machine_type=cells[1].get_text(strip=True),
                            status=status_text,
                            available_count=count,
                        )
                    )
            return machines
        except Exception as exc:
            self._set_error(f"Could not load machine availability: {exc}")
            return []

    def get_booking_slots(
        self, machine_type_id: int, date: str | None = None
    ) -> list[BookingSlot] | None:
        self._clear_error()
        try:
            url = f"{BASE_URL}/FindAvailableFromMachineType.php?ObjectMachineTypeID={machine_type_id}"
            if date:
                url += f"&Start_Date={date}"
            resp = self._get(url, init_location=True)
            soup = BeautifulSoup(resp.text, "html.parser")
            header = soup.find("div", id="CalendarObjectKopTekst")
            blocks = soup.find_all("div", class_="DivCalendarObjectTijdsBlokNotBooked")
            if header is None and not blocks:
                self._set_error(
                    f"Failed to load {self._machine_type_name(machine_type_id).lower()} slots from DUWO."
                )
                return None
            slots = []
            for block in blocks:
                name = block.get("name", "")
                parts = name.split("|")
                if len(parts) != 4:
                    continue
                text = block.get_text()
                avail = 0
                m = re.search(r"Available for booking:\s*(\d+)", text)
                if m:
                    avail = int(m.group(1))
                price = ""
                pm = re.search(r"Pay on delivery\s*:\s*[€\u20ac]?\s*([\d.,]+)", text)
                if pm:
                    price = pm.group(1)
                slots.append(
                    BookingSlot(
                        raw_value=name,
                        location_id=parts[0],
                        date=parts[1],
                        start_time=parts[2],
                        end_time=parts[3],
                        available_count=avail,
                        price=price,
                    )
                )
            if slots:
                self._remember_room_id(slots[0].location_id)
            slots = [
                slot
                for slot in slots
                if slot.available_count > 0
                and datetime.fromisoformat(f"{slot.date}T{slot.end_time}")
                >= datetime.now()
            ]
            return sorted(slots, key=lambda slot: (slot.date, slot.start_time))
        except Exception as exc:
            self._set_error(
                f"Could not load "
                f"{self._machine_type_name(machine_type_id).lower()} slots: {exc}"
            )
            return None

    MAX_BOOKINGS = 10

    def _validate_window(self, machine_type_id: int, date: str, start: str, end: str):
        if machine_type_id not in (WASHER_TYPE_ID, DRYER_TYPE_ID):
            raise ValueError("Unknown machine type.")
        begin = datetime.strptime(f"{date} {start[:5]}", "%Y-%m-%d %H:%M")
        finish = datetime.strptime(f"{date} {end[:5]}", "%Y-%m-%d %H:%M")
        if begin < datetime.now().replace(second=0, microsecond=0):
            raise ValueError("The booking start is in the past.")
        if finish <= begin:
            raise ValueError(
                "The end time must be after the start time on the same day."
            )
        return begin, finish

    @staticmethod
    def _script_value(markup: str, name: str) -> str | None:
        match = re.search(
            r"\b" + re.escape(name) + r"\s*=\s*['\"]([^'\"]*)['\"]", markup
        )
        return match.group(1) if match else None

    def _create_booking(self, slot: BookingSlot, machine_type_id: int) -> str | None:
        self._clear_error()
        submitted = False
        try:
            start, end = self._validate_window(
                machine_type_id, slot.date, slot.start_time, slot.end_time
            )
            before = self.get_own_bookings(include_inactive=True)
            if before is None:
                raise RuntimeError(
                    "Booking aborted: your current bookings could not be loaded."
                )
            known = {b["id"] for b in before}
            # This request selects the machine type in PHP. Nothing that changes
            # account/room scope may run between here and the final submission.
            if self.get_booking_slots(machine_type_id, date=slot.date) is None:
                raise RuntimeError(
                    self.last_error or "Could not select the machine type."
                )
            query = urlencode({"value": slot.raw_value})
            announce = self._get(
                f"{BASE_URL}/AnnouncmentBooking.php?{query}", allow_retry=False
            )
            if (
                not BeautifulSoup(announce.text, "html.parser").find(id="BtnOkBooking")
                or self._script_value(announce.text, "Value") != slot.raw_value
                or self._script_value(announce.text, "ObjectMachineTypeID")
                != str(machine_type_id)
            ):
                raise RuntimeError(
                    "DUWO did not offer confirmation for the requested booking."
                )
            confirm = self._get(
                f"{BASE_URL}/ConfirmCreateBooking.php?{query}", allow_retry=False
            )
            if (
                "CreateBooking.php?value=" not in confirm.text
                or self._script_value(confirm.text, "Value") != slot.raw_value
                or self._script_value(confirm.text, "ObjectMachineTypeID")
                != str(machine_type_id)
            ):
                raise RuntimeError("DUWO rejected the booking confirmation.")
            submitted = True
            self.balance_stale = True
            try:
                self._get(
                    f"{BASE_URL}/CreateBooking.php?{query}",
                    allow_retry=False,
                    allow_http_error=True,
                )
            except (requests.RequestException, RuntimeError):
                # A timeout/HTTP 500 can occur after the commit. Never repeat it.
                pass
            after = self.get_own_bookings(include_inactive=True)
            if after is not None:
                matches = [
                    b
                    for b in after
                    if b["id"] not in known
                    and b["machine"] == self._machine_type_name(machine_type_id)
                    and b["start"] == start
                    and b["end"] == end
                ]
                if len(matches) == 1:
                    self._clear_error()
                    print(f"[OK] Booking confirmed: {matches[0]['id']}")
                    return matches[0]["id"]
            raise RuntimeError(
                "Submission outcome is unconfirmed. Check /bookings before trying again."
            )
        except Exception as exc:
            prefix = (
                "Booking submitted; check /bookings before retrying. "
                if submitted
                else ""
            )
            self._set_error(prefix + str(exc))
            return None

    def book_slot(self, slot: BookingSlot, machine_type_id: int) -> bool:
        return self._create_booking(slot, machine_type_id) is not None

    def book_multiple(self, machine_type_id: int, count: int) -> list[BookingSlot]:
        self._clear_error()
        if not 1 <= count <= self.MAX_BOOKINGS:
            self._set_error(f"Choose between 1 and {self.MAX_BOOKINGS} machines.")
            return []
        slots = self.get_booking_slots(machine_type_id)
        if slots is None:
            return []
        slot = next((s for s in slots if s.available_count >= count), None)
        if slot is None:
            self._set_error(f"No single time window has {count} machines available.")
            return []
        booked = []
        for _ in range(count):
            if not self.book_slot(slot, machine_type_id):
                break
            booked.append(slot)
        return booked

    def get_balance(self) -> str | None:
        """Fetch the account page explicitly; never use history or booking previews."""
        self._clear_error()
        try:
            resp = self._account_home()
            balance = self._extract_balance(resp.text)
            if balance is None:
                raise RuntimeError("DUWO did not display the current balance.")
            self.last_known_balance = balance
            self.balance_checked_at = datetime.now()
            self.balance_stale = False
            return balance
        except Exception as exc:
            self.balance_stale = True
            self._set_error(f"Could not refresh balance: {exc}")
            return self.last_known_balance

    def get_balance_float(self) -> float | None:
        balance = self.get_balance()
        return float(balance) if balance is not None else None

    def cancel_booking(self, res_nr: str) -> bool:
        self._clear_error()
        if not re.fullmatch(r"[0-9]+", res_nr):
            self._set_error("Booking IDs must contain digits only.")
            return False
        submitted = False
        try:
            before = self.get_own_bookings(include_inactive=True)
            if before is None:
                raise RuntimeError(
                    "Cancellation aborted: your bookings could not be loaded."
                )
            booking = next((b for b in before if b["id"] == res_nr), None)
            if booking is None:
                booking = next(
                    (b for b in before if b.get("booking_nr") == res_nr), None
                )
            if booking is None:
                raise RuntimeError(f"Booking {res_nr} was not found in your account.")
            if booking["status"] != "BookingReady" or booking["end"] < datetime.now():
                raise RuntimeError(
                    "Only an active, unused reservation can be cancelled."
                )
            res_nr = booking["id"]
            announce = self._get(
                f"{BASE_URL}/AnnouncmentBooking.php?{urlencode({'ResNr': res_nr})}",
                init_location=True,
            )
            if self._script_value(
                announce.text, "ResNr"
            ) != res_nr or not BeautifulSoup(announce.text, "html.parser").find(
                id="BtnRemoveBooking"
            ):
                raise RuntimeError(
                    "DUWO did not offer cancellation for that reservation."
                )
            submitted = True
            self.balance_stale = True
            try:
                self._get(
                    f"{BASE_URL}/DeleteBooking.php",
                    allow_retry=False,
                    allow_http_error=True,
                )
            except (requests.RequestException, RuntimeError):
                pass
            after = self.get_own_bookings(include_inactive=True)
            if after is None or any(b["id"] == res_nr for b in after):
                raise RuntimeError(
                    "Cancellation is unconfirmed. Check /bookings before retrying."
                )
            self._clear_error()
            print(f"[OK] Cancelled booking {res_nr}")
            return True
        except Exception as exc:
            prefix = "Cancellation submitted; " if submitted else ""
            self._set_error(prefix + str(exc))
            return False

    def create_timed_booking(
        self, machine_type_id: int, date: str, start: str, end: str
    ) -> str | None:
        self._clear_error()
        try:
            self._validate_window(machine_type_id, date, start, end)
            location = self._location_id(machine_type_id, date)
            if location is None:
                raise RuntimeError(
                    self.last_error or "Could not identify the laundry room."
                )
            slot = BookingSlot(
                f"{location}|{date}|{start}:00|{end}", location, date, start, end, 1, ""
            )
            return self._create_booking(slot, machine_type_id)
        except Exception as exc:
            self._set_error(str(exc))
            return None

    def _location_id(self, machine_type_id: int, date: str | None = None) -> str | None:
        """The laundry room id a booking value starts with -- the 45 in
        "45|2026-09-14|17:00:00|17:59".

        DUWO prints it only on *free* calendar blocks, so a day with nothing
        left to book carries it nowhere.  That is precisely when /book_at is
        wanted -- late at night, or on a full room -- which is why reading it
        off "today, whatever is free" used to fail seemingly at random.

        It identifies the room, not the day or the machine, so once seen it is
        remembered for good (in the state file, and overridable by
        DUWO_LOCATION_ID).  Failing that, days more likely to still be empty
        are tried before giving up.
        """
        if self._room_id:
            return self._room_id
        today = datetime.now().date()
        days = [date] + [
            (today + timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in (0, 1, 2)
        ]
        # The id is room-wide, so the other machine type will do just as well.
        types = [machine_type_id] + [
            t for t in (WASHER_TYPE_ID, DRYER_TYPE_ID) if t != machine_type_id
        ]
        for type_id in types:
            for day in dict.fromkeys(d for d in days if d):
                slots = self.get_booking_slots(type_id, date=day)
                if slots:
                    return self._remember_room_id(slots[0].location_id)
                if slots is None:
                    # The calendar did not load at all -- a dead session or a
                    # site error, not a full day.  Other days will fail the
                    # same way, so stop and let the real error stand.
                    return None
        return None

    def _remember_room_id(self, value: str) -> str | None:
        """Keep the first room id we see, and write it through to the state file."""
        if LOCATION_ID:
            # An explicit override outranks whatever the calendar says.
            return LOCATION_ID
        value = (value or "").strip()
        if not value:
            return None
        if value != self._room_id:
            self._room_id = value
            if self._state_store is not None:
                self._state_store.remember_location_id(value)
        return value

    def _ensure_own_scope(self):
        if self._location_set:
            self._account_home()

    def _parse_bookings(self, markup: str, *, own: bool) -> list[dict]:
        table = BeautifulSoup(markup, "html.parser").find(id="BookingOverviewTable")
        if table is None:
            raise ValueError("DUWO did not return a booking overview.")
        now = datetime.now()
        bookings = []
        for row in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
            if not cells:
                continue
            if len(cells) < 4:
                raise ValueError(
                    "Unrecognized booking row; refusing an incomplete schedule."
                )
            day = re.fullmatch(r"(\d{2})-(\d{2})(?:-(\d{4}))?", cells[0])
            window = re.fullmatch(r"(\d{2}):(\d{2})\s*->\s*(\d{2}):(\d{2})", cells[1])
            if not day or not window:
                raise ValueError("Unrecognized booking date or window.")
            machine = self._machine_name(cells[3])
            if machine is None:
                raise ValueError("Unrecognized machine in the booking overview.")
            status = next(
                (
                    name
                    for tag in row.find_all(class_=True)
                    for name in tag.get("class", [])
                    if name.startswith("Booking")
                ),
                "",
            )
            if status not in ("BookingReady", "BookingBusy", "BookingFinished"):
                raise ValueError(
                    "Unrecognized booking status; the schedule is unavailable."
                )
            if day.group(3):
                start = datetime(
                    int(day.group(3)),
                    int(day.group(2)),
                    int(day.group(1)),
                    int(window.group(1)),
                    int(window.group(2)),
                )
            else:
                start = self._booking_datetime(
                    now,
                    int(day.group(2)),
                    int(day.group(1)),
                    int(window.group(1)),
                    int(window.group(2)),
                )
            end = self._booking_end(start, int(window.group(3)), int(window.group(4)))
            entry = dict(
                machine=machine,
                start=start,
                end=end,
                end_label=end.strftime("%H:%M"),
                status=status,
            )
            if own:
                # Only parse the hidden identifiers after restoring account scope.
                markup_row = str(row)
                res = re.search(r"RemoveBooking\(\s*['\"]?(\d+)['\"]?\s*\)", markup_row)
                if not res:
                    raise ValueError(
                        "An account booking has no cancellation identifier."
                    )
                entry["id"] = res.group(1)
                number = re.search(
                    r"RemoveBooking\([^)]*\)[^>]*>\s*<p[^>]*>\s*(\d+)\s*</p>",
                    markup_row,
                )
                entry["booking_nr"] = number.group(1) if number else ""
            bookings.append(entry)
        return sorted(bookings, key=lambda b: b["start"])

    @staticmethod
    def _machine_name(text: str) -> str | None:
        if "wash" in text.lower():
            return "Washer"
        if "dry" in text.lower():
            return "Dryer"
        return None

    def get_own_bookings(self, *, include_inactive: bool = False) -> list[dict] | None:
        self._clear_error()
        try:
            self._ensure_own_scope()
            resp = self._get(f"{BASE_URL}/BookingOverview.php")
            bookings = self._parse_bookings(resp.text, own=True)
            if include_inactive:
                return bookings
            now = datetime.now()
            return [
                b
                for b in bookings
                if (b["status"] == "BookingReady" and b["end"] >= now)
                or (
                    b["status"] == "BookingBusy"
                    and b["end"] >= now - timedelta(minutes=30)
                )
            ]
        except Exception as exc:
            self._set_error(f"Could not load your bookings: {exc}")
            return None

    def get_location_bookings(self) -> list[dict] | None:
        self._clear_error()
        try:
            resp = self._get(f"{BASE_URL}/BookingOverview.php", init_location=True)
            return self._parse_bookings(resp.text, own=False)
        except Exception as exc:
            self._set_error(f"Could not load the room schedule: {exc}")
            return None

    @staticmethod
    def _booking_end(start: datetime, hour: int, minute: int) -> datetime:
        end = start.replace(hour=hour, minute=minute)
        return end + timedelta(days=1) if end < start else end

    @staticmethod
    def _booking_datetime(
        now: datetime, month: int, day: int, hour: int, minute: int
    ) -> datetime:
        candidates = []
        for year in (now.year - 1, now.year, now.year + 1):
            try:
                candidates.append(datetime(year, month, day, hour, minute))
            except ValueError:
                continue
        if not candidates:
            raise ValueError("Invalid booking date.")
        return min(candidates, key=lambda stamp: abs(stamp - now))

    def get_recent_cycles(self) -> list[Cycle] | None:
        """Parse our own machine starts out of UserLog.php.

        UserLog.php is always scoped to the logged-in account.  BookingOverview.php
        is NOT: once LocNR is set in the PHP session (which any init_location=True
        call does, via findmachinetypes.php) it switches to listing the whole
        location's bookings instead of ours, so it must not be used for this.

        A terminal start looks like:

            16:12:28  Depreciation on location  -2,00  Washing Mach.
            16:12:37  Booking Started                  BookingNR : 8005487
            16:12:39  Booking Payd                     8005487 ->  200

        Prefer a payment matched to the exact booking ID (200 = EUR 2.00 =
        washer, 100 = dryer), then use an adjacent machine-specific debit.
        """
        self._clear_error()
        try:
            resp = self._get(f"{BASE_URL}/UserLog.php")
            soup = BeautifulSoup(resp.text, "html.parser")
            rows = []
            for row in soup.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
                if len(cells) < 3:
                    continue
                try:
                    stamp = datetime.strptime(cells[0], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                cells = (cells + [""] * 6)[:6]
                rows.append({"at": stamp, "action": cells[2], "info": cells[5]})
            if not rows and not soup.find(id=re.compile(r"UserLog", re.I)):
                self._set_error("Could not read the DUWO activity log.")
                return None
            rows.sort(key=lambda r: r["at"])
            cutoff = datetime.now() - timedelta(hours=CYCLE_LOOKBACK_HOURS)
            cycles = []
            for index, row in enumerate(rows):
                if row["action"] != "Booking Started" or row["at"] < cutoff:
                    continue
                match = re.search(r"BookingNR\s*:\s*(\d+)", row["info"])
                if not match:
                    continue
                machine = self._machine_from_context(rows, index, match.group(1))
                if machine is None:
                    continue
                cycles.append(
                    Cycle(
                        booking_nr=match.group(1),
                        machine=machine,
                        started_at=row["at"],
                    )
                )
            return cycles
        except Exception as exc:
            self._set_error(f"Could not read the DUWO activity log: {exc}")
            return None

    @staticmethod
    def _machine_from_context(
        rows: list[dict], index: int, booking_nr: str
    ) -> str | None:
        started = rows[index]["at"]
        window = timedelta(minutes=3)
        # Payment rows carry the exact booking ID; adjacent debits can belong
        # to a different machine started at nearly the same time.
        for row in rows:
            if abs(row["at"] - started) > window or row["action"] != "Booking Payd":
                continue
            paid = re.fullmatch(r"\s*(\d+)\s*->\s*(\d+)\s*", row["info"])
            if paid and paid.group(1) == booking_nr:
                machine = {200: "Washer", 100: "Dryer"}.get(int(paid.group(2)))
                if machine:
                    return machine
        for row in reversed(rows[:index]):
            if started - row["at"] > window:
                break
            if row["action"] == "Booking Started":
                break
            if row["action"] == "Depreciation on location":
                return DUWOClient._machine_name(row["info"])
        return None

    def get_qr_text(self) -> str | None:
        """Generate a new QR code and return its text value."""
        self._clear_error()
        try:
            resp = self._get(f"{BASE_URL}/GenUserQrcode.php?GenNew=TRUE")
            m = re.search(r'"text"\s*:\s*"([^"]+)"', resp.text)
            if m:
                return m.group(1)
            self._set_error("DUWO did not return a QR text value.")
            return None
        except Exception:
            self._set_error("get_qr_text failed.")
            return None


# ============== Bot Command Handler ==============


HELP_TEXT = """<b>DUWO Laundry Bot</b>

<b>Commands:</b>
/status - Machine availability
/cycles - Your running machines
/done - Mark your laundry collected
/slots - Washer slots
/slots_dryer - Dryer slots
/book N - Book N washers
/book_dryer N - Book N dryers
/book_at TYPE FROM TO - Reserve any window
/bookings - Your bookings
/cancel ID - Cancel a booking
/balance - Account balance
/qr - Laundry QR code
/help - This message

<b>Examples:</b>
/book 2
/book_at washer 23:17 23:57
/cancel 12345

Cycle alerts are sent for your own machines. Connection alerts can also be sent. DUWO frees a machine well before its door actually opens, so the bot runs its own clock: washer {washer} min, dryer {dryer} min. Send /done when you really open the door and it learns your timings.
""".replace("{washer}", str(WASHER_CYCLE_MINUTES)).replace(
    "{dryer}", str(DRYER_CYCLE_MINUTES)
)


def _day_label(when: datetime, now: datetime) -> str:
    """Compact, readable day for a booking row."""
    delta = when.date() - now.date()
    if delta.days == 0:
        return "today   "
    if delta.days == 1:
        return "tomorrow"
    return when.strftime("%d-%m   ")


def _reservation_state(booking: dict, now: datetime) -> str:
    if booking["status"] == "BookingBusy":
        return "DUWO timer ended" if booking["end"] < now else "running (DUWO)"
    return {"BookingReady": "reserved", "BookingFinished": "finished"}.get(
        booking["status"], "unknown"
    )


def handle_command(cmd: str, duwo: DUWOClient, bot: TelegramBot, tracker: CycleTracker):
    """Process a Telegram command and reply."""
    cmd = cmd.strip()
    lower = re.sub(r"^(/[a-z_]+)@[a-z0-9_]+(?=\s|$)", r"\1", cmd.lower())

    # /help or /start
    if lower in ("/help", "/start"):
        bot.send(HELP_TEXT)
        return

    # /status
    if lower == "/status":
        poll_cycles(duwo, bot, tracker, suppress=not tracker.cycle_baseline_set)
        stamp = datetime.now()
        machines = duwo.get_availability()
        sep = "-" * 32
        rows = [
            f"DUWO availability · {stamp:%d-%m %H:%M}",
            f"{'Type':<18}{'Free':>5}",
            sep,
        ]
        if machines:
            for machine in machines:
                count = (
                    str(machine.available_count)
                    if machine.available_count is not None
                    else "?"
                )
                rows.append(f"{machine.machine_type[:18]:<18}{count:>5}")
        else:
            rows.append("Availability unavailable")
        rows.append("Free counts follow DUWO's timer.")
        balance = duwo.get_balance()
        rows.append(sep)
        if balance is None:
            rows.append("Balance: unavailable")
        elif duwo.balance_stale:
            rows.append(f"Balance: EUR {balance} (last known)")
        else:
            rows.append(f"Balance: EUR {balance}")

        mine = duwo.get_own_bookings()
        rows.extend(["", "YOUR RESERVATIONS"])
        if mine is None:
            rows.append("  unavailable (DUWO read failed)")
        elif not mine:
            rows.append("  nothing booked")
        else:
            for booking in mine:
                state = _reservation_state(booking, stamp)
                rows.append(
                    f"  {booking['machine']} {_day_label(booking['start'], stamp)} "
                    f"{booking['start']:%H:%M}-{booking['end_label']} {state}"
                )

        pending = tracker.pending()
        if pending:
            rows.extend(["", "YOUR CYCLE ESTIMATES"])
            for cycle in pending:
                eta = tracker.eta(cycle)
                state = (
                    f"ready ~{eta:%H:%M}"
                    if eta > stamp
                    else f"expected ready since {eta:%H:%M}"
                )
                rows.append(f"  {cycle.machine} {cycle.started_at:%H:%M}: {state}")

        room = duwo.get_location_bookings()
        rows.extend(
            [
                "",
                "RESERVED BY OTHERS (DUWO overview)"
                if mine is not None
                else "ROOM RESERVATIONS (may include yours)",
            ]
        )
        if room is None:
            rows.append("  unavailable (DUWO read failed)")
        else:
            # Subtract individual reservations, not all machines at the same time.
            owned = Counter(
                (b["machine"], b["start"], b["end"])
                for b in mine or []
                if b["status"] == "BookingReady"
            )
            others = []
            for booking in room:
                if booking["status"] != "BookingReady" or booking["end"] < stamp:
                    continue
                key = (booking["machine"], booking["start"], booking["end"])
                if owned[key]:
                    owned[key] -= 1
                else:
                    others.append(booking)
            for booking in others[:8]:
                rows.append(
                    f"  {booking['machine']} {_day_label(booking['start'], stamp)} "
                    f"{booking['start']:%H:%M}-{booking['end_label']}"
                )
            if len(others) > 8:
                rows.append(f"  ... {len(others) - 8} more")
            if not others:
                rows.append("  no upcoming reservations shown")
        bot.send(f"<pre>{html.escape(chr(10).join(rows))}</pre>")
        return

    # /slots (washers)
    if lower == "/slots":
        slots = duwo.get_booking_slots(WASHER_TYPE_ID)
        if slots is None:
            bot.send(
                html.escape(duwo.last_error or "Failed to load washer slots from DUWO.")
            )
            return
        if not slots:
            bot.send("No washer slots available.")
            return
        lines = ["<b>Washer slots:</b>", ""]
        for s in slots:
            lines.append(
                f"{s.date} {s.start_time[:5]}-{s.end_time} "
                f"| free: {s.available_count} | €{html.escape(s.price)}"
            )
        lines.append("")
        lines.append("Reply /book N to book N washers for the earliest slot.")
        bot.send("\n".join(lines))
        return

    # /slots_dryer
    if lower == "/slots_dryer":
        slots = duwo.get_booking_slots(DRYER_TYPE_ID)
        if slots is None:
            bot.send(
                html.escape(duwo.last_error or "Failed to load dryer slots from DUWO.")
            )
            return
        if not slots:
            bot.send("No dryer slots available.")
            return
        lines = ["<b>Dryer slots:</b>", ""]
        for s in slots:
            lines.append(
                f"{s.date} {s.start_time[:5]}-{s.end_time} "
                f"| free: {s.available_count} | €{html.escape(s.price)}"
            )
        lines.append("")
        lines.append("Reply /book_dryer N to book N dryers for the earliest slot.")
        bot.send("\n".join(lines))
        return

    # /balance
    if lower == "/balance":
        bal = duwo.get_balance()
        if bal is None:
            bot.send(
                html.escape(duwo.last_error or "Failed to load balance from DUWO.")
            )
        elif duwo.balance_stale:
            bot.send(
                f"Balance: EUR {bal} (last known)\n"
                f"{html.escape(duwo.last_error or 'DUWO did not answer just now.')}"
            )
        else:
            bot.send(f"Balance: EUR {bal}")
        return

    # /bookings (must be before /book to avoid regex collision)
    if lower == "/bookings":
        bookings = duwo.get_own_bookings()
        if bookings is None:
            bot.send(
                html.escape(duwo.last_error or "Failed to load bookings from DUWO.")
            )
            return
        upcoming = [b for b in bookings if b["status"] != "BookingFinished"]
        if not upcoming:
            bot.send("No active bookings.")
            return
        lines = ["<b>Your bookings:</b>", ""]
        stamp = datetime.now()
        for b in upcoming:
            state = _reservation_state(b, stamp)
            when = b["start"].strftime("%d-%m %H:%M")
            cancel_id = (
                f" | ID: <code>{html.escape(b['id'])}</code>"
                if b["status"] == "BookingReady"
                else ""
            )
            lines.append(
                f"  {b['machine']} | {when}-{b['end_label']} | {state}{cancel_id}"
            )
        if any(b["status"] == "BookingReady" for b in upcoming):
            lines.extend(["", "Cancel an unused reservation: /cancel ID"])
        bot.send("\n".join(lines))
        return

    # /cancel ID
    m = re.match(r"/cancel\s+([0-9]+)\s*$", lower)
    if m:
        res_nr = m.group(1)
        bot.send(f"Cancelling booking {res_nr}...")
        if duwo.cancel_booking(res_nr):
            bot.send(f"[OK] Booking {res_nr} cancelled.")
        else:
            bot.send(
                html.escape(
                    f"[FAIL] {duwo.last_error or f'Could not cancel booking {res_nr}.'}"
                )
            )
        return

    # /cancel without args
    if lower.startswith("/cancel"):
        bot.send("Usage: /cancel ID\nSend /bookings to see your booking IDs.")
        return

    # /book_at TYPE HH:MM HH:MM [YYYY-MM-DD]
    m = re.match(
        r"/book_at\s+(washer|dryer)\s+(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})"
        r"(?:\s+(\d{4}-\d{2}-\d{2}))?\s*$",
        lower,
    )
    if m:
        kind, start_raw, end_raw, date_raw = m.groups()
        try:
            start_t = datetime.strptime(start_raw, "%H:%M").time()
            end_t = datetime.strptime(end_raw, "%H:%M").time()
        except ValueError:
            bot.send("Times must look like HH:MM, e.g. /book_at washer 23:17 23:57")
            return
        now = datetime.now()
        if date_raw:
            try:
                day = datetime.strptime(date_raw, "%Y-%m-%d").date()
            except ValueError:
                bot.send("Date must look like YYYY-MM-DD.")
                return
        else:
            day = now.date()
            if datetime.combine(day, start_t) < now.replace(second=0, microsecond=0):
                day = day + timedelta(days=1)
        if datetime.combine(day, end_t) <= datetime.combine(day, start_t):
            bot.send("The end time must be after the start time.")
            return

        type_id = WASHER_TYPE_ID if kind == "washer" else DRYER_TYPE_ID
        start_s = start_t.strftime("%H:%M")
        end_s = end_t.strftime("%H:%M")
        date_s = day.strftime("%Y-%m-%d")
        bot.send(f"Reserving {kind} {date_s} {start_s}-{end_s}...")
        booking_nr = duwo.create_timed_booking(type_id, date_s, start_s, end_s)
        if booking_nr:
            bot.send(
                f"[OK] Reserved {kind} {date_s} {start_s}-{end_s}\n"
                f"Cancel ID: <code>{html.escape(booking_nr)}</code>\n"
                f"Use /bookings to view it, or /cancel {html.escape(booking_nr)}."
            )
        else:
            bot.send(
                f"[FAIL] {html.escape(duwo.last_error or 'Could not reserve that window.')}"
            )
        return

    # /book_at without usable arguments
    if lower.startswith("/book_at"):
        bot.send(
            "Usage: /book_at washer|dryer HH:MM HH:MM [YYYY-MM-DD]\n"
            "Examples: /book_at washer 23:17 23:57\n"
            "          /book_at dryer 19:30 20:10 2026-09-16\n\n"
            "Books any window, not just whole hours. "
            "Defaults to today, or tomorrow if that time has passed."
        )
        return

    # /book N and /book_dryer N reserve one shared time window.
    match = re.fullmatch(
        r"/book(?P<kind>_washer|_dryer)?(?:\s+(?P<count>[0-9]{1,3}))?", lower
    )
    if match:
        count = int(match.group("count") or "1")
        kind = "dryer" if match.group("kind") == "_dryer" else "washer"
        type_id = DRYER_TYPE_ID if kind == "dryer" else WASHER_TYPE_ID
        if not 1 <= count <= DUWOClient.MAX_BOOKINGS:
            bot.send(f"Choose between 1 and {DUWOClient.MAX_BOOKINGS} machines.")
            return
        bot.send(f"Booking {count} {kind}(s)...")
        booked = duwo.book_multiple(type_id, count)
        if booked:
            label = "OK" if len(booked) == count else "PARTIAL"
            lines = [f"[{label}] Booked {len(booked)} of {count} {kind}(s):"]
            for slot in booked:
                lines.append(f"  {slot.date} {slot.start_time[:5]}-{slot.end_time}")
            if len(booked) < count:
                lines.append(
                    duwo.last_error or "The remaining reservations were not confirmed."
                )
            lines.append("Use /bookings for cancellation IDs.")
            bot.send(html.escape("\n".join(lines)))
        else:
            bot.send(html.escape(f"[FAIL] {duwo.last_error or 'No slots available.'}"))
        return
    if lower.startswith("/book"):
        bot.send(
            "Usage: /book N or /book_dryer N. All machines use the same time window."
        )
        return

    # /qr
    if lower == "/qr":
        qr_text = duwo.get_qr_text()
        if not qr_text:
            bot.send(html.escape(duwo.last_error or "Failed to get QR code."))
            return
        if qrcode is None:
            bot.send(
                f"QR text: <code>{html.escape(qr_text)}</code>\n(qrcode lib not installed)"
            )
            return
        img = qrcode.make(qr_text)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        bot.send_photo(buf.getvalue(), caption="Laundry QR Code")
        return

    # /cycles - what of ours is running right now
    if lower == "/cycles":
        poll_cycles(duwo, bot, tracker, suppress=not tracker.cycle_baseline_set)
        pending = tracker.pending()
        if not pending:
            bot.send("Nothing of yours is running.")
            return
        stamp = datetime.now()
        lines = ["<b>Your machines:</b>", ""]
        for cycle in pending:
            eta = tracker.eta(cycle)
            left = (eta - stamp).total_seconds() / 60.0
            if left > 0:
                state = f"estimated ready ~{eta.strftime('%H:%M')} ({math.ceil(left)} min left)"
            else:
                state = f"expected ready since {eta.strftime('%H:%M')}"
            lines.append(
                f"  {cycle.machine} started {cycle.started_at.strftime('%H:%M')} - {state}"
            )
        lines.append("")
        lines.append("Send /done once you have actually opened the door.")
        bot.send("\n".join(lines))
        return

    # /done - confirm collection, and calibrate from it
    if lower == "/done":
        poll_cycles(duwo, bot, tracker, suppress=not tracker.cycle_baseline_set)
        batch, observed, learned = tracker.record_collected(datetime.now())
        if not batch:
            bot.send(
                "No eligible load is waiting for /done (at least 15 minutes after starting)."
            )
            return
        tracker.save()
        machines = ", ".join(sorted({c.machine for c in batch}))
        parts = [f"Marked {len(batch)} machine(s) collected ({machines})."]
        if observed is not None:
            parts.append(f"That was {observed:.0f} min after starting.")
        if learned is not None:
            parts.append(f"{batch[-1].machine} estimate is now {learned:.0f} min.")
        bot.send(" ".join(parts))
        return

    # Unknown
    bot.send("Unknown command. Send /help for available commands.")


# ============== Main Loop ==============


def poll_cycles(
    duwo: DUWOClient, bot: TelegramBot, tracker: CycleTracker, suppress: bool
) -> bool:
    """Keep persisted clocks running even when the website cannot be reached."""
    observed = duwo.get_recent_cycles()
    stamp = datetime.now()
    changed = tracker.prune(stamp)
    if observed is None:
        print(f"[WARN] {duwo.last_error or 'cycle poll failed'}")
    else:
        changed = bool(tracker.merge(observed, suppress=suppress)) or changed
        if not tracker.cycle_baseline_set:
            tracker.cycle_baseline_set = True
            changed = True
        changed = tracker.prune(stamp) or changed

    for cycle in tracker.pending():
        eta = tracker.eta(cycle)
        if not cycle.notified_start:
            if stamp >= eta or bot.send(
                f"<b>{cycle.machine} started</b>\n\n"
                f"Started: {cycle.started_at:%H:%M}\n"
                f"Estimated ready: about {eta:%H:%M}"
            ):
                cycle.notified_start = True
                changed = True
            if stamp < eta:
                continue
        if not cycle.notified_headsup and stamp >= eta - timedelta(
            minutes=CYCLE_HEADSUP_MINUTES
        ):
            if stamp >= eta or bot.send(
                f"<b>{cycle.machine} almost done</b>\n\n"
                f"Estimated ready around {eta:%H:%M} — time to head down."
            ):
                cycle.notified_headsup = True
                changed = True
        if not cycle.notified_done and stamp >= eta:
            learns = cycle.machine in CycleTracker.CALIBRATED
            if bot.send(
                f"<b>{cycle.machine} should be ready</b>\n\n"
                f"Started {cycle.started_at:%H:%M}; estimated finish {eta:%H:%M}."
                + ("\nSend /done once you have actually opened it." if learns else "")
            ):
                cycle.notified_done = True
                if not learns:
                    cycle.collected_at = eta
                changed = True
                print(
                    f"[CYCLE] {cycle.machine} {cycle.booking_nr} ready notification delivered"
                )
    if changed:
        tracker.save()
    return observed is not None


def run():
    """Main entry point: monitor + interactive Telegram bot."""
    print("=" * 50)
    print("  DUWO Laundry Bot")
    print(f"  Check interval: {CHECK_INTERVAL}s")
    print(
        f"  Cycle estimate: washer {WASHER_CYCLE_MINUTES}min, dryer {DRYER_CYCLE_MINUTES}min"
    )
    print(f"  Head-up before ready: {CYCLE_HEADSUP_MINUTES}min")
    print(f"  State file: {STATE_PATH}")
    print(f"  Availability alerts: {'on' if NOTIFY_AVAILABILITY else 'off'}")
    print(f"  Low balance alerts: {'on' if NOTIFY_LOW_BALANCE else 'off'}")
    print("=" * 50)

    tracker = CycleTracker(STATE_PATH)
    bot = TelegramBot(store=tracker)
    duwo = DUWOClient(notify_callback=bot.send, store=tracker)

    bot.set_commands()

    if not tracker.cycle_baseline_set:
        print("[INFO] No state file yet: existing log entries will not be announced.")

    last_washer = None
    last_dryer = None
    last_check = 0
    last_cycle_check = 0
    last_balance_check = 0
    low_balance_notified = False

    while True:
        try:
            # 1. Process Telegram commands
            for cmd in bot.poll_commands():
                cmd_name = cmd.split()[0] if cmd else cmd
                print(f"[CMD] {cmd_name}")
                try:
                    handle_command(cmd, duwo, bot, tracker)
                except Exception:
                    traceback.print_exc()
                    bot.send("Something went wrong handling that command.")

            now = time.time()

            # 2. Track our own machines -- this is the job that matters
            if now - last_cycle_check >= CHECK_INTERVAL:
                last_cycle_check = now
                poll_cycles(duwo, bot, tracker, suppress=not tracker.cycle_baseline_set)

            # 3. Availability alerts, off unless NOTIFY_AVAILABILITY is set
            if NOTIFY_AVAILABILITY and now - last_check >= CHECK_INTERVAL:
                last_check = now
                ts = datetime.now().strftime("%H:%M:%S")
                machines = duwo.get_availability()

                if not machines:
                    print(f"[{ts}] Failed to get status")
                    last_check = now - CHECK_INTERVAL + 15
                    continue

                washer = next(
                    (m for m in machines if "wash" in m.machine_type.lower()), None
                )
                dryer = next(
                    (m for m in machines if "dry" in m.machine_type.lower()), None
                )
                w = washer.available_count if washer else None
                d = dryer.available_count if dryer else None

                print(f"[{ts}] Washer: {w} | Dryer: {d}")

                # Notify on current machine status thresholds
                if last_washer is not None and w is not None:
                    if last_washer < WASHER_NOTIFY_THRESHOLD <= w:
                        bot.send(
                            f"<b>Washers available now</b>\n\n"
                            f"Current free washers: {w}\n"
                            f"Alert threshold: {WASHER_NOTIFY_THRESHOLD}\n\n"
                            f"Use /status to check current machine status."
                        )
                    elif last_washer > 0 and w == 0:
                        bot.send("All washers now occupied.")

                if last_dryer is not None and d is not None:
                    if last_dryer < DRYER_NOTIFY_THRESHOLD <= d:
                        bot.send(
                            f"<b>Dryers available now</b>\n\n"
                            f"Current free dryers: {d}\n"
                            f"Alert threshold: {DRYER_NOTIFY_THRESHOLD}\n\n"
                            f"Use /status to check current machine status."
                        )
                    elif last_dryer > 0 and d == 0:
                        bot.send("All dryers now occupied.")

                last_washer = w
                last_dryer = d

            # 4. Low-balance alert, off unless NOTIFY_LOW_BALANCE is set
            if NOTIFY_LOW_BALANCE and now - last_balance_check >= 600:
                last_balance_check = now
                bal = duwo.get_balance_float()
                if bal is not None and not duwo.balance_stale:
                    if bal < LOW_BALANCE_THRESHOLD and not low_balance_notified:
                        delivered = bot.send(
                            f"<b>Low balance: EUR {bal:.2f}</b>\n\n"
                            f"Top up on the DUWO website (Add Credits → Upgrade online, min EUR 20)."
                        )
                        low_balance_notified = delivered
                    elif bal >= LOW_BALANCE_THRESHOLD:
                        low_balance_notified = False
                elif duwo.last_error:
                    print("[INFO] Skipping balance alert")

        except KeyboardInterrupt:
            bot.send("DUWO Laundry Bot stopped.")
            print("\n[INFO] Stopped")
            break
        except Exception:
            traceback.print_exc()
            print("[ERROR] Main loop error")
            # Don't blindly reset logged_in — let ensure_login/login
            # handle re-auth with proper cooldown on next iteration.

        time.sleep(2)


if __name__ == "__main__":
    run()


def main():
    run()
