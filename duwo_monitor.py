#!/usr/bin/env python3
"""DUWO Laundry Monitor - Telegram bot that monitors and books laundry machines."""

import html
import io
import math
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse
import json
import os
import re
import time
import traceback
from contextlib import contextmanager
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
                _v = _v.strip()
                # Accept KEY="value" the way docker compose's env_file does.
                if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in "\"'":
                    _v = _v[1:-1]
                os.environ.setdefault(_k.strip(), _v)

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
# DUWO keeps one session per account: logging in on the phone ends the bot's
# session and the bot's next login ends the phone's. After losing its session
# the bot leaves it to the owner this long before background checks log in.
SESSION_YIELD_MINUTES = max(0, _env_int("SESSION_YIELD_MINUTES", 15))

# The laundry room id that every booking value starts with (the 45 in
# "45|2026-09-14|17:00:00|17:59").  Normally discovered from the calendar and
# remembered, so this is only an escape hatch for when DUWO stops printing it.
LOCATION_ID = (os.environ.get("DUWO_LOCATION_ID") or "").strip()
if LOCATION_ID and not re.fullmatch(r"[0-9]+", LOCATION_ID):
    print(f"[WARN] DUWO_LOCATION_ID={LOCATION_ID!r} is not a number; detecting it")
    LOCATION_ID = ""

WASHER_TYPE_ID = 93
DRYER_TYPE_ID = 94
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# DUWO's attack filter blocks the whole internet connection for hours when a
# request carries a character it does not accept. The login page strips these
# from the e-mail address and password before submitting, so the bot does too.
LOGIN_FORBIDDEN = re.compile(r"[\"'`,/\\]")
# Every value the bot puts in a URL is a number, a date, a time or a booking
# value joined by "|"; anything else is refused before it is sent.
SAFE_QUERY_NAME = re.compile(r"[A-Za-z0-9_]+")
SAFE_QUERY_VALUE = re.compile(r"[A-Za-z0-9 ._@|:+-]*")


class DuwoUnavailable(RuntimeError):
    """DUWO cannot be asked right now; the message says why and until when."""


class DuwoBlocked(DuwoUnavailable):
    """DUWO refuses this connection or this account for a while."""


class DuwoPaused(DuwoUnavailable):
    """Background checks leave the DUWO session to the owner for a while."""


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
class CalendarBlock:
    """One hour of DUWO's booking calendar for one machine type."""

    start: datetime
    end: datetime
    free: int  # bookable places left
    yours: bool = False  # this account holds a booking in it
    slot: BookingSlot | None = None  # the booking value, while places are left


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


def _after(stamp: datetime, minutes: float) -> datetime:
    """Local time `minutes` of real time after `stamp`, even across DST."""
    return datetime.fromtimestamp(stamp.timestamp() + minutes * 60)


def _minutes_between(start: datetime, end: datetime) -> float:
    """Real minutes from `start` to `end`, even across DST."""
    return (end.timestamp() - start.timestamp()) / 60.0


