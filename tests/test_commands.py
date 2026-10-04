from datetime import datetime, timedelta
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from test_regressions import app, response
from test_flows import Website, booking


class Commands(unittest.TestCase):
    def test_status_keeps_other_reservations_in_same_window(self):
        site = Website()
        site.mine = [booking()]
        client = site.client()
        client.get_availability = Mock(
            return_value=[app.MachineStatus("ROOM", "Washer", "Available: 2", 2)]
        )
        with tempfile.TemporaryDirectory() as tmp:
            tracker = app.CycleTracker(tmp + "/state.json")
            bot = Mock()
            app.handle_command("/status", client, bot, tracker)
        text = bot.send.call_args.args[0]
        self.assertIn("Washer tomorrow", text.split("RESERVED BY OTHERS")[1])

    def test_account_page_switch_can_take_one_extra_read(self):
        client = app.DUWOClient()
        client.logged_in = True
        client._location_set = True
        old = response("<script>var ParentFile='main.php';</script>Room page")
        current = response(
            "<script>var ParentFile='user.php';</script><span id='LblUserCredits'>81,00</span>"
        )
        client.session.get = Mock(side_effect=[old, current])
        self.assertEqual(client.get_balance(), "81.00")
        self.assertFalse(client.balance_stale)
        self.assertFalse(client._location_set)

    def test_status_refreshes_cycle_estimates_on_demand(self):
        site = Website()
        client = site.client()
        client.get_availability = Mock(return_value=[])
        client.get_recent_cycles = Mock(
            return_value=[app.Cycle("77", "Washer", datetime.now())]
        )
        with tempfile.TemporaryDirectory() as tmp:
            tracker = app.CycleTracker(tmp + "/state.json")
            bot = Mock()
            app.handle_command("/status", client, bot, tracker)
        self.assertIn("YOUR CYCLE ESTIMATES", bot.send.call_args.args[0])

    def test_named_bot_command_works(self):
        client = Mock()
        client.get_balance.return_value = "0.00"
        client.balance_stale = False
        bot = Mock()
        app.handle_command("/balance@LaundryBot", client, bot, Mock())
        self.assertEqual(bot.send.call_args.args[0], "Balance: EUR 0.00")

    def test_untrusted_error_is_escaped(self):
        client = Mock()
        client.get_balance.return_value = None
        client.last_error = "<bad&error>"
        bot = Mock()
        app.handle_command("/balance", client, bot, Mock())
        self.assertEqual(bot.send.call_args.args[0], "&lt;bad&amp;error&gt;")

    def test_partial_booking_reports_requested_count_and_failure(self):
        site = Website()
        client = site.client()
        slot = client.get_booking_slots(93)[0]
        client.book_multiple = Mock(return_value=[slot])
        client.last_error = "One slot was taken."
        bot = Mock()
        app.handle_command("/book 2", client, bot, Mock())
        text = bot.send.call_args.args[0]
        self.assertIn("PARTIAL", text)
        self.assertIn("1 of 2", text)
        self.assertIn("One slot was taken.", text)

    def test_unknown_machine_status_is_not_zero_available(self):
        client = app.DUWOClient()
        client.logged_in = True
        client.session.get = Mock(
            return_value=response(
                '<div id="MachineAvailabilityTable"><table><tr><td>ROOM</td><td>Washer</td><td>Offline</td></tr></table></div>'
            )
        )
        self.assertIsNone(client.get_availability()[0].available_count)

    def test_bookings_labels_stale_duwo_timer_without_offering_cancel(self):
        site = Website()
        site.mine = [
            booking(start=datetime.now() - timedelta(minutes=65), status="BookingBusy")
        ]
        bot = Mock()
        app.handle_command("/bookings", site.client(), bot, Mock())
        message = bot.send.call_args.args[0]
        self.assertIn("DUWO timer ended", message)
        self.assertNotIn("running", message)
        self.assertNotIn("/cancel", message)

    def test_past_reservations_are_not_listed_as_active(self):
        site = Website()
        site.mine = [booking(start=datetime.now() - timedelta(hours=3))]
        self.assertEqual(site.client().get_own_bookings(), [])

    def test_unknown_booking_status_is_not_an_empty_schedule(self):
        site = Website()
        site.mine = [booking(status="BookingChanged")]
        self.assertIsNone(site.client().get_own_bookings())

    def test_invalid_date_does_not_become_now(self):
        with self.assertRaises(ValueError):
            app.DUWOClient._booking_datetime(datetime(2026, 1, 1), 2, 30, 10, 0)

    def test_year_rollover(self):
        self.assertEqual(
            app.DUWOClient._booking_datetime(datetime(2026, 12, 31), 1, 1, 10, 0),
            datetime(2027, 1, 1, 10),
        )

    def test_malformed_booking_row_cannot_confirm_cancellation(self):
        client = app.DUWOClient()
        with self.assertRaises(ValueError):
            client._parse_bookings(
                '<div id="BookingOverviewTable"><tr><td>bad</td></tr></div>', own=True
            )

    def test_done_does_not_close_an_unfinished_dryer(self):
        now = datetime.now()
        with tempfile.TemporaryDirectory() as tmp:
            tracker = app.CycleTracker(tmp + "/state.json")
            washer = app.Cycle("1", "Washer", now - timedelta(minutes=55))
            dryer = app.Cycle("2", "Dryer", now - timedelta(minutes=42))
            tracker.cycles = {"1": washer, "2": dryer}
            batch, observed, learned = tracker.record_collected(now)
            self.assertEqual(batch, [washer])
            self.assertEqual(observed, 55)
            self.assertIsNotNone(learned)
            self.assertIsNone(dryer.collected_at)

    def test_cycle_baseline_survives_empty_first_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = app.CycleTracker(tmp + "/state.json")
            client = Mock()
            client.get_recent_cycles.return_value = []
            app.poll_cycles(client, Mock(), tracker, True)
            self.assertTrue(app.CycleTracker(tracker.path).cycle_baseline_set)

    def test_failed_first_poll_does_not_set_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = app.CycleTracker(tmp + "/state.json")
            client = Mock()
            client.get_recent_cycles.return_value = None
            self.assertFalse(app.poll_cycles(client, Mock(), tracker, True))
            self.assertFalse(tracker.cycle_baseline_set)

    def test_long_preformatted_message_uses_valid_chunks(self):
        bot = app.TelegramBot()
        ok = Mock(ok=True)
        ok.json.return_value = {"ok": True}
        long_text = "<pre>" + ("A line of machine data\n" * 250) + "</pre>"
        with patch.object(app.requests, "post", return_value=ok) as send:
            self.assertTrue(bot.send(long_text))
        self.assertGreater(len(send.call_args_list), 1)
        for call in send.call_args_list:
            body = call.kwargs["data"]["text"]
            self.assertLessEqual(len(body), 4096)
            self.assertTrue(body.startswith("<pre>"))
            self.assertTrue(body.endswith("</pre>"))


