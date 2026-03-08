#!/usr/bin/env python3
"""DUWO Laundry Monitor - Telegram bot that monitors and books laundry machines."""

import io
import os
import re
import time
import traceback
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))
LOW_BALANCE_THRESHOLD = float(os.environ.get("LOW_BALANCE_THRESHOLD", "3.0"))
WASHER_NOTIFY_THRESHOLD = int(os.environ.get("WASHER_NOTIFY_THRESHOLD", "3"))
DRYER_NOTIFY_THRESHOLD = int(os.environ.get("DRYER_NOTIFY_THRESHOLD", "1"))

WASHER_TYPE_ID = 93
DRYER_TYPE_ID = 94
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Payment methods for pay.nl
PAYMENT_METHODS = {
    "ideal": {"id": "10", "name": "iDEAL"},
    "card": {"id": "706", "name": "Visa/Mastercard"},
}


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
            {"command": "slots", "description": "Washer slots"},
            {"command": "slots_dryer", "description": "Dryer slots"},
            {"command": "book", "description": "Book N washers"},
            {"command": "book_dryer", "description": "Book N dryers"},
            {"command": "bookings", "description": "Your bookings"},
            {"command": "cancel", "description": "Cancel a booking"},
            {"command": "balance", "description": "Account balance"},
            {"command": "qr", "description": "Laundry QR code"},
            {"command": "topup", "description": "Top up (ideal/card)"},
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

    def _normalize_amount_input(self, raw_amount: str) -> tuple[Decimal, str] | None:
        normalized = raw_amount.strip().replace(",", ".")
        if not re.fullmatch(r"\d+(?:\.\d{1,2})?", normalized):
            return None
        try:
            amount = Decimal(normalized)
        except InvalidOperation:
            return None
        if amount <= 0:
            return None
        return amount, normalized

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
                # Load findmachinetypes.php to set LocNR in PHP session
                # (required for booking/cancel operations to work)
                self._init_location()
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
        """Load findmachinetypes.php to set LocNR in PHP session."""
        self.session.get(f"{BASE_URL}/findmachinetypes.php", timeout=20)

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
        self._clear_error()
        try:
            before_bookings = self.get_bookings()
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

            after_bookings = self.get_bookings()
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

    def create_payment(self, amount_input: str, option_id: str) -> str | None:
        """
        Create a top-up payment.
        Returns the payment URL, or None on failure.
        amount_input: EUR string with up to 2 decimals (e.g. "20", "20.50")
        option_id: pay.nl option ID ("10" for iDEAL, "706" for Visa/Mastercard)
        """
        self._clear_error()
        parsed = self._normalize_amount_input(amount_input)
        if parsed is None:
            self._set_error("Invalid amount. Use a positive number with up to 2 decimals.")
            return None
        amount_decimal, normalized_amount = parsed
        cents = int(amount_decimal * 100)

        self.ensure_login()
        try:
            # Step 1: Create payment record
            resp = self.session.get(
                f"{BASE_URL}/startpayment.php?amount={normalized_amount}"
            )
            m = re.search(r"Inserted\s*:\s*(\d+)", resp.text)
            if not m:
                print("[ERROR] create_payment: no PayID")
                self._set_error("DUWO did not create a payment record.")
                return None
            pay_id = m.group(1)

            # Step 2: Submit pay.nl form with selected payment method
            resp2 = self.session.post(
                f"{BASE_URL}/pay.nl/index.php?id={pay_id}",
                data={
                    "action": "startTransaction",
                    "bedrag": str(cents),
                    "optionId": option_id,
                },
            )
            # Step 3: Extract payment URL from HTML
            soup2 = BeautifulSoup(resp2.text, "html.parser")
            btn = soup2.find("a", class_="button")
            if btn and btn.get("href"):
                pay_url = btn["href"]

                # Step 4: Store in DB (like the JS does)
                tx_link_m = re.search(
                    r'TransactionLink\s*=\s*"([^"]+)"', resp2.text
                )
                tx_id_m = re.search(
                    r'TransactionID\s*=\s*"([^"]+)"', resp2.text
                )
                if tx_link_m and tx_id_m:
                    self.session.get(
                        f"{BASE_URL}/pay.nl/WriteDataToDB.php",
                        params={
                            "PayID": pay_id,
                            "TransactionID": tx_id_m.group(1),
                            "TransactionLink": tx_link_m.group(1),
                        },
                    )
                print("[OK] Payment link created")
                return pay_url

            print("[ERROR] create_payment: no payment link in response")
            self._set_error("pay.nl did not return a payment link.")
            return None

        except Exception:
            print("[ERROR] create_payment failed")
            self._set_error("create_payment failed.")
            return None


# ============== Bot Command Handler ==============