class CycleTracker:
    """Runs our own clock over our own machines, and learns the real duration.

    DUWO frees a machine on a fixed timer measured from the terminal start
    (washer 35 min, dryer 40 min) which expires well before the door does, so
    its "finished" is not a usable "go collect it" signal.  We anchor instead
    on the machine start -- which DUWO's activity log reports to the minute --
    and count forward by a per-type duration that /done feedback calibrates.
    """

    EWMA_ALPHA = 0.4
    # The dryer really does run its advertised 40 minutes, so only the washer
    # -- whose door lags DUWO's timer by ~20 min -- is worth learning.
    CALIBRATED = ("Washer",)
    MIN_MINUTES = 15.0
    MAX_MINUTES = 150.0
    # A /done far from the estimate is a late or early tap, not a cycle time.
    PLAUSIBLE = (0.6, 1.5)
    # Washers this close to their estimated finish are emptied with the batch.
    DUE_SOON = timedelta(minutes=5)
    # A machine first seen this long after its estimated finish is history.
    LATE_GRACE = timedelta(minutes=30)

    def __init__(self, path: str):
        self.path = path
        self.cycles: dict[str, Cycle] = {}
        self.configured = {
            "Washer": float(WASHER_CYCLE_MINUTES),
            "Dryer": float(DRYER_CYCLE_MINUTES),
        }
        self.duration = dict(self.configured)
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
        if re.fullmatch(r"[0-9]+", stored_location):
            self.location_id = stored_location
        durations = data.get("duration")
        bases = data.get("duration_base")
        bases = bases if isinstance(bases, dict) else {}
        for machine, value in (
            durations if isinstance(durations, dict) else {}
        ).items():
            if machine not in self.CALIBRATED:
                # Not learned, so the configured value wins over whatever an
                # older state file happens to hold.
                continue
            base = bases.get(machine)
            if isinstance(base, (int, float)) and float(base) != self.configured[machine]:
                # The configured duration was changed on purpose since this
                # value was learned: start again from the new setting.
                print(f"[INFO] {machine} duration reset to the configured value")
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
                or not re.fullmatch(r"[0-9]+", cycle.booking_nr)
            ):
                continue
            if cycle.started_at.tzinfo is not None:
                cycle.started_at = datetime.fromtimestamp(cycle.started_at.timestamp())
            if cycle.collected_at and cycle.collected_at.tzinfo is not None:
                cycle.collected_at = datetime.fromtimestamp(
                    cycle.collected_at.timestamp()
                )
            if cycle.started_at.timestamp() > time.time() + 60:
                continue
            self.cycles[cycle.booking_nr] = cycle
        return True

    def save(self):
        payload = {
            "telegram_offset": self.telegram_offset,
            "cycle_baseline_set": self.cycle_baseline_set,
            "duration": self.duration,
            # The configured value each learned duration started from.
            "duration_base": {m: self.configured[m] for m in self.CALIBRATED},
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
        return _after(cycle.started_at, self.minutes_for(cycle.machine))

    def merge(self, observed: list[Cycle], suppress: bool) -> list[Cycle]:
        """Register cycles we have not seen before; return the new ones.

        On the very first run there is no state file, so every cycle still in
        the log would fire a burst of stale notifications.  `suppress` marks
        them as already announced instead. A machine first seen long after its
        estimated finish (the bot was down) is closed without announcing it,
        so it neither sends a stale message nor waits for a /done.
        """
        added = []
        now = datetime.now()
        for cycle in observed:
            if cycle.booking_nr in self.cycles:
                continue
            eta = self.eta(cycle)
            if now >= eta + self.LATE_GRACE:
                cycle.notified_start = cycle.notified_headsup = True
                cycle.notified_done = True
                cycle.collected_at = eta
            elif suppress:
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
        """Close out the load the owner just emptied, and learn from it.

        The anchor is the washer whose estimated finish is nearest to `now`:
        the one the last "should be ready" message was about. It is closed
        with every washer started before it and any due within DUE_SOON, which
        covers loads run back to back; a washer started well after it keeps
        running. Only the anchor teaches the estimate, and only when its time
        is plausible. Dryers close by themselves and are not included.
        """
        self.prune(now)
        eligible = [
            c
            for c in self.pending()
            if c.machine in self.CALIBRATED
            and _minutes_between(c.started_at, now) >= self.MIN_MINUTES
        ]
        if not eligible:
            return [], None, None
        anchor = min(
            eligible, key=lambda c: abs(_minutes_between(self.eta(c), now))
        )
        batch = [
            c
            for c in eligible
            if c.started_at <= anchor.started_at or self.eta(c) <= now + self.DUE_SOON
        ]
        observed = _minutes_between(anchor.started_at, now)
        previous = self.minutes_for(anchor.machine)
        learned = None
        low, high = self.PLAUSIBLE
        if low * previous <= observed <= high * previous:
            learned = self._clamp(
                self.EWMA_ALPHA * observed + (1 - self.EWMA_ALPHA) * previous
            )
            self.duration[anchor.machine] = learned
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
                cycle.collected_at = self.eta(cycle)

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
        self.username: str | None = None  # learned from getMe

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

    def poll_commands(self) -> list[tuple[str, datetime | None]]:
        """New commands from the owner's chat, each with when it was sent."""
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
                    sent = msg.get("date")
                    sent_at = (
                        datetime.fromtimestamp(sent)
                        if type(sent) is int and sent > 0
                        else None
                    )
                    commands.append((text, sent_at))
            return commands
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            print(f"[TG] poll error: {type(exc).__name__}")
            return []

    def fetch_username(self) -> str | None:
        """Learn the bot's own @name, so "/status@ThisBot" in a group works."""
        try:
            resp = requests.get(f"{TG_API}/getMe", timeout=10)
            if self._validate_response("getMe", resp):
                name = resp.json().get("result", {}).get("username")
                if isinstance(name, str) and name:
                    self.username = name
        except (requests.RequestException, ValueError, AttributeError) as exc:
            print(f"[WARN] Could not read the bot's own name: {type(exc).__name__}")
        return self.username

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
    LOGIN_RETRY_MAX = 300  # cap for network errors: 5 minutes
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
        self._login_alerted = False
        self._login_cooldown_until = 0.0  # timestamp: don't attempt login before this
        # Set while the main loop polls on its own, rather than for a command.
        self.background = False
        self._yield_until = 0.0  # background logins wait until this timestamp
        # A block notice from DUWO pauses every request until it lifts.
        self._blocked_until = 0.0
        self._block_message = ""
        self._last_warning = ("", 0.0)
        self._notify = notify_callback  # optional callable(str) for Telegram alerts
        self._location_set = False  # has LocNR been set in this session?
        self._state_store = store  # CycleTracker, for the room id
        self._log_unreadable = False  # alerted that UserLog.php cannot be parsed
        self._unattributed: set[str] = set()  # starts with an unknown machine
        self._room_id = LOCATION_ID or (store.location_id if store else None)

    def _clear_error(self):
        self.last_error = None

    @contextmanager
    def in_background(self):
        """Mark requests made by the main loop rather than by a command."""
        self.background = True
        try:
            yield
        finally:
            self.background = False

    def _yield_session(self):
        """Leave the account to the owner after DUWO ended our session.

        DUWO keeps one session per account, so the usual reason is a login on
        the owner's phone; logging straight back in would sign them out.
        """
        if SESSION_YIELD_MINUTES <= 0:
            return
        self._yield_until = time.time() + SESSION_YIELD_MINUTES * 60
        resume = datetime.fromtimestamp(self._yield_until)
        print(
            "[INFO] DUWO ended the session, most likely a login elsewhere; "
            f"background checks resume at {resume:%H:%M}"
        )

    def _set_error(self, message: str):
        self.last_error = message
        # A block or an outage repeats the same warning every poll; log it once
        # per half hour instead of filling the container log.
        previous, logged_at = self._last_warning
        if message != previous or time.time() - logged_at >= 1800:
            print(f"[WARN] {message}")
            self._last_warning = (message, time.time())

    def _send_alert(self, message: str):
        """Send a Telegram notification if callback is configured."""
        if self._notify:
            try:
                self._notify(message)
            except Exception:
                pass

    def _is_login_blocked(self) -> bool:
        """Check if we should NOT attempt login due to cooldown or a block."""
        now = time.time()
        if self._blocked_until > now:
            return True
        if self._login_cooldown_until > now:
            remaining = int(self._login_cooldown_until - now)
            print(f"[WAIT] Login cooldown, {remaining}s remaining")
            return True
        return False

    def _on_login_success(self):
        """Reset cooldown state after successful login."""
        if self._login_alerted:
            self._send_alert("Login restored successfully.")
        self._login_fail_count = 0
        self._login_alerted = False
        self._login_cooldown_until = 0.0

    def _on_login_failure(self, rejected: bool):
        """Apply exponential backoff after a login failure.

        Only a login DUWO actually refused is worth a message; an unreachable
        website is already visible in the log and in every command's reply.
        """
        self._login_fail_count += 1
        cooldown = min(
            self.LOGIN_COOLDOWN_BASE
            * (self.LOGIN_COOLDOWN_MULTIPLIER ** min(self._login_fail_count - 1, 10)),
            # Keep retrying an unreachable website often enough to notice soon
            # that it is back; only refused logins back off all the way.
            self.LOGIN_COOLDOWN_MAX if rejected else self.LOGIN_RETRY_MAX,
        )
        self._login_cooldown_until = time.time() + cooldown
        print(
            f"[WAIT] Login failed ({self._login_fail_count}x), next attempt in {int(cooldown)}s"
        )
        if rejected and not self._login_alerted:
            self._login_alerted = True
            self._send_alert(
                f"<b>Login failed</b>\nDUWO did not accept the login. "
                f"Next attempt in {int(cooldown)}s.\n"
                f"If this persists, check your credentials."
            )

    BLOCK_DEFAULT_SECONDS = {"connection": 3 * 3600, "login": 1800, "account": 6 * 3600}

    def _check_blocked(self, markup: str):
        """Recognise DUWO's block notices and pause all requests until they lift.

        Instead of the requested page DUWO then answers with a short notice:
        "Temporarily blocked" (error code 101) for the whole internet
        connection, "Your account is blocked" (error code 101-<number>) for
        the account, or a lock after too many logins. A request its filter
        rejects first gets "report abuse to admin", and the block follows.
        """
        lower = markup.lower()
        if not any(
            hint in lower
            for hint in ("blockmessage", "report abuse", "locked after too many")
        ):
            return
        soup = BeautifulSoup(markup, "html.parser")
        notice = soup.find(class_="BlockMessage") or soup
        text = " ".join(notice.get_text(" ").split())
        code = re.search(r"error\s+code\s*:?\s*(101(?:\s*-\s*\d+)?)", text, re.I)
        code = re.sub(r"\s+", "", code.group(1)) if code else "101"
        if "blockmessage" not in lower and "report abuse" in lower:
            kind = "connection"
            message = (
                "DUWO rejected a request as unsafe and will block this internet "
                "connection for a few hours."
            )
            until = time.time() + 1800
        else:
            if "-" in code or re.search(r"account\s+is\s+blocked", text, re.I):
                kind = "account"
            elif re.search(r"too\s+many|locked\s+after", text, re.I):
                kind = "login"
            else:
                kind = "connection"
            until = self._unblock_time(text) or (
                time.time() + self.BLOCK_DEFAULT_SECONDS[kind]
            )
            when = datetime.fromtimestamp(until)
            stamp = when.strftime(
                "%H:%M" if when.date() == datetime.now().date() else "%d-%m %H:%M"
            )
            message = {
                "connection": (
                    f"DUWO has temporarily blocked this internet connection "
                    f"(error code {code}) until {stamp}. Another device or app on "
                    f"the same connection can cause this too."
                ),
                "login": f"DUWO locked the login after too many attempts until {stamp}.",
                "account": (
                    f"DUWO has blocked this account (error code {code}). Ask the "
                    f"administrator of your location to lift it; the bot tries "
                    f"again at {stamp}."
                ),
            }[kind]
        is_new = message != self._block_message or until > self._blocked_until + 60
        self._blocked_until = until
        self._block_message = message
        if is_new:
            print(f"[BLOCK] {message}")
            self._send_alert(f"<b>DUWO access paused</b>\n{html.escape(message)}")
        raise DuwoBlocked(message)

    @staticmethod
    def _unblock_time(text: str) -> float | None:
        """The "try again after 21:05" time of a block notice, as a timestamp.

        A time that passed only a little while ago means the notice is still
        being shown right at the end of the block, so retry shortly instead of
        waiting until the same time tomorrow.
        """
        match = re.search(
            r"(?:try\s+again|after)\D{0,20}?(\d{1,2}):(\d{2})(?::(\d{2}))?", text, re.I
        )
        if not match:
            return None
        hour, minute, second = (int(part or 0) for part in match.groups())
        if hour > 23 or minute > 59 or second > 59:
            return None
        now = datetime.now()
        unlock = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if unlock <= now - timedelta(hours=6):
            unlock += timedelta(days=1)
        elif unlock <= now:
            unlock = now + timedelta(minutes=5)
        return unlock.timestamp()

    def _raise_if_blocked(self):
        if time.time() < self._blocked_until:
            raise DuwoBlocked(self._block_message)

    def _block_lifted(self):
        """Report once that DUWO answers normally again after a block."""
        if self._block_message:
            print("[OK] DUWO answers again")
            self._send_alert("DUWO answers again; the block has been lifted.")
            self._block_message = ""
            self._blocked_until = 0.0

    @staticmethod
    def _check_outgoing(url: str):
        """Refuse to send a parameter DUWO's attack filter could block us for."""
        for name, value in parse_qsl(urlparse(url).query, keep_blank_values=True):
            if not SAFE_QUERY_NAME.fullmatch(name) or not SAFE_QUERY_VALUE.fullmatch(
                value
            ):
                raise ValueError(
                    f"Refusing to send an unexpected {name!r} value to DUWO."
                )

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
        self._check_outgoing(url)
        self.ensure_login()
        try:
            if init_location:
                self._init_location()
            resp = self._fetch(url)
        except PermissionError:
            self.logged_in = False
            if self.background:
                self._yield_session()
            if not allow_retry:
                raise RuntimeError(
                    "DUWO session expired; the operation was not retried."
                )
            self.ensure_login()
            try:
                if init_location:
                    self._init_location()
                resp = self._fetch(url)
            except PermissionError:
                # Back off as for a failed login, so a session DUWO keeps
                # dropping cannot turn every read into two more logins.
                self.logged_in = False
                self._on_login_failure(rejected=False)
                raise RuntimeError("DUWO rejected the renewed session.")
        if not allow_http_error:
            resp.raise_for_status()
        return resp

    def _fetch(self, url: str) -> requests.Response:
        """One GET that stops at a block notice and reports a lost session."""
        self._raise_if_blocked()
        resp = self.session.get(url, timeout=20)
        self._check_blocked(resp.text)
        self._block_lifted()
        if self._is_auth_redirect(resp):
            raise PermissionError("DUWO session expired")
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
            self._check_blocked(init_resp.text)
            resp = self.session.post(
                f"{BASE_URL}/login/submit.php",
                data={
                    "UserInput": LOGIN_FORBIDDEN.sub("", EMAIL),
                    "PwdInput": LOGIN_FORBIDDEN.sub("", PASSWORD),
                },
                allow_redirects=False,
                timeout=20,
            )
            resp.raise_for_status()
            self._check_blocked(resp.text)
            if "StartSite.php" in resp.text:
                match = re.search(r"document\.location\s*=\s*'([^']+)'", resp.text)
                if match:
                    redirect = urljoin(f"{BASE_URL}/login/submit.php", match.group(1))
                    if urlparse(redirect).netloc != urlparse(BASE_URL).netloc:
                        raise RuntimeError("Unexpected login redirect host")
                    self.start_url = redirect
                    landing = self.session.get(redirect, timeout=20)
                    landing.raise_for_status()
                    self._check_blocked(landing.text)
                    if self._is_auth_redirect(landing):
                        raise RuntimeError("Login redirect was not authenticated")
                else:
                    raise RuntimeError("DUWO did not provide a login destination")
                # LocNR is deliberately NOT set here: calls that need the room
                # selected pass init_location=True.
                self._location_set = False
                self.logged_in = True
                self._block_lifted()
                self._on_login_success()
                print("[OK] Login successful")
                return True
            print("[FAIL] Login failed")
            self._on_login_failure(rejected=True)
            return False
        except DuwoBlocked:
            return False
        except Exception as e:
            print(f"[ERROR] Login: {e}")
            self._on_login_failure(rejected=False)
            return False

    def _init_location(self):
        if self._location_set:
            return
        resp = self._fetch(f"{BASE_URL}/findmachinetypes.php")
        resp.raise_for_status()
        if "findmachinetypes" not in resp.text.lower():
            raise RuntimeError("Could not select the laundry room")
        self._location_set = True

    def ensure_login(self):
        self._raise_if_blocked()
        if self.logged_in:
            return
        if self.background and time.time() < self._yield_until:
            resume = datetime.fromtimestamp(self._yield_until)
            raise DuwoPaused(
                f"DUWO is signed in elsewhere; background checks resume at {resume:%H:%M}."
            )
        if not self.login():
            self._raise_if_blocked()
            raise RuntimeError(
                "DUWO login unavailable; retry after the login cooldown."
            )

    def _account_home(self) -> requests.Response:
        # main.php renders whichever page the PHP session showed last, often a
        # calendar; page=home, the site's own Home button, selects the
        # dashboard with the balance. A second read covers DUWO rendering the
        # previous page once more.
        for _ in range(2):
            resp = self._get(f"{BASE_URL}/main.php?page=home")
            if re.search(r"ParentFile\s*=\s*['\"]user\.php['\"]", resp.text, re.I):
                self._location_set = False
                return resp
        raise RuntimeError("DUWO did not return the account page")

    @staticmethod
    def _available_count(cell) -> int | None:
        """Free machines in an availability cell, or None if unrecognised.

        Reads "2 available" and "Available : 2"; "Not available" or the
        TxtNotAvailable label means none.
        """
        text = cell.get_text(" ", strip=True)
        match = re.search(r"(\d+)\s+available\b", text, re.I) or re.search(
            r"\bavailable\s*:\s*(\d+)", text, re.I
        )
        if match:
            return int(match.group(1))
        if re.search(r"\bnot\s+available\b", text, re.I) or cell.find(
            class_="TxtNotAvailable"
        ):
            return 0
        return None

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
                    machines.append(
                        MachineStatus(
                            location=cells[0].get_text(strip=True),
                            machine_type=cells[1].get_text(strip=True),
                            status=cells[2].get_text(" ", strip=True),
                            available_count=self._available_count(cells[2]),
                        )
                    )
            return machines
        except Exception as exc:
            self._set_error(f"Could not load machine availability: {exc}")
            return []

    def get_calendar(
        self, machine_type_id: int, date: str | None = None
    ) -> list[CalendarBlock] | None:
        """One day of the booking calendar for a machine type (default today)."""
        self._clear_error()
        day = date or datetime.now().strftime("%Y-%m-%d")
        try:
            query = urlencode({"ObjectMachineTypeID": machine_type_id, "Start_Date": day})
            resp = self._get(
                f"{BASE_URL}/FindAvailableFromMachineType.php?{query}",
                init_location=True,
            )
            blocks = self._parse_calendar(resp.text, day)
        except Exception as exc:
            self._set_error(
                f"Could not load "
                f"{self._machine_type_name(machine_type_id).lower()} slots: {exc}"
            )
            return None
        slots = [block.slot for block in blocks if block.slot]
        if slots:
            self._remember_room_id(slots[0].location_id)
        return blocks

    @staticmethod
    def _parse_calendar(markup: str, day: str) -> list[CalendarBlock]:
        """The hour blocks of FindAvailableFromMachineType.php.

        A block with places left carries its booking value as its name
        ("45|2026-11-08|19:00:00|19:59") and "Available for booking:N"; a
        full block only shows its hours, as BookedNotByYou, or as BookedByYou
        when this account holds a booking in it.
        """
        soup = BeautifulSoup(markup, "html.parser")
        if soup.find(id="CalendarObjectKopTekst") is None:
            raise ValueError("DUWO did not return the booking calendar.")
        date = datetime.strptime(day, "%Y-%m-%d")
        blocks = []
        for div in soup.find_all("div"):
            classes = div.get("class") or []
            free = "DivCalendarObjectTijdsBlokNotBooked" in classes
            if not free and not {"BookedNotByYou", "BookedByYou"} & set(classes):
                continue
            text = div.get_text(" ")
            if not free:
                hours = re.search(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", text)
                if not hours:
                    raise ValueError("A calendar block has no recognisable hours.")
                begin = date.replace(hour=int(hours.group(1)), minute=int(hours.group(2)))
                finish = DUWOClient._booking_end(
                    begin, int(hours.group(3)), int(hours.group(4))
                )
                blocks.append(
                    CalendarBlock(begin, finish, 0, yours="BookedByYou" in classes)
                )
                continue
            parts = div.get("name", "").split("|")
            count = re.search(r"Available for booking\s*:\s*(\d+)", text)
            window = (
                re.fullmatch(r"(\d{2}):(\d{2})(?::\d{2})?", parts[2])
                and re.fullmatch(r"(\d{2}):(\d{2})", parts[3])
                if len(parts) == 4
                else None
            )
            if not count or not window:
                raise ValueError("A free calendar block has no booking value or count.")
            begin = datetime.strptime(f"{parts[1]} {parts[2][:5]}", "%Y-%m-%d %H:%M")
            finish = DUWOClient._booking_end(
                begin, int(parts[3][:2]), int(parts[3][3:5])
            )
            price = re.search(r"Pay on delivery\s*:\s*[€\u20ac]?\s*([\d.,]+)", text)
            slot = BookingSlot(
                raw_value=div["name"],
                location_id=parts[0],
                date=parts[1],
                start_time=parts[2],
                end_time=parts[3],
                available_count=int(count.group(1)),
                price=price.group(1) if price else "",
            )
            blocks.append(CalendarBlock(begin, finish, slot.available_count, slot=slot))
        return sorted(blocks, key=lambda block: block.start)

    def get_booking_slots(
        self, machine_type_id: int, date: str | None = None
    ) -> list[BookingSlot] | None:
        """Bookable slots of one day that have not ended yet."""
        blocks = self.get_calendar(machine_type_id, date)
        if blocks is None:
            return None
        now = datetime.now()
        return [
            block.slot
            for block in blocks
            if block.slot and block.free > 0 and block.end >= now
        ]

    MAX_BOOKINGS = 10
    # A booking must be started within 15 minutes of its start, so a longer
    # window only keeps a place from the neighbours; a typo should not.
    MAX_WINDOW = timedelta(hours=2)
    MAX_AHEAD = timedelta(days=14)
    START_GRACE = timedelta(minutes=15)

    def _validate_window(
        self,
        machine_type_id: int,
        date: str,
        start: str,
        end: str,
        *,
        allow_started: bool = False,
    ):
        """Check a window before anything is sent; return its start and end.

        allow_started accepts a calendar hour DUWO still offers after it began.
        """
        if machine_type_id not in (WASHER_TYPE_ID, DRYER_TYPE_ID):
            raise ValueError("Unknown machine type.")
        begin = datetime.strptime(f"{date} {start[:5]}", "%Y-%m-%d %H:%M")
        finish = datetime.strptime(f"{date} {end[:5]}", "%Y-%m-%d %H:%M")
        now = datetime.now().replace(second=0, microsecond=0)
        if finish <= begin:
            raise ValueError(
                "The end time must be after the start time on the same day."
            )
        if finish <= now:
            raise ValueError("That window has already ended.")
        if begin < now and not allow_started:
            raise ValueError("The booking start is in the past.")
        if finish - begin > self.MAX_WINDOW:
            raise ValueError("A booking can last at most 2 hours.")
        if begin > now + self.MAX_AHEAD:
            raise ValueError("A booking can start at most 14 days ahead.")
        return begin, finish

    @staticmethod
    def _page_message(markup: str) -> str:
        """The visible text of a short DUWO reply, for an error message."""
        soup = BeautifulSoup(markup, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        return " ".join(soup.get_text(" ").split())[:200]

    @staticmethod
    def _script_value(markup: str, name: str) -> str | None:
        match = re.search(
            r"\b" + re.escape(name) + r"\s*=\s*['\"]([^'\"]*)['\"]", markup
        )
        return match.group(1) if match else None

    def _create_booking(
        self, slot: BookingSlot, machine_type_id: int, *, allow_started: bool = False
    ) -> str | None:
        self._clear_error()
        submitted = False
        try:
            start, end = self._validate_window(
                machine_type_id,
                slot.date,
                slot.start_time,
                slot.end_time,
                allow_started=allow_started,
            )
            before = self.get_own_bookings(include_inactive=True)
            if before is None:
                raise RuntimeError(
                    "Booking aborted: your current bookings could not be loaded."
                )
            known = {b["id"] for b in before}
            # This request selects the machine type in PHP. Nothing that changes
            # the selection may run between here and the final submission.
            blocks = self.get_calendar(machine_type_id, slot.date)
            if blocks is None:
                raise RuntimeError(
                    self.last_error or "Could not select the machine type."
                )
            rooms = {block.slot.location_id for block in blocks if block.slot}
            if rooms and slot.location_id not in rooms:
                raise RuntimeError(
                    f"The calendar now belongs to room {min(rooms)}, not "
                    f"{slot.location_id}; nothing was booked. Try again."
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
                reason = self._page_message(confirm.text)
                raise RuntimeError(
                    "DUWO rejected the booking"
                    + (f": {reason}" if reason else ".")
                )
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
        """Book a slot DUWO's calendar offers, even one whose hour has begun."""
        return (
            self._create_booking(slot, machine_type_id, allow_started=True) is not None
        )

    def get_upcoming_slots(self, machine_type_id: int) -> list[BookingSlot] | None:
        """Bookable slots for the rest of today and for tomorrow."""
        slots = []
        for offset in (0, 1):
            day = (datetime.now() + timedelta(days=offset)).strftime("%Y-%m-%d")
            found = self.get_booking_slots(machine_type_id, day)
            if found is None:
                return None
            slots += found
        return slots

    def book_multiple(self, machine_type_id: int, count: int) -> list[BookingSlot]:
        self._clear_error()
        if not 1 <= count <= self.MAX_BOOKINGS:
            self._set_error(f"Choose between 1 and {self.MAX_BOOKINGS} machines.")
            return []
        slots = self.get_upcoming_slots(machine_type_id)
        if slots is None:
            return []
        slot = next((s for s in slots if s.available_count >= count), None)
        if slot is None:
            self._set_error(
                f"No single time window today or tomorrow has {count} places left."
            )
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
        if not re.fullmatch(r"[0-9]+", value):
            return None
        if value != self._room_id:
            self._room_id = value
            if self._state_store is not None:
                self._state_store.remember_location_id(value)
        return value

    def _parse_bookings(self, markup: str) -> list[dict]:
        """This account's rows of BookingOverview.php, which DUWO limits to the
        logged-in account whichever room the session has selected."""
        soup = BeautifulSoup(markup, "html.parser")
        table = soup.find(id="BookingTable") or soup.find(id="BookingOverviewTable")
        if table is None:
            raise ValueError("DUWO did not return a booking overview.")
        now = datetime.now()
        bookings = []
        for row in table.find_all("tr"):
            tds = row.find_all("td")
            cells = [td.get_text(" ", strip=True) for td in tds]
            if not cells:
                continue
            if len(tds) == 1 and (
                "EmptyRow" in (tds[0].get("class") or []) or tds[0].get("colspan")
            ):
                continue  # the "no bookings" placeholder of an empty table
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
            markup_row = str(row)
            res = re.search(r"RemoveBooking\(\s*['\"]?(\d+)['\"]?\s*\)", markup_row)
            if not res:
                raise ValueError("A booking has no cancellation identifier.")
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
        # Whole words only: a terminal or room called "...laundry" is no dryer.
        if re.search(r"\b(?:wash|wasmachine|wasautomaat)", text, re.I):
            return "Washer"
        if re.search(r"\b(?:dry|droger)", text, re.I):
            return "Dryer"
        return None

    def get_own_bookings(self, *, include_inactive: bool = False) -> list[dict] | None:
        self._clear_error()
        try:
            resp = self._get(f"{BASE_URL}/BookingOverview.php")
            bookings = self._parse_bookings(resp.text)
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

    def get_room_calendar(self, machine_type_id: int, hours: int = 6):
        """The next hour blocks of the booking calendar, into tomorrow if needed.

        DUWO shows no other resident's booking, so how full the coming hours
        are is the closest thing to the room's schedule.
        """
        now = datetime.now()
        blocks = self.get_calendar(machine_type_id)
        if blocks is None:
            return None
        upcoming = [block for block in blocks if block.end >= now]
        if len(upcoming) < hours:
            tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            more = self.get_calendar(machine_type_id, tomorrow)
            if more is None:
                return None
            upcoming += more
        return upcoming[:hours]

    @staticmethod
    def _booking_end(start: datetime, hour: int, minute: int) -> datetime:
        end = start.replace(hour=hour, minute=minute)
        return end + timedelta(days=1) if end < start else end

    @staticmethod
    def _booking_datetime(
        now: datetime, month: int, day: int, hour: int, minute: int
    ) -> datetime:
        """A year-less overview date: the latest one not beyond the booking
        horizon, so a row from last spring stays in the past."""
        horizon = now + timedelta(days=60)
        for year in range(now.year + 1, now.year - 5, -1):
            try:
                stamp = datetime(year, month, day, hour, minute)
            except ValueError:
                continue
            if stamp <= horizon:
                return stamp
        raise ValueError("Invalid booking date.")

    LOG_MONTHS = {
        "jan": 1, "feb": 2, "mar": 3, "mrt": 3, "apr": 4, "may": 5, "mei": 5,
        "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "okt": 10,
        "nov": 11, "dec": 12,
    }  # fmt: skip
    # What a start at the payment terminal costs tells which machine it was.
    START_PRICES = {Decimal("2.00"): "Washer", Decimal("1.00"): "Dryer"}

    def get_recent_cycles(self) -> list[Cycle] | None:
        """Our own machine starts within the lookback window, from UserLog.php.

        UserLog.php is the account's activity log, newest first and grouped
        under day headers ("Sun 8 Nov 2026"). Oldest first, a start at the
        payment terminal reads:

            09:49  Request balance           VM_DuwoSCP2
            09:49  Depreciation on location  VM_DuwoSCP2 · Washing Mach.   -2,00
            09:49  Create booking            VM_DuwoSCP2 · Booking 8170551
            09:49  Booking started           VM_DuwoSCP2 · Booking 8170551
            09:50  Booking paid              VM_DuwoSCP2 · Booking 8170551: 2,00

        The payment names the exact booking and its amount the machine; the
        debit just before the start is the fallback.
        """
        self._clear_error()
        try:
            resp = self._get(f"{BASE_URL}/UserLog.php")
        except Exception as exc:
            self._set_error(f"Could not read the DUWO activity log: {exc}")
            return None
        try:
            rows = self._parse_user_log(resp.text)
        except Exception as exc:
            # A layout the parser no longer understands would otherwise look
            # exactly like a quiet laundry room, so say so once.
            self._set_error(f"Could not read the DUWO activity log: {exc}")
            if not self._log_unreadable:
                self._log_unreadable = True
                self._send_alert(
                    "<b>Cannot read DUWO's activity log</b>\n"
                    f"{html.escape(str(exc))}\n"
                    "Machine-start alerts are paused until it can be read again."
                )
            return None
        self._log_unreadable = False
        rows.reverse()
        rows.sort(key=lambda row: row["at"])  # stable: same-minute rows keep order
        cutoff = datetime.now() - timedelta(hours=CYCLE_LOOKBACK_HOURS)
        cycles = []
        for index, row in enumerate(rows):
            if row["action"] != "booking started" or row["at"] < cutoff:
                continue
            number = self._log_booking_nr(row["info"])
            if number is None:
                continue
            machine = self._machine_from_context(rows, index, number)
            if machine is None:
                if number not in self._unattributed:
                    self._unattributed.add(number)
                    print(f"[WARN] Could not tell which machine booking {number} started")
                continue
            cycles.append(Cycle(booking_nr=number, machine=machine, started_at=row["at"]))
        return cycles

    @classmethod
    def _parse_user_log(cls, markup: str) -> list[dict]:
        """UserLog.php entries in page order (newest first): at, action, info."""
        table = BeautifulSoup(markup, "html.parser").find(id="UserLogTable")
        if table is None:
            raise ValueError("DUWO did not return the activity log.")
        rows, day, skipped = [], None, 0
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue  # column headers
            if "LogDay" in (row.get("class") or []):
                day = cls._log_day(cells[0].get_text(" ", strip=True))
                continue
            if len(cells) == 1:
                continue  # the placeholder row of an empty log
            clock = re.fullmatch(
                r"(\d{1,2}):(\d{2})(?::(\d{2}))?", cells[0].get_text(strip=True)
            )
            action = row.find(class_="LogAction")
            if day is None or clock is None or action is None:
                skipped += 1
                continue
            hour, minute, second = (int(part or 0) for part in clock.groups())
            detail = row.find(class_="LogDetail")
            rows.append(
                dict(
                    at=day.replace(hour=hour, minute=minute, second=second),
                    action=" ".join(action.get_text(" ").split()).lower(),
                    info=" ".join(detail.get_text(" ").split()) if detail else "",
                )
            )
        if skipped and not rows:
            raise ValueError("the activity log has a layout the bot does not know.")
        return rows

    @classmethod
    def _log_day(cls, text: str) -> datetime:
        """The date of an activity log day header such as "Sun 8 Nov 2026"."""
        match = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{4})\b", text)
        month = cls.LOG_MONTHS.get(match.group(2).lower()) if match else None
        if month is None:
            raise ValueError(f"unrecognised day {text[:40]!r} in the activity log.")
        return datetime(int(match.group(3)), month, int(match.group(1)))

    @staticmethod
    def _log_booking_nr(info: str) -> str | None:
        match = re.search(r"\bBooking\s*(?:NR)?\s*:?\s*(\d+)", info, re.I)
        return match.group(1) if match else None

    @classmethod
    def _machine_from_context(
        cls, rows: list[dict], index: int, booking_nr: str
    ) -> str | None:
        started = rows[index]["at"]
        window = timedelta(minutes=3)
        # The payment carries the exact booking number; a debit nearby could
        # belong to another machine started at nearly the same time.
        for row in rows:
            if row["action"] != "booking paid" or abs(row["at"] - started) > window:
                continue
            paid = re.search(
                r"\bBooking\s+(\d+)\s*:\s*(\d+(?:[.,]\d{1,2})?)", row["info"]
            )
            if paid and paid.group(1) == booking_nr:
                amount = Decimal(paid.group(2).replace(",", "."))
                if amount in cls.START_PRICES:
                    return cls.START_PRICES[amount]
        for row in reversed(rows[:index]):
            if started - row["at"] > window:
                break
            number = cls._log_booking_nr(row["info"])
            if number is not None and number != booking_nr:
                break  # rows of another booking: a different start
            if row["action"] == "depreciation on location":
                return cls._machine_name(row["info"].split("·")[-1])
        return None

    def get_qr_text(self) -> str | None:
        """Generate a new QR code and return its text value."""
        self._clear_error()
        try:
            resp = self._get(f"{BASE_URL}/GenUserQrcode.php?GenNew=TRUE")
            holder = BeautifulSoup(resp.text, "html.parser").find(
                attrs={"data-qrtext": True}
            )
            if holder and holder["data-qrtext"].strip():
                return holder["data-qrtext"].strip()
            m = re.search(r'"text"\s*:\s*"([^"]+)"', resp.text)
            if m:
                return m.group(1)
            self._set_error("DUWO did not return a QR text value.")
            return None
        except Exception as exc:
            self._set_error(f"Could not get a new QR code: {exc}")
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
"""


def help_text(tracker: CycleTracker) -> str:
    """The help text with the cycle estimates the bot actually uses."""
    return HELP_TEXT.replace(
        "{washer}", f"{tracker.minutes_for('Washer'):.0f}"
    ).replace("{dryer}", f"{tracker.minutes_for('Dryer'):.0f}")


def _day_label(when: datetime, now: datetime) -> str:
    """Compact, readable day for a booking row."""
    delta = when.date() - now.date()
    if delta.days == 0:
        return "today   "
    if delta.days == 1:
        return "tomorrow"
    return when.strftime("%d-%m   ")


def _calendar_rows(washers, dryers, now: datetime, mine=None) -> list[str]:
    """Places left per coming hour for washers and dryers, side by side.

    An hour is "yours" when one of our reservations overlaps it: DUWO lists
    the places left in such an hour as a separate free block, and shows an
    hour holding a custom /book_at window only as taken.
    """
    if washers is None and dryers is None:
        return ["  unavailable (DUWO read failed)"]
    hours: dict[datetime, list[str]] = {}
    for column, (machine, blocks) in enumerate((("Washer", washers), ("Dryer", dryers))):
        for block in blocks or []:
            ours = block.yours or any(
                b["machine"] == machine
                and b["status"] == "BookingReady"
                and b["start"] < block.end
                and block.start < b["end"]
                for b in mine or []
            )
            row = hours.setdefault(block.start, ["?", "?"])
            if row[column] != "yours":
                row[column] = "yours" if ours else str(block.free) if block.free else "full"
    if not hours:
        return ["  no more slots in the calendar"]
    lines = [f"  {'Hour':<12}{'Washer':<8}Dryer"]
    for start in sorted(hours)[:6]:
        label = f"{start:%H:%M}" if start.date() == now.date() else f"tmrw {start:%H:%M}"
        washer, dryer = hours[start]
        lines.append(f"  {label:<12}{washer:<8}{dryer}")
    return lines


def _reservation_state(booking: dict, now: datetime) -> str:
    if booking["status"] == "BookingBusy":
        return "DUWO timer ended" if booking["end"] < now else "running (DUWO)"
    return {"BookingReady": "reserved", "BookingFinished": "finished"}.get(
        booking["status"], "unknown"
    )


# Commands that change something at DUWO; run hours late they would book a
# window nobody asked for any more or invalidate a QR code in use.
STALE_COMMAND_MINUTES = 10
CHANGING_COMMANDS = ("/book", "/book_washer", "/book_dryer", "/book_at", "/cancel", "/qr")


def handle_command(
    cmd: str,
    duwo: DUWOClient,
    bot: TelegramBot,
    tracker: CycleTracker,
    sent_at: datetime | None = None,
):
    """Process a Telegram command and reply."""
    parts = cmd.strip().split(maxsplit=1)
    if not parts:
        return
    word = parts[0]
    if "@" in word:
        # In a group Telegram appends the bot's name; a command for another bot
        # is none of our business.
        word, _, target = word.partition("@")
        own = getattr(bot, "username", None)
        if not isinstance(own, str) or target.lower() != own.lower():
            return
    cmd = " ".join([word] + parts[1:])
    lower = cmd.lower()
    now = datetime.now()
    if (
        sent_at is not None
        and word.lower() in CHANGING_COMMANDS
        and now - sent_at > timedelta(minutes=STALE_COMMAND_MINUTES)
    ):
        bot.send(
            f"Skipped {html.escape(word)} sent at {sent_at:%H:%M}: it arrived while "
            f"the bot could not answer. Send it again if you still want it."
        )
        return

    # /help or /start
    if lower in ("/help", "/start"):
        bot.send(help_text(tracker))
        return

    # /status
    if lower == "/status":
        fresh = poll_cycles(duwo, bot, tracker, suppress=not tracker.cycle_baseline_set)
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
        if pending or not fresh:
            rows.extend(["", "YOUR CYCLE ESTIMATES"])
        if not fresh:
            rows.append("  activity log unreadable: saved timers only")
        if pending:
            for cycle in pending:
                eta = tracker.eta(cycle)
                state = (
                    f"ready ~{eta:%H:%M}"
                    if eta > stamp
                    else f"expected ready since {eta:%H:%M}"
                )
                rows.append(f"  {cycle.machine} {cycle.started_at:%H:%M}: {state}")

        rows.extend(["", "ROOM CALENDAR (bookable places left)"])
        rows.extend(
            _calendar_rows(
                duwo.get_room_calendar(WASHER_TYPE_ID),
                duwo.get_room_calendar(DRYER_TYPE_ID),
                stamp,
                mine,
            )
        )
        bot.send(f"<pre>{html.escape(chr(10).join(rows))}</pre>")
        return

    # /slots and /slots_dryer
    if lower in ("/slots", "/slots_dryer"):
        kind = "dryer" if lower == "/slots_dryer" else "washer"
        slots = duwo.get_upcoming_slots(
            DRYER_TYPE_ID if kind == "dryer" else WASHER_TYPE_ID
        )
        if slots is None:
            bot.send(
                html.escape(duwo.last_error or f"Failed to load {kind} slots from DUWO.")
            )
            return
        if not slots:
            bot.send(f"No {kind} slots left today or tomorrow.")
            return
        lines = [f"<b>{kind.capitalize()} slots:</b>", ""]
        for s in slots:
            lines.append(
                f"{s.date} {s.start_time[:5]}-{s.end_time} "
                f"| free: {s.available_count} | €{html.escape(s.price)}"
            )
        command = "/book_dryer" if kind == "dryer" else "/book"
        lines += ["", f"Reply {command} N to book N {kind}s for the earliest slot."]
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
        if end_t <= start_t:
            bot.send("The end time must be after the start time on the same day.")
            return
        now = datetime.now().replace(second=0, microsecond=0)
        if date_raw:
            try:
                day = datetime.strptime(date_raw, "%Y-%m-%d").date()
            except ValueError:
                bot.send("Date must look like YYYY-MM-DD.")
                return
        else:
            # A window that is over today means tomorrow; one still running
            # today is a mistake to point out, not to move to another day.
            day = now.date()
            if datetime.combine(day, end_t) <= now:
                day = day + timedelta(days=1)
            elif datetime.combine(day, start_t) < now:
                bot.send(
                    "That window has already started today. Choose a start from "
                    "now on, or add a date for another day."
                )
                return

        type_id = WASHER_TYPE_ID if kind == "washer" else DRYER_TYPE_ID
        start_s = start_t.strftime("%H:%M")
        end_s = end_t.strftime("%H:%M")
        date_s = day.strftime("%Y-%m-%d")
        bot.send(f"Reserving {kind} {date_s} {start_s}-{end_s}...")
        booking_nr = duwo.create_timed_booking(type_id, date_s, start_s, end_s)
        if booking_nr:
            start_by = datetime.combine(day, start_t) + DUWOClient.START_GRACE
            bot.send(
                f"[OK] Reserved {kind} {date_s} {start_s}-{end_s}\n"
                f"Start the machine by {start_by:%H:%M} or the booking expires.\n"
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
            "Books any window of up to 2 hours, not just whole hours. "
            "Defaults to today, or tomorrow if that window is already over."
        )
        return

    # /book N and /book_dryer N reserve one shared time window.
    # The number is required: Telegram turns a bare /book in any message into a
    # link, and one tap must not reserve a machine.
    match = re.fullmatch(r"/book(?P<kind>_washer|_dryer)?\s+(?P<count>[0-9]{1,3})", lower)
    if match:
        count = int(match.group("count"))
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
                start_by = (
                    datetime.strptime(
                        f"{slot.date} {slot.start_time[:5]}", "%Y-%m-%d %H:%M"
                    )
                    + DUWOClient.START_GRACE
                )
                lines.append(
                    f"  {slot.date} {slot.start_time[:5]}-{slot.end_time} "
                    f"(start by {start_by:%H:%M})"
                )
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
            "Usage: /book N or /book_dryer N, e.g. /book 2. All machines share the "
            "earliest time window with enough places today or tomorrow; DUWO "
            "normally allows 2 open bookings."
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
        if not bot.send_photo(buf.getvalue(), caption="Laundry QR Code"):
            bot.send(
                "DUWO made a new QR code, so the previous one no longer works, "
                "but sending it failed. Send /qr again."
            )
        return

    # /cycles - what of ours is running right now
    if lower == "/cycles":
        fresh = poll_cycles(duwo, bot, tracker, suppress=not tracker.cycle_baseline_set)
        pending = tracker.pending()
        caveat = (
            ""
            if fresh
            else "DUWO's activity log could not be read just now; this is from "
            "the saved timers only, so a machine started since is missing.\n\n"
        )
        if not pending:
            bot.send(caveat + "Nothing of yours is running.")
            return
        stamp = datetime.now()
        lines = ([caveat.strip(), ""] if caveat else []) + ["<b>Your machines:</b>", ""]
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
        # When the owner sent it, not when it was processed, is when the door
        # was opened; a /done delivered late must not learn a longer cycle.
        collected = min(now, sent_at) if sent_at else now
        poll_cycles(duwo, bot, tracker, suppress=not tracker.cycle_baseline_set)
        batch, observed, learned = tracker.record_collected(collected)
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
            parts.append(f"Washer estimate is now {learned:.0f} min.")
        elif observed is not None:
            parts.append(
                f"That is too far from the {tracker.minutes_for('Washer'):.0f} min "
                f"estimate to learn from."
            )
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
    if observed is not None:
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
    print(
        f"  Estimates in use: washer {tracker.minutes_for('Washer'):.0f}min, "
        f"dryer {tracker.minutes_for('Dryer'):.0f}min"
    )
    bot = TelegramBot(store=tracker)
    duwo = DUWOClient(notify_callback=bot.send, store=tracker)

    bot.set_commands()
    bot.fetch_username()

    if not tracker.cycle_baseline_set:
        print("[INFO] No state file yet: existing log entries will not be announced.")

    last_washer = None
    last_dryer = None
    # Monotonic, so a clock step on the NAS cannot stall the schedule.
    last_check = last_cycle_check = last_balance_check = float("-inf")
    low_balance_notified = False

    while True:
        try:
            # 1. Process Telegram commands
            for cmd, sent_at in bot.poll_commands():
                cmd_name = cmd.split()[0] if cmd else cmd
                print(f"[CMD] {cmd_name}")
                try:
                    handle_command(cmd, duwo, bot, tracker, sent_at=sent_at)
                except Exception:
                    traceback.print_exc()
                    bot.send("Something went wrong handling that command.")

            now = time.monotonic()

            # Checks the bot makes on its own leave the DUWO session to the
            # owner for a while after a login elsewhere ended it.
            with duwo.in_background():
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
                        machines = []

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