class TelegramDelivery(unittest.TestCase):
    def test_update_offset_persists_before_dispatch(self):
        data = {
            "ok": True,
            "result": [
                {
                    "update_id": 42,
                    "message": {"chat": {"id": "test"}, "text": "/book 1"},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            tracker = app.CycleTracker(tmp + "/state.json")
            bot = app.TelegramBot(tracker)
            with patch.object(
                app.requests, "get", return_value=response(json.dumps(data))
            ) as get:
                self.assertEqual(bot.poll_commands(), ["/book 1"])
            restored = app.TelegramBot(app.CycleTracker(tracker.path))
            self.assertEqual(restored.last_update_id, 42)
            self.assertEqual(get.call_args.kwargs["params"]["limit"], 1)

    def test_no_dispatch_if_offset_cannot_be_persisted(self):
        data = {
            "ok": True,
            "result": [
                {
                    "update_id": 42,
                    "message": {"chat": {"id": "test"}, "text": "/book 1"},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            tracker = app.CycleTracker(tmp + "/state.json")
            bot = app.TelegramBot(tracker)
            with (
                patch.object(
                    app.requests, "get", return_value=response(json.dumps(data))
                ),
                patch.object(tracker, "save", return_value=False),
            ):
                self.assertEqual(bot.poll_commands(), [])
            self.assertEqual(bot.last_update_id, 0)

    def test_unauthorized_chat_cannot_dispatch(self):
        data = {
            "ok": True,
            "result": [
                {
                    "update_id": 42,
                    "message": {"chat": {"id": "stranger"}, "text": "/cancel 1"},
                }
            ],
        }
        with patch.object(app.requests, "get", return_value=response(json.dumps(data))):
            self.assertEqual(app.TelegramBot().poll_commands(), [])

    def test_telegram_exception_does_not_log_token(self):
        with patch.object(
            app.requests, "post", side_effect=app.requests.ConnectionError(app.TG_API)
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertFalse(app.TelegramBot().send("hi"))
            self.assertNotIn(app.TG_API, output.getvalue())


if __name__ == "__main__":
    unittest.main()