HELP_TEXT = """<b>DUWO Laundry Bot</b>

<b>Commands:</b>
/status - Machine availability
/slots - Washer slots
/slots_dryer - Dryer slots
/book N - Book N washers
/book_dryer N - Book N dryers
/bookings - Your bookings
/cancel ID - Cancel a booking
/balance - Account balance
/qr - Laundry QR code
/topup AMOUNT [ideal|card] - Top up balance
/help - This message

<b>Examples:</b>
/topup 20 ideal
/topup 20 card
/cancel 12345

Auto-monitors every {interval}s. Low balance alert below EUR {threshold}.
""".replace("{interval}", str(CHECK_INTERVAL)).replace(
    "{threshold}", str(LOW_BALANCE_THRESHOLD)
)


def handle_command(cmd: str, duwo: DUWOClient, bot: TelegramBot):
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
            bot.send("Failed to get status. Try again.")
            return
        header = f"{'Type':<12} {'Status':<12} {'Free':>4}"
        sep = "-" * len(header)
        rows = [header, sep]
        for m in machines:
            short_type = m.machine_type[:12]
            short_status = "Available" if m.available_count > 0 else "Occupied"
            rows.append(f"{short_type:<12} {short_status:<12} {m.available_count:>4}")
        bal = duwo.get_balance()
        rows.append(sep)
        if bal is None:
            rows.append("Balance: unavailable")
        else:
            rows.append(f"Balance: EUR {bal}")
        bot.send(f"<pre>{chr(10).join(rows)}</pre>")
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
        bookings = duwo.get_bookings()
        if bookings is None:
            bot.send(duwo.last_error or "Failed to load bookings from DUWO.")
            return
        if not bookings:
            bot.send("No active bookings.")
            return
        lines = ["<b>Your bookings:</b>", ""]
        for b in bookings:
            lines.append(f"  {b['type']} | {b['date']} {b['time']} | ID: <code>{b['id']}</code>")
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

    # /topup AMOUNT [METHOD]
    m = re.match(r"/topup\s+(\d+(?:[.,]\d{1,2})?)(?:\s+(\w+))?\s*$", lower)
    if m:
        amount_input = m.group(1)
        normalized_amount = amount_input.replace(",", ".")
        try:
            amount_value = int(float(normalized_amount))
        except ValueError:
            amount_value = 0
        if amount_value < 20:
            bot.send("Minimum top-up amount is €20.")
            return
        if "." in normalized_amount or "," in amount_input:
            bot.send("Only whole euro amounts are accepted (e.g. /topup 20).")
            return
        method_key = m.group(2) or "ideal"
        method = PAYMENT_METHODS.get(method_key)
        if not method:
            bot.send(
                f"Unknown method: {method_key}\n"
                "Available: <code>ideal</code>, <code>card</code>"
            )
            return
        bot.send(f"Creating EUR {normalized_amount} payment via {method['name']}...")
        url = duwo.create_payment(amount_input, method["id"])
        if url:
            bot.send(
                f"<b>Payment link ready</b>\n\n"
                f"Amount: EUR {normalized_amount}\n"
                f"Method: {method['name']}\n\n"
                f'<a href="{url}">Click here to pay</a>'
            )
        else:
            bot.send(duwo.last_error or "[FAIL] Failed to create payment link.")
        return

    # /topup without args
    if lower.startswith("/topup"):
        bot.send(
            "Usage: /topup AMOUNT [ideal|card]\n"
            "Examples: /topup 20 ideal\n"
            "          /topup 20.50 card\n\n"
            "Default: iDEAL. Minimum: €20"
        )
        return

    # Unknown
    bot.send("Unknown command. Send /help for available commands.")


# ============== Main Loop ==============


def run():
    """Main entry point: monitor + interactive Telegram bot."""
    print("=" * 50)
    print("  DUWO Laundry Bot")
    print(f"  Check interval: {CHECK_INTERVAL}s")
    print(f"  Low balance threshold: EUR {LOW_BALANCE_THRESHOLD}")
    print(f"  Washer notify threshold: {WASHER_NOTIFY_THRESHOLD}")
    print(f"  Dryer notify threshold: {DRYER_NOTIFY_THRESHOLD}")
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
    bot.send(HELP_TEXT)

    last_washer = None
    last_dryer = None
    last_check = 0
    last_balance_check = 0
    low_balance_notified = False

    while True:
        try:
            # 1. Process Telegram commands
            for cmd in bot.poll_commands():
                cmd_name = cmd.split()[0] if cmd else cmd
                print(f"[CMD] {cmd_name}")
                handle_command(cmd, duwo, bot)

            # 2. Periodic availability check
            now = time.time()
            if now - last_check >= CHECK_INTERVAL:
                last_check = now
                ts = datetime.now().strftime("%H:%M:%S")
                machines = duwo.get_availability()

                if not machines:
                    print(f"[{ts}] Failed to get status")
                    time.sleep(5)
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

            # 3. Periodic balance check (every 10 min)
            if now - last_balance_check >= 600:
                last_balance_check = now
                bal = duwo.get_balance_float()
                if bal is not None:
                    if bal < LOW_BALANCE_THRESHOLD and not low_balance_notified:
                        bot.send(
                            f"<b>Low balance: EUR {bal:.2f}</b>\n\n"
                            f"Top up: /topup 20 ideal\n"
                            f"Or: /topup 20 card"
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
