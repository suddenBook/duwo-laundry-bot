#!/usr/bin/env python3
"""DUWO Laundry Monitor - Telegram bot that monitors and books laundry machines."""

import html
import io
import json
import os
import re
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta

import urllib3
import requests
from bs4 import BeautifulSoup

try:
    import qrcode
except ImportError:
    qrcode = None

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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

_REQUIRED_VARS = ["DUWO_EMAIL", "DUWO_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
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
        return float(raw.replace(",", "."))
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
WASHER_CYCLE_MINUTES = _env_int("WASHER_CYCLE_MINUTES", 55)
DRYER_CYCLE_MINUTES = _env_int("DRYER_CYCLE_MINUTES", 40)
CYCLE_HEADSUP_MINUTES = _env_int("CYCLE_HEADSUP_MINUTES", 5)
CYCLE_LOOKBACK_HOURS = _env_int("CYCLE_LOOKBACK_HOURS", 6)
STATE_PATH = (os.environ.get("STATE_PATH") or "/data/state.json").strip()

WASHER_TYPE_ID = 93
DRYER_TYPE_ID = 94
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


# ============== Data ==============


@dataclass
class MachineStatus:
    location: str
    machine_type: str
    status: str
    available_count: int


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
        for machine, value in (data.get("duration") or {}).items():
            if machine not in self.CALIBRATED:
                # Not learned, so the configured value wins over whatever an
                # older state file happens to hold.
                continue
            try:
                self.duration[machine] = self._clamp(float(value))
            except (TypeError, ValueError):
                continue
        for raw in data.get("cycles") or []:
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
            self.cycles[cycle.booking_nr] = cycle
        return True

    def save(self):
        payload = {
            "duration": self.duration,
            "cycles": [
                {
                    "booking_nr": c.booking_nr,
                    "machine": c.machine,
                    "started_at": c.started_at.isoformat(),
                    "notified_start": c.notified_start,
                    "notified_headsup": c.notified_headsup,
                    "notified_done": c.notified_done,
                    "collected_at": c.collected_at.isoformat() if c.collected_at else None,
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
        except OSError as exc:
            print(f"[WARN] Could not write state file {self.path}: {exc}")

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
        for cycle in observed:
            if cycle.booking_nr in self.cycles:
                continue
            if suppress:
                cycle.notified_start = True
                cycle.notified_headsup = True
                cycle.notified_done = True
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

        Machines started within BATCH_WINDOW of each other are one batch --
        that is how they are actually used, two or three loads back to back.
        Calibration uses the LAST machine to start, because that is the one
        that decides when the whole batch can be emptied.
        """
        pending = self.pending()
        if not pending:
            return [], None, None
        newest = pending[-1]
        batch = [c for c in pending if newest.started_at - c.started_at <= self.BATCH_WINDOW]
        observed = (now - newest.started_at).total_seconds() / 60.0
        learned = None
        if newest.machine in self.CALIBRATED and self.MIN_MINUTES <= observed <= self.MAX_MINUTES:
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
        stale = now - timedelta(hours=CYCLE_LOOKBACK_HOURS)
        for cycle in self.cycles.values():
            if cycle.collected_at is None and cycle.notified_done and cycle.started_at < stale:
                cycle.collected_at = cycle.started_at + timedelta(
                    minutes=self.minutes_for(cycle.machine)
                )

        cutoff = now - timedelta(hours=max(CYCLE_LOOKBACK_HOURS * 2, 24))
        for key in [k for k, c in self.cycles.items() if c.started_at < cutoff]:
            del self.cycles[key]


# ============== Telegram Bot ==============


class TelegramBot:
    """Handles sending messages and receiving commands via Telegram."""

    def __init__(self):
        self.last_update_id = 0

    def _validate_response(self, action: str, resp: requests.Response) -> bool:
        if not resp.ok:
            print(f"[TG] {action} http error: {resp.status_code}")
            return False
        try:
            data = resp.json()
        except ValueError:
            print(f"[TG] {action} invalid JSON response")
            return False
        if not data.get("ok"):
            print(f"[TG] {action} api error")
            return False
        return True

    def send(self, text: str) -> bool:
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
            print(f"[TG] send error: {e}")
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
            print(f"[TG] send_photo error: {e}")
            return False

    def poll_commands(self) -> list[str]:
        try:
            resp = requests.get(
                f"{TG_API}/getUpdates",
                params={"offset": self.last_update_id + 1, "timeout": 0},
                timeout=5,
            )
            data = resp.json()
            if not data.get("ok"):
                return []
            commands = []
            for update in data.get("result", []):
                self.last_update_id = update["update_id"]
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip()
                if chat_id == TELEGRAM_CHAT_ID and text:
                    commands.append(text)
            return commands
        except Exception:
            return []

    def flush_old_updates(self):
        try:
            resp = requests.get(
                f"{TG_API}/getUpdates", params={"timeout": 0}, timeout=5
            )
            data = resp.json()
            if data.get("ok") and data.get("result"):
                self.last_update_id = data["result"][-1]["update_id"]
        except Exception:
            pass

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
            print(f"[WARN] Failed to set command menu: {e}")
            return False


# ============== DUWO Client ==============


class DUWOClient:
    """Handles all communication with the DUWO laundry website."""

    # Login cooldown: exponential backoff on repeated failures
    LOGIN_COOLDOWN_BASE = 60       # first retry after 60s
    LOGIN_COOLDOWN_MAX = 1800      # cap at 30 minutes
    LOGIN_COOLDOWN_MULTIPLIER = 2

    def __init__(self, notify_callback=None):
        self.session = requests.Session()
        self.session.verify = False
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
        self.last_error = None
        # Login rate-limiting state
        self._login_fail_count = 0
        self._login_cooldown_until = 0.0    # timestamp: don't attempt login before this
        self._locked_until = 0.0            # timestamp: account lockout detected
        self._notify = notify_callback      # optional callable(str) for Telegram alerts
        self._location_set = False          # has LocNR been set in this session?
        self._cache: dict[str, tuple[float, list]] = {}

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

    def _on_login_failure(self):
        """Apply exponential backoff after a login failure."""
        self._login_fail_count += 1
        cooldown = min(
            self.LOGIN_COOLDOWN_BASE * (self.LOGIN_COOLDOWN_MULTIPLIER ** (self._login_fail_count - 1)),
            self.LOGIN_COOLDOWN_MAX,
        )
        self._login_cooldown_until = time.time() + cooldown
        print(f"[WAIT] Login failed ({self._login_fail_count}x), next attempt in {int(cooldown)}s")
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
        if any(kw in lower for kw in (
            "locked after too many",
            "too many login",
            "too many attempts",
            "geblokkeerd",
            "error 101",
        )):
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
            now = datetime.now()
            unlock = now.replace(hour=h, minute=m, second=0, microsecond=0)
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
        """Detect the site's JS redirect that indicates an invalid session."""
        body = resp.text.lower()
        if resp.url.lower().endswith("/login/index.php"):
            return True
        return bool(
            re.search(
                r"window\.location(?:\.href)?\s*=\s*['\"](?:\.\./)?index\.html['\"]",
                body,
            )
        )

    def _get(
        self, url: str, *, init_location: bool = False, allow_retry: bool = True
    ) -> requests.Response:
        """GET with auth validation and optional location/session re-init.

        Session validity is checked passively: if the response indicates an
        auth redirect we re-login once.  There is no proactive checkAuth.php
        request — this dramatically reduces login-related traffic.
        """
        self.ensure_login()
        if init_location:
            self._init_location()
        resp = self.session.get(url, timeout=20)
        if allow_retry and self._is_auth_redirect(resp):
            print("[INFO] Session expired, re-logging in...")
            self.logged_in = False
            if not self.login():
                raise RuntimeError("Re-login failed")
            if init_location:
                self._init_location()
            resp = self.session.get(url, timeout=20)
        return resp

    def _extract_balance(self, html: str) -> str | None:
        """Extract balance from known page variants without matching payment history."""
        soup = BeautifulSoup(html, "html.parser")
        selectors = (
            "#LblUserCredits",
            "#DivDispCredits",
            "#DivDispCreditsTransferCode",
        )
        for selector in selectors:
            el = soup.select_one(selector)
            if not el:
                continue
            text = el.get_text(" ", strip=True)
            match = re.search(r"([0-9]+(?:[.,][0-9]{1,2})?)", text)
            if match:
                return match.group(1)
            if text:
                return text

        for line in soup.get_text("\n", strip=True).splitlines():
            lower = line.lower()
            if "balance upgrades" in lower:
                continue
            if "balance" not in lower and "credit" not in lower:
                continue
            match = re.search(r"[€\u20ac]?\s*([0-9]+(?:[.,][0-9]{1,2})?)", line)
            if match:
                return match.group(1)

        return None

    def _machine_type_name(self, machine_type_id: int) -> str:
        return "Washer" if machine_type_id == WASHER_TYPE_ID else "Dryer"

    def _slot_booking_date(self, slot: BookingSlot) -> str:
        return datetime.strptime(slot.date, "%Y-%m-%d").strftime("%d-%m-%Y")

    def _booking_matches_slot(
        self, booking: dict, slot: BookingSlot, type_name: str
    ) -> bool:
        return (
            booking.get("type") == type_name
            and booking.get("date") == self._slot_booking_date(slot)
            and slot.start_time[:5] in booking.get("time", "")
        )

    def _count_matching_bookings(
        self, bookings: list[dict], slot: BookingSlot, type_name: str
    ) -> int:
        return sum(
            1 for booking in bookings if self._booking_matches_slot(booking, slot, type_name)
        )

    def _slot_available_count(
        self, slots: list[BookingSlot], target_slot: BookingSlot
    ) -> int | None:
        for slot in slots:
            if (
                slot.date == target_slot.date
                and slot.start_time == target_slot.start_time
                and slot.end_time == target_slot.end_time
            ):
                return slot.available_count
        return None

    def login(self) -> bool:
        if self._is_login_blocked():
            return False
        try:
            init_resp = self.session.get(f"{BASE_URL}/login/index.php", timeout=20)
            if self._detect_lockout(init_resp.text):
                return False
            resp = self.session.post(
                f"{BASE_URL}/login/submit.php",
                data={"UserInput": EMAIL, "PwdInput": PASSWORD},
                allow_redirects=False,
                timeout=20,
            )
            if "StartSite.php" in resp.text:
                match = re.search(r"document\.location\s*=\s*'([^']+)'", resp.text)
                if match:
                    redirect = match.group(1)
                    if redirect.startswith(".."):
                        redirect = redirect.replace("..", BASE_URL, 1)
                    elif not redirect.startswith("http"):
                        redirect = f"{BASE_URL}/{redirect}"
                    self.start_url = redirect
                    self.session.get(redirect, timeout=20)
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
        """Load findmachinetypes.php to set LocNR in the PHP session."""
        self.session.get(f"{BASE_URL}/findmachinetypes.php", timeout=20)
        self._location_set = True

    def ensure_login(self):
        """Ensure we are logged in. Does NOT proactively probe checkAuth.php.

        Session validity is verified passively by _get() — if a page response
        indicates an auth redirect, _get() will call login() at that point.
        This avoids unnecessary requests to the login subsystem.
        """
        if not self.logged_in:
            self.login()

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
                    count = 0
                    m = re.search(r"Available\s*:(\d+)", status_text)
                    if m:
                        count = int(m.group(1))
                    machines.append(
                        MachineStatus(
                            location=cells[0].get_text(strip=True),
                            machine_type=cells[1].get_text(strip=True),
                            status=status_text,
                            available_count=count,
                        )
                    )
            return machines
        except Exception:
            print("[ERROR] get_availability failed")
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
                m = re.search(r"Available for booking:(\d+)", text)
                if m:
                    avail = int(m.group(1))
                price = ""
                pm = re.search(
                    r"Pay on delivery\s*:\s*[€\u20ac]?\s*([\d.,]+)", text
                )
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
            return slots
        except Exception:
            self._set_error("get_booking_slots failed.")
            return None

    def book_slot(self, slot: BookingSlot, machine_type_id: int) -> bool:
        self._clear_error()
        try:
            type_name = self._machine_type_name(machine_type_id)
            before_bookings = self.get_bookings()
            before_match_count = (
                self._count_matching_bookings(before_bookings, slot, type_name)
                if before_bookings is not None
                else None
            )
            before_slots = self.get_booking_slots(machine_type_id, date=slot.date)
            before_available = (
                self._slot_available_count(before_slots, slot)
                if before_slots is not None
                else None
            )

            # Step 1: AnnouncmentBooking sets up session state
            announce = self._get(
                f"{BASE_URL}/AnnouncmentBooking.php?value={slot.raw_value}",
                init_location=True,
            )
            # Step 2: ConfirmCreateBooking prepares the booking
            confirm = self._get(
                f"{BASE_URL}/ConfirmCreateBooking.php?value={slot.raw_value}"
            )
            # Step 3: CreateBooking executes it
            resp = self._get(
                f"{BASE_URL}/CreateBooking.php?value={slot.raw_value}"
            )
            for step_name, step_resp in (
                ("AnnouncmentBooking", announce),
                ("ConfirmCreateBooking", confirm),
                ("CreateBooking", resp),
            ):
                step_body = step_resp.text.lower()
                if "locnr" in step_body or "mysql" in step_body:
                    print(f"[FAIL] {step_name} failed: location/session state missing")
                    return False

            body = resp.text.lower()

            if "error" in body or "fail" in body or "not available" in body:
                print(f"[FAIL] Booking rejected: {slot.date} {slot.start_time[:5]}")
                return False

            after_bookings = self.get_bookings()
            if after_bookings is not None:
                after_match_count = self._count_matching_bookings(
                    after_bookings, slot, type_name
                )
                if before_match_count is None:
                    if after_match_count > 0:
                        self._clear_error()
                        print(f"[OK] Booked: {slot.date} {slot.start_time[:5]}-{slot.end_time}")
                        return True
                elif after_match_count > before_match_count:
                    self._clear_error()
                    print(f"[OK] Booked: {slot.date} {slot.start_time[:5]}-{slot.end_time}")
                    return True

            after_slots = self.get_booking_slots(machine_type_id, date=slot.date)
            if after_slots is not None and before_available is not None:
                after_available = self._slot_available_count(after_slots, slot)
                if after_available is None or after_available < before_available:
                    self._clear_error()
                    print(f"[OK] Booked: {slot.date} {slot.start_time[:5]}-{slot.end_time}")
                    return True

            self._set_error(
                f"Booking submission finished, but DUWO did not confirm "
                f"{type_name.lower()} {slot.date} {slot.start_time[:5]}."
            )
            return False
        except Exception as e:
            self._set_error(f"book_slot failed: {e}")
            return False

    def book_multiple(self, machine_type_id: int, count: int) -> list[BookingSlot]:
        self._clear_error()
        booked = []
        for _ in range(count):
            slots = self.get_booking_slots(machine_type_id)
            if slots is None:
                break
            if not slots or slots[0].available_count <= 0:
                break
            if self.book_slot(slots[0], machine_type_id):
                booked.append(slots[0])
            else:
                break
        return booked

    def get_balance(self) -> str | None:
        """Get account balance via AnnouncmentBooking.php.

        The DUWO main page does not include balance in its HTML (it's AJAX-
        loaded in the browser).  However, the booking confirmation page
        (AnnouncmentBooking.php) always shows "Your Balance : € X.XX",
        so we use that as a reliable source.  We request it with any valid
        slot value — this only prepares a booking preview, it does NOT
        actually create a booking.
        """
        self._clear_error()
        try:
            # Get any available slot to use as a parameter
            slots = self.get_booking_slots(WASHER_TYPE_ID)
            if not slots:
                slots = self.get_booking_slots(DRYER_TYPE_ID)
            if not slots:
                self._set_error("No slots available to query balance.")
                return self.last_known_balance
            resp = self._get(
                f"{BASE_URL}/AnnouncmentBooking.php?value={slots[0].raw_value}",
                init_location=True,
            )
            m = re.search(
                r"Your Balance\s*:.*?[€\u20ac&]?\s*([\d]+(?:[.,]\d{1,2})?)",
                resp.text, re.DOTALL,
            )
            if m:
                self.last_known_balance = m.group(1)
                return self.last_known_balance
            # Fallback: try extracting from HTML
            balance = self._extract_balance(resp.text)
            if balance:
                self.last_known_balance = balance
                return balance
        except Exception:
            pass
        self._set_error("Could not retrieve balance from DUWO.")
        return self.last_known_balance

    def get_balance_float(self) -> float | None:
        bal = self.get_balance()
        if bal is None:
            return None
        try:
            clean = re.sub(r"[^0-9,.-]", "", bal)
            return float(clean.replace(",", "."))
        except (ValueError, AttributeError):
            self._set_error(f"Could not parse DUWO balance value: {bal}")
            return None

    def get_bookings(self) -> list[dict] | None:
        """Return list of bookings with id, date, time, type info."""
        self._clear_error()
        bookings = []
        try:
            for type_id, type_name in [(WASHER_TYPE_ID, "Washer"), (DRYER_TYPE_ID, "Dryer")]:
                resp = self._get(
                    f"{BASE_URL}/FindAvailableFromMachineType.php?ObjectMachineTypeID={type_id}",
                    init_location=True,
                )
                soup = BeautifulSoup(resp.text, "html.parser")
                header = soup.find("div", id="CalendarObjectKopTekst")
                if header is None and not soup.find_all("div", class_="BookedByYou"):
                    self._set_error(
                        f"Failed to load {type_name.lower()} bookings from DUWO."
                    )
                    return None
                # Get the date from page header
                date_str = ""
                if header:
                    dm = re.search(r"(\d{2}-\d{2}-\d{4})", header.get_text())
                    if dm:
                        date_str = dm.group(1)
                for div in soup.find_all("div", class_="BookedByYou"):
                    res_nr = div.get("name", "")
                    # Extract time from TijdsBlokTijd span
                    time_span = div.find("span", class_="TijdsBlokTijd")
                    time_str = time_span.get_text(strip=True) if time_span else ""
                    bookings.append({
                        "id": res_nr,
                        "type": type_name,
                        "date": date_str,
                        "time": time_str,
                    })
        except Exception:
            self._set_error("get_bookings failed.")
            return None
        return bookings

    def cancel_booking(self, res_nr: str) -> bool:
        """Cancel one of our bookings.

        Uses get_own_bookings() rather than the hour-grid calendar, because a
        window booked by create_timed_booking is not rendered there at all.
        """
        self._clear_error()
        try:
            before_bookings = self.get_own_bookings()
            if before_bookings is None:
                self._set_error("Cancellation aborted because current bookings could not be loaded.")
                return False
            if not any(b["id"] == res_nr for b in before_bookings):
                self._set_error(f"Booking {res_nr} was not found before cancellation.")
                return False

            # Step 1: Re-init location and set ResNr in session
            announce = self._get(
                f"{BASE_URL}/AnnouncmentBooking.php?ResNr={res_nr}",
                init_location=True,
            )
            # Step 3: DeleteBooking reads ResNr from session
            resp = self._get(f"{BASE_URL}/DeleteBooking.php")
            for step_name, step_resp in (
                ("AnnouncmentBooking", announce),
                ("DeleteBooking", resp),
            ):
                step_body = step_resp.text.lower()
                if "locnr" in step_body or "mysql" in step_body:
                    print(f"[FAIL] {step_name} failed: location/session state missing")
                    return False
            body = resp.text.lower()
            if "error" in body:
                print(f"[FAIL] cancel_booking ResNr={res_nr}: server error")
                return False

            # The verification read below must not be served from the
            # pre-cancellation cache.
            self._cache.clear()
            after_bookings = self.get_own_bookings()
            if after_bookings is None:
                self._set_error(
                    f"Cancellation for booking {res_nr} was submitted, but verification failed."
                )
                return False
            if any(b["id"] == res_nr for b in after_bookings):
                self._set_error(f"Booking {res_nr} still appears after cancellation attempt.")
                return False

            self._clear_error()
            print(f"[OK] Cancelled booking ResNr={res_nr}")
            return True
        except Exception as e:
            self._set_error(f"cancel_booking failed: {e}")
            return False

    def _latest_booking_event(self) -> tuple[str, datetime] | None:
        """Most recent 'Create Booking' in our own activity log."""
        try:
            resp = self._get(f"{BASE_URL}/UserLog.php")
            newest = None
            for row in BeautifulSoup(resp.text, "html.parser").find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
                if len(cells) < 3 or cells[2] != "Create Booking":
                    continue
                try:
                    stamp = datetime.strptime(cells[0], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                match = re.search(r"BookingNR\s*:\s*(\d+)", " ".join(cells))
                if match and (newest is None or stamp > newest[1]):
                    newest = (match.group(1), stamp)
            return newest
        except Exception:
            return None

    def create_timed_booking(
        self, machine_type_id: int, date: str, start: str, end: str
    ) -> str | None:
        """Reserve an arbitrary window, not just the whole hours the UI offers.

        DUWO's calendar only renders hour-aligned blocks, but the backend
        stores real windows -- a walk-up start at 16:12 produces 16:12->16:47 --
        and CreateBooking.php accepts a hand-made value just fine.  It then
        returns HTTP 500 while trying to render the confirmation into the hour
        grid, *after* the booking has already been committed, so the status
        code must not be read as failure.  The hour grid also cannot display
        the result, which is why verification goes through UserLog.php.

        Returns the new BookingNR, or None.
        """
        self._clear_error()
        try:
            location = self._location_id(machine_type_id)
            if location is None:
                self._set_error("Could not work out the laundry room id.")
                return None
            before = self._latest_booking_event()
            value = f"{location}|{date}|{start}:00|{end}"
            for step, url in (
                ("AnnouncmentBooking", f"{BASE_URL}/AnnouncmentBooking.php?value={value}"),
                ("ConfirmCreateBooking", f"{BASE_URL}/ConfirmCreateBooking.php?value={value}"),
                ("CreateBooking", f"{BASE_URL}/CreateBooking.php?value={value}"),
            ):
                resp = self._get(url, init_location=(step == "AnnouncmentBooking"))
                print(f"[BOOK] {step} -> HTTP {resp.status_code}")
            self._cache.clear()

            after = self._latest_booking_event()
            if after and (before is None or after[0] != before[0]):
                print(f"[OK] Timed booking {after[0]}: {date} {start}-{end}")
                return after[0]
            self._set_error(
                f"DUWO did not record a booking for {date} {start}-{end}."
            )
            return None
        except Exception as exc:
            self._set_error(f"Timed booking failed: {exc}")
            return None

    def _location_id(self, machine_type_id: int) -> str | None:
        """Read the room id off a real slot rather than hard-coding it."""
        slots = self.get_booking_slots(machine_type_id)
        if slots:
            return slots[0].location_id
        return None

    def _ensure_own_scope(self):
        """Drop LocNR so BookingOverview.php reports OUR bookings again.

        LocNR sticks to the PHP session and flips that page to the whole
        location, so the only way back is a fresh session.
        """
        if self._location_set:
            self.logged_in = False
            self.login()

    BOOKING_CACHE_TTL = 45  # seconds; /status reads both views back to back

    def _cached(self, key: str):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.BOOKING_CACHE_TTL:
            return hit[1]
        return None

    def _store(self, key: str, value):
        if value is not None:
            self._cache[key] = (time.time(), value)
        return value

    def get_own_bookings(self) -> list[dict] | None:
        """Our own bookings, including windows the hour grid cannot render.

        The calendar's BookedByYou blocks only cover hour-aligned slots, so a
        booking made by create_timed_booking is invisible there.  This view has
        all of them, with the ResNr needed to cancel.
        """
        self._clear_error()
        cached = self._cached("own_bookings")
        if cached is not None:
            return cached
        try:
            self._ensure_own_scope()
            if self._location_set:
                # Refuse rather than risk presenting the whole location's
                # bookings as the user's own, complete with cancel ids.
                self._set_error(
                    "Could not switch back to your own bookings. Try again shortly."
                )
                return None
            resp = self._get(f"{BASE_URL}/BookingOverview.php")
            table = BeautifulSoup(resp.text, "html.parser").find(id="BookingOverviewTable")
            if table is None:
                self._set_error("Failed to load your bookings from DUWO.")
                return None
            now = datetime.now()
            bookings = []
            for row in table.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
                if len(cells) < 4:
                    continue
                day = re.match(r"(\d{2})-(\d{2})$", cells[0])
                window = re.match(r"(\d{2}):(\d{2})->(\d{2}):(\d{2})", cells[1])
                if not day or not window:
                    continue
                res = re.search(r"RemoveBooking\((\d+)\)", str(row))
                status = next(
                    (
                        name
                        for tag in row.find_all("p")
                        for name in (tag.get("class") or [])
                        if name.startswith("Booking")
                    ),
                    "",
                )
                bookings.append(
                    {
                        "id": res.group(1) if res else "",
                        "machine": "Washer" if "wash" in cells[3].lower() else "Dryer",
                        "start": self._booking_datetime(
                            now,
                            int(day.group(2)),
                            int(day.group(1)),
                            int(window.group(1)),
                            int(window.group(2)),
                        ),
                        "end_label": f"{window.group(3)}:{window.group(4)}",
                        "status": status,
                    }
                )
            bookings.sort(key=lambda b: b["start"])
            return self._store("own_bookings", bookings)
        except Exception as exc:
            self._set_error(f"Failed to load your bookings from DUWO: {exc}")
            return None

    def get_location_bookings(self) -> list[dict] | None:
        """Upcoming reservations for the whole laundry room.

        BookingOverview.php normally lists the logged-in account's own
        bookings, but switches to the whole location once LocNR is set in the
        PHP session -- which is what init_location does here.  That location
        view is the room's shared schedule: it is how a resident sees which
        machines are already spoken for.

        Only what the page actually renders is read: date, time window and
        machine type.  The page also leaks other residents' booking ids inside
        HTML comments; those are deliberately not parsed.
        """
        self._clear_error()
        cached = self._cached("room_bookings")
        if cached is not None:
            return cached
        try:
            resp = self._get(f"{BASE_URL}/BookingOverview.php", init_location=True)
            table = BeautifulSoup(resp.text, "html.parser").find(id="BookingOverviewTable")
            if table is None:
                self._set_error("Could not load the laundry room schedule.")
                return None
            now = datetime.now()
            bookings = []
            for row in table.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
                if len(cells) < 4:
                    continue
                day = re.match(r"(\d{2})-(\d{2})$", cells[0])
                window = re.match(r"(\d{2}):(\d{2})->(\d{2}):(\d{2})", cells[1])
                if not day or not window:
                    continue
                status = next(
                    (
                        name
                        for tag in row.find_all("p")
                        for name in (tag.get("class") or [])
                        if name.startswith("Booking")
                    ),
                    "",
                )
                bookings.append(
                    {
                        "start": self._booking_datetime(
                            now,
                            int(day.group(2)),
                            int(day.group(1)),
                            int(window.group(1)),
                            int(window.group(2)),
                        ),
                        "end_label": f"{window.group(3)}:{window.group(4)}",
                        "machine": "Washer" if "wash" in cells[3].lower() else "Dryer",
                        "status": status,
                    }
                )
            bookings.sort(key=lambda b: b["start"])
            return self._store("room_bookings", bookings)
        except Exception as exc:
            self._set_error(f"Could not load the laundry room schedule: {exc}")
            return None

    @staticmethod
    def _booking_datetime(now: datetime, month: int, day: int, hour: int, minute: int) -> datetime:
        """DUWO renders dates as DD-MM with no year; pick the nearest one."""
        try:
            stamp = datetime(now.year, month, day, hour, minute)
        except ValueError:
            return now
        if stamp - now > timedelta(days=180):
            return stamp.replace(year=now.year - 1)
        if now - stamp > timedelta(days=180):
            return stamp.replace(year=now.year + 1)
        return stamp

    def get_recent_cycles(self) -> list[Cycle] | None:
        """Parse our own machine starts out of UserLog.php.

        UserLog.php is always scoped to the logged-in account.  BookingOverview.php
        is NOT: once LocNR is set in the PHP session (which login() does, via
        findmachinetypes.php) it switches to listing the whole location's
        bookings instead of ours, so it must not be used for this.

        A terminal start looks like:

            16:12:28  Depreciation on location  -2,00  Washing Mach.
            16:12:37  Booking Started                  BookingNR : 8005487
            16:12:39  Booking Payd                     8005487 ->  200

        Machine type comes from the adjacent Depreciation row, falling back to
        the amount on Booking Payd (200 = EUR 2.00 = washer, 100 = dryer).
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
            if not rows:
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
    def _machine_from_context(rows: list[dict], index: int, booking_nr: str) -> str | None:
        window = timedelta(minutes=3)
        started = rows[index]["at"]
        for row in reversed(rows[:index]):
            if started - row["at"] > window:
                break
            if row["action"] == "Depreciation on location" and row["info"]:
                return "Washer" if "wash" in row["info"].lower() else "Dryer"
        for row in rows[index:]:
            if row["at"] - started > window:
                break
            if row["action"] == "Booking Payd" and booking_nr in row["info"]:
                paid = re.search(r"->\s*(\d+)", row["info"])
                if paid:
                    return "Washer" if int(paid.group(1)) >= 200 else "Dryer"
                break
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

Start and ready alerts are sent for your own machines only; nothing else is pushed. DUWO frees a machine well before its door actually opens, so the bot runs its own clock: washer {washer} min, dryer {dryer} min. Send /done when you really open the door and it learns your timings.
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


def handle_command(
    cmd: str, duwo: DUWOClient, bot: TelegramBot, tracker: CycleTracker
):
    """Process a Telegram command and reply."""
    cmd = cmd.strip()
    lower = cmd.lower()

    # /help or /start
    if lower in ("/help", "/start"):
        bot.send(HELP_TEXT)
        return

    # /status
    if lower == "/status":
        machines = duwo.get_availability()
        if not machines:
            bot.send(duwo.last_error or "Failed to get status. Try again.")
            return
        stamp = datetime.now()
        header = f"{'Type':<13}{'Free':>4}"
        sep = "-" * 30
        rows = [header, sep]
        for m in machines:
            rows.append(f"{m.machine_type[:13]:<13}{m.available_count:>4}")
        bal = duwo.get_balance()
        rows.append(sep)
        rows.append("Balance: unavailable" if bal is None else f"Balance: EUR {bal}")

        # Ours first. Read before the room view, which sets LocNR and so has to
        # come second.
        mine = duwo.get_own_bookings() or []
        mine = [b for b in mine if b["status"] != "BookingFinished"]
        rows.append("")
        rows.append("YOURS")
        if mine:
            for b in mine:
                state = {"BookingReady": "reserved", "BookingBusy": "running"}.get(
                    b["status"], b["status"] or "?"
                )
                rows.append(
                    f"  {b['machine']:<7}{_day_label(b['start'], stamp)} "
                    f"{b['start']:%H:%M}-{b['end_label']}  {state}"
                )
        else:
            rows.append("  nothing booked")

        room = duwo.get_location_bookings() or []
        owned = {(b["machine"], b["start"]) for b in mine}
        others = [
            b
            for b in room
            if b["status"] == "BookingReady"
            and b["start"] >= stamp - timedelta(hours=1)
            and (b["machine"], b["start"]) not in owned
        ]
        rows.append("")
        rows.append("RESERVED BY OTHERS")
        if others:
            for b in others[:8]:
                rows.append(
                    f"  {b['machine']:<7}{_day_label(b['start'], stamp)} "
                    f"{b['start']:%H:%M}-{b['end_label']}"
                )
        else:
            rows.append("  nothing reserved ahead")

        bot.send(f"<pre>{html.escape(chr(10).join(rows))}</pre>")
        return


    # /slots (washers)
    if lower == "/slots":
        slots = duwo.get_booking_slots(WASHER_TYPE_ID)
        if slots is None:
            bot.send(duwo.last_error or "Failed to load washer slots from DUWO.")
            return
        if not slots:
            bot.send("No washer slots available.")
            return
        lines = ["<b>Washer slots:</b>", ""]
        for i, s in enumerate(slots):
            lines.append(
                f"[{i}] {s.start_time[:5]}-{s.end_time} "
                f"| free: {s.available_count} | €{s.price}"
            )
        lines.append("")
        lines.append("Reply /book N to book N washers for the earliest slot.")
        bot.send("\n".join(lines))
        return

    # /slots_dryer
    if lower == "/slots_dryer":
        slots = duwo.get_booking_slots(DRYER_TYPE_ID)
        if slots is None:
            bot.send(duwo.last_error or "Failed to load dryer slots from DUWO.")
            return
        if not slots:
            bot.send("No dryer slots available.")
            return
        lines = ["<b>Dryer slots:</b>", ""]
        for i, s in enumerate(slots):
            lines.append(
                f"[{i}] {s.start_time[:5]}-{s.end_time} "
                f"| free: {s.available_count} | €{s.price}"
            )
        lines.append("")
        lines.append("Reply /book_dryer N to book N dryers for the earliest slot.")
        bot.send("\n".join(lines))
        return

    # /balance
    if lower == "/balance":
        bal = duwo.get_balance()
        if bal is None:
            bot.send(duwo.last_error or "Failed to load balance from DUWO.")
        else:
            bot.send(f"Balance: EUR {bal}")
        return

    # /bookings (must be before /book to avoid regex collision)
    if lower == "/bookings":
        bookings = duwo.get_own_bookings()
        if bookings is None:
            bot.send(duwo.last_error or "Failed to load bookings from DUWO.")
            return
        upcoming = [
            b for b in bookings if b["status"] != "BookingFinished"
        ]
        if not upcoming:
            bot.send("No active bookings.")
            return
        lines = ["<b>Your bookings:</b>", ""]
        for b in upcoming:
            state = {"BookingReady": "reserved", "BookingBusy": "running"}.get(
                b["status"], b["status"] or "?"
            )
            when = b["start"].strftime("%d-%m %H:%M")
            lines.append(
                f"  {b['machine']} | {when}-{b['end_label']} | {state}"
                f" | ID: <code>{html.escape(b['id'])}</code>"
            )
        lines.append("")
        lines.append("Cancel: /cancel ID")
        bot.send("\n".join(lines))
        return

    # /cancel ID
    m = re.match(r"/cancel\s+(\S+)\s*$", lower)
    if m:
        res_nr = m.group(1)
        bot.send(f"Cancelling booking {res_nr}...")
        if duwo.cancel_booking(res_nr):
            bot.send(f"[OK] Booking {res_nr} cancelled.")
        else:
            bot.send(
                f"[FAIL] {duwo.last_error or f'Could not cancel booking {res_nr}.'}"
            )
        return

    # /cancel without args
    if lower == "/cancel":
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
            if datetime.combine(day, start_t) < now:
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
                f"Booking {html.escape(booking_nr)}\n\n"
                f"It will not show on DUWO's hour calendar. "
                f"Use /bookings to see or cancel it."
            )
        else:
            bot.send(f"[FAIL] {html.escape(duwo.last_error or 'Could not reserve that window.')}")
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

    # /book N
    m = re.match(r"/book(?:_washer)?\s*(\d+)?\s*$", lower)
    if m:
        count = int(m.group(1)) if m.group(1) else 1
        bot.send(f"Booking {count} washer(s)...")
        booked = duwo.book_multiple(WASHER_TYPE_ID, count)
        if booked:
            lines = [f"[OK] Booked {len(booked)} washer(s):"]
            for s in booked:
                lines.append(f"  {s.date} {s.start_time[:5]}-{s.end_time}")
            bot.send("\n".join(lines))
        elif duwo.last_error:
            bot.send(f"[FAIL] {duwo.last_error}")
        else:
            bot.send("[FAIL] No washer slots available to book.")
        return

    # /book_dryer N
    m = re.match(r"/book_dryer\s*(\d+)?\s*$", lower)
    if m:
        count = int(m.group(1)) if m.group(1) else 1
        bot.send(f"Booking {count} dryer(s)...")
        booked = duwo.book_multiple(DRYER_TYPE_ID, count)
        if booked:
            lines = [f"[OK] Booked {len(booked)} dryer(s):"]
            for s in booked:
                lines.append(f"  {s.date} {s.start_time[:5]}-{s.end_time}")
            bot.send("\n".join(lines))
        elif duwo.last_error:
            bot.send(f"[FAIL] {duwo.last_error}")
        else:
            bot.send("[FAIL] No dryer slots available to book.")
        return

    # /qr
    if lower == "/qr":
        qr_text = duwo.get_qr_text()
        if not qr_text:
            bot.send(duwo.last_error or "Failed to get QR code.")
            return
        if qrcode is None:
            bot.send(f"QR text: <code>{qr_text}</code>\n(qrcode lib not installed)")
            return
        img = qrcode.make(qr_text)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        bot.send_photo(buf.getvalue(), caption=f"Laundry QR Code")
        return

    # /cycles - what of ours is running right now
    if lower == "/cycles":
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
                state = f"ready ~{eta.strftime('%H:%M')} ({int(left)} min left)"
            else:
                state = f"ready since {eta.strftime('%H:%M')}"
            lines.append(
                f"  {cycle.machine} started {cycle.started_at.strftime('%H:%M')} - {state}"
            )
        lines.append("")
        lines.append("Send /done once you have actually opened the door.")
        bot.send("\n".join(lines))
        return

    # /done - confirm collection, and calibrate from it
    if lower == "/done":
        batch, observed, learned = tracker.record_collected(datetime.now())
        if not batch:
            bot.send("Nothing of yours is waiting to be collected.")
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


def poll_cycles(duwo: DUWOClient, bot: TelegramBot, tracker: CycleTracker, suppress: bool):
    """Detect our own machine starts and fire the two messages that matter."""
    observed = duwo.get_recent_cycles()
    if observed is None:
        print(f"[WARN] {duwo.last_error or 'cycle poll failed'}")
        return
    changed = bool(tracker.merge(observed, suppress=suppress))
    stamp = datetime.now()

    for cycle in tracker.pending():
        eta = tracker.eta(cycle)
        if not cycle.notified_start:
            cycle.notified_start = True
            changed = True
            if stamp < eta:
                print(f"[CYCLE] {cycle.machine} {cycle.booking_nr} started "
                      f"{cycle.started_at:%H:%M}, ready ~{eta:%H:%M}")
                bot.send(
                    f"<b>{cycle.machine} started</b>\n\n"
                    f"Started: {cycle.started_at.strftime('%H:%M')}\n"
                    f"Ready to open: about {eta.strftime('%H:%M')}"
                )
                continue
            # First seen only after its ETA had already passed (e.g. the bot
            # was down for a while): skip the start message, report it ready.

        if not cycle.notified_headsup and stamp >= eta - timedelta(minutes=CYCLE_HEADSUP_MINUTES):
            cycle.notified_headsup = True
            changed = True
            if stamp < eta:
                bot.send(
                    f"<b>{cycle.machine} almost done</b>\n\n"
                    f"Ready to open around {eta.strftime('%H:%M')} - time to head down."
                )

        if not cycle.notified_done and stamp >= eta:
            cycle_minutes = tracker.minutes_for(cycle.machine)
            cycle.notified_done = True
            changed = True
            learns = cycle.machine in CycleTracker.CALIBRATED
            if not learns:
                # Nothing to calibrate for this machine, so close it out here
                # rather than leave it sitting in /cycles waiting for a /done.
                cycle.collected_at = stamp
            print(f"[CYCLE] {cycle.machine} {cycle.booking_nr} should be openable now")
            bot.send(
                f"<b>{cycle.machine} ready</b>\n\n"
                f"Started {cycle.started_at.strftime('%H:%M')}, "
                f"{cycle_minutes:.0f} min ago."
                + ("\nSend /done once you have actually opened it." if learns else "")
            )

    tracker.prune(stamp)
    if changed:
        tracker.save()


def run():
    """Main entry point: monitor + interactive Telegram bot."""
    print("=" * 50)
    print("  DUWO Laundry Bot")
    print(f"  Check interval: {CHECK_INTERVAL}s")
    print(f"  Cycle estimate: washer {WASHER_CYCLE_MINUTES}min, dryer {DRYER_CYCLE_MINUTES}min")
    print(f"  Head-up before ready: {CYCLE_HEADSUP_MINUTES}min")
    print(f"  State file: {STATE_PATH}")
    print(f"  Availability alerts: {'on' if NOTIFY_AVAILABILITY else 'off'}")
    print(f"  Low balance alerts: {'on' if NOTIFY_LOW_BALANCE else 'off'}")
    print("=" * 50)

    bot = TelegramBot()
    duwo = DUWOClient(notify_callback=bot.send)

    # Retry initial login with backoff — don't exit on lockout
    while not duwo.login():
        if duwo._locked_until > time.time():
            wait = int(duwo._locked_until - time.time()) + 5
            print(f"[LOCK] Waiting {wait}s for lockout to expire...")
            time.sleep(wait)
        elif duwo._login_cooldown_until > time.time():
            wait = int(duwo._login_cooldown_until - time.time()) + 1
            print(f"[WAIT] Cooldown {wait}s...")
            time.sleep(wait)
        else:
            print("[FATAL] Cannot login. Check credentials.")
            return

    bot.flush_old_updates()
    bot.set_commands()

    tracker = CycleTracker(STATE_PATH)
    first_cycle_poll = not tracker.loaded
    if first_cycle_poll:
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
                poll_cycles(duwo, bot, tracker, first_cycle_poll)
                first_cycle_poll = False

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
                w = washer.available_count if washer else 0
                d = dryer.available_count if dryer else 0

                print(f"[{ts}] Washer: {w} | Dryer: {d}")

                # Notify on current machine status thresholds
                if last_washer is not None:
                    if last_washer < WASHER_NOTIFY_THRESHOLD <= w:
                        bot.send(
                            f"<b>Washers available now</b>\n\n"
                            f"Current free washers: {w}\n"
                            f"Alert threshold: {WASHER_NOTIFY_THRESHOLD}\n\n"
                            f"Use /status to check current machine status."
                        )
                    elif last_washer > 0 and w == 0:
                        bot.send("All washers now occupied.")

                if last_dryer is not None:
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
                if bal is not None:
                    if bal < LOW_BALANCE_THRESHOLD and not low_balance_notified:
                        bot.send(
                            f"<b>Low balance: EUR {bal:.2f}</b>\n\n"
                            f"Top up on the DUWO website (Add Credits → Upgrade online, min EUR 20)."
                        )
                        low_balance_notified = True
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
