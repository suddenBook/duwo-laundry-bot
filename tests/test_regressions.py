import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import Mock

for key in ("DUWO_EMAIL", "DUWO_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
    os.environ[key] = "test"

import duwo_monitor as app


def response(body, status=200):
    resp = app.requests.Response()
    resp.status_code = status
    resp._content = body.encode()
    resp.url = app.BASE_URL + "/BookingOverview.php"
    return resp


def overview():
    day = datetime.now().strftime("%d-%m")
    return f"""<div id="BookingOverviewTable"><table><tr>
    <td>{day}</td><td>23:00-&gt;23:59</td><td><p class="BookingReady">Ready</p></td>
    <td>Washing Mach.</td><td><button onclick="RemoveBooking(12345)">Cancel</button></td>
    </tr></table></div>"""


class ReportedRegressions(unittest.TestCase):
    def test_own_bookings_can_be_loaded_with_state_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = app.DUWOClient(store=app.CycleTracker(tmp + "/state.json"))
            client._get = Mock(return_value=response(overview()))
            self.assertIsNotNone(client.get_own_bookings())

    def test_room_bookings_can_be_loaded_without_state_store(self):
        client = app.DUWOClient()
        client._get = Mock(return_value=response(overview()))
        self.assertIsNotNone(client.get_location_bookings())

    def test_status_does_not_report_failed_read_as_empty_schedule(self):
        client = Mock()
        client.get_availability.return_value = [
            app.MachineStatus("room", "Washer", "Available:3", 3)
        ]
        client.get_balance.return_value = "5.00"
        client.balance_stale = False
        client.get_own_bookings.return_value = None
        client.get_location_bookings.return_value = None
        bot = Mock()
        tracker = Mock()
        tracker.pending.return_value = []
        app.handle_command("/status", client, bot, tracker)
        text = bot.send.call_args.args[0]
        self.assertNotIn("nothing booked", text)
        self.assertNotIn("nothing reserved ahead", text)
        self.assertIn("unavailable", text.lower())

    def test_balance_does_not_use_credit_history_total(self):
        client = app.DUWOClient()
        page = "<div>Credit added 20.00</div><div>Balance upgrades 80.00</div>"
        self.assertIsNone(client._extract_balance(page))


if __name__ == "__main__":
    unittest.main()
