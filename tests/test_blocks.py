from datetime import datetime, timedelta
import time
import unittest
from unittest.mock import Mock, patch

from test_regressions import app, response


def block_page(until="21:05", code="101", title="Temporarily blocked"):
    # Markup of the notice DUWO serves instead of every page while blocked.
    return (
        "\n <!--\n/*\n\n*/\n -->\n<div class='BlockMessage' role='alert' "
        "style=\"max-width:460px;\"><strong style='display:block;'>"
        f"{title}</strong><div>For your security, access to this website from "
        "your internet connection is blocked for a few hours, because the website "
        "received a request it does not accept. This can also be caused by another "
        "device or app on the same internet connection.<br><br>You can try again "
        f"after <strong>{until}</strong>. Does this keep happening? Please contact "
        "the administrator of your location and mention the error code below.</div>"
        "<div style='margin-top:10px;'>Error code: <strong style='color:#1d2433;'>"
        f"{code}</strong></div></div>"
    )


def frozen_now(stamp):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return stamp

    return patch.object(app, "datetime", Frozen)


class BlockNotices(unittest.TestCase):
    def client(self, *pages):
        alerts = []
        client = app.DUWOClient(notify_callback=alerts.append)
        client.logged_in = True
        client.session.get = Mock(side_effect=[response(p) for p in pages])
        return client, alerts

    def test_connection_block_pauses_every_request_until_it_lifts(self):
        until = (datetime.now() + timedelta(hours=2)).strftime("%H:%M")
        client, alerts = self.client(block_page(until))
        self.assertIsNone(client.get_recent_cycles())
        self.assertIn(f"until {until}", client.last_error)
        self.assertIn("error code 101", client.last_error)
        self.assertIsNone(client.get_own_bookings())
        self.assertIsNone(client.get_balance())
        self.assertEqual(client.session.get.call_count, 1)
        self.assertEqual(len(alerts), 1)
        self.assertIn("blocked this internet connection", alerts[0])

    def test_block_during_login_is_not_reported_as_bad_credentials(self):
        alerts = []
        client = app.DUWOClient(notify_callback=alerts.append)
        client.session.get = Mock(return_value=response(block_page()))
        client.session.post = Mock()
        self.assertFalse(client.login())
        client.session.post.assert_not_called()
        self.assertEqual(client._login_fail_count, 0)
        self.assertEqual(len(alerts), 1)
        self.assertNotIn("credentials", alerts[0])
        with self.assertRaises(app.DuwoBlocked):
            client.ensure_login()

    def test_account_block_names_the_code_and_the_administrator(self):
        page = block_page(code="101-4711", title="Your account is blocked")
        client, alerts = self.client(page)
        self.assertIsNone(client.get_own_bookings())
        self.assertIn("101-4711", client.last_error)
        self.assertIn("administrator", client.last_error)

    def test_rejected_request_pauses_before_the_block_is_announced(self):
        client, _ = self.client(" report abuse  to admin -> DUWO85 -> <br>")
        self.assertIsNone(client.get_recent_cycles())
        self.assertIn("unsafe", client.last_error)
        self.assertGreater(client._blocked_until, time.time() + 25 * 60)

    def test_a_block_time_that_just_passed_is_retried_soon_not_tomorrow(self):
        with frozen_now(datetime(2026, 11, 8, 21, 5, 10)):
            soon = app.DUWOClient._unblock_time("You can try again after 21:05.")
            tomorrow = app.DUWOClient._unblock_time("You can try again after 03:00.")
            later = app.DUWOClient._unblock_time("Try again @:23:30:00")
        self.assertEqual(datetime.fromtimestamp(soon), datetime(2026, 11, 8, 21, 10, 10))
        self.assertEqual(datetime.fromtimestamp(tomorrow), datetime(2026, 11, 9, 3, 0))
        self.assertEqual(datetime.fromtimestamp(later), datetime(2026, 11, 8, 23, 30))

    def test_lifted_block_is_reported_once(self):
        client, alerts = self.client(
            '<div id="UserLogTable"><table></table></div>',
            '<div id="UserLogTable"><table></table></div>',
        )
        client._block_message = "DUWO has temporarily blocked this connection."
        client._blocked_until = time.time() - 1
        client.get_recent_cycles()
        client.get_recent_cycles()
        self.assertEqual(alerts, ["DUWO answers again; the block has been lifted."])

    def test_help_text_about_blocks_is_not_a_block(self):
        faq = (
            "<summary>The website says I am blocked (error code 101).</summary>"
            "<p><strong>Temporarily blocked</strong>, error code 101: the website "
            "received a request it does not accept. Try again after 21:05.</p>"
        )
        client, alerts = self.client(faq)
        client._get(app.BASE_URL + "/BookingHelp.php")
        self.assertEqual(client._blocked_until, 0.0)
        self.assertEqual(alerts, [])


class OutgoingRequests(unittest.TestCase):
    def test_login_strips_the_characters_the_login_page_strips(self):
        client = app.DUWOClient()
        client.session.get = Mock(return_value=response("<form></form>"))
        client.session.post = Mock(return_value=response("Wrong password"))
        with (
            patch.object(app, "EMAIL", "o'neil/x@example.com"),
            patch.object(app, "PASSWORD", 'pa"ss,wo`rd\\'),
        ):
            client.login()
        sent = client.session.post.call_args.kwargs["data"]
        self.assertEqual(sent, {"UserInput": "oneilx@example.com", "PwdInput": "password"})

    def test_unexpected_query_value_is_never_sent(self):
        client = app.DUWOClient()
        client.logged_in = True
        client.session.get = Mock()
        for value in ["a<b", "x'y", "1,2", "a/b", "%3Cscript%3E"]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    client._get(f"{app.BASE_URL}/AnnouncmentBooking.php?value={value}")
        client.session.get.assert_not_called()

    def test_booking_values_and_dates_are_allowed(self):
        app.DUWOClient._check_outgoing(
            app.BASE_URL + "/AnnouncmentBooking.php?value=45%7C2026-11-09%7C03%3A17%3A00%7C03%3A57"
        )
        app.DUWOClient._check_outgoing(
            app.BASE_URL
            + "/FindAvailableFromMachineType.php?ObjectMachineTypeID=93&Start_Date=2026-11-09"
        )

    def test_unreachable_website_is_retried_soon_without_blaming_credentials(self):
        alerts = []
        client = app.DUWOClient(notify_callback=alerts.append)
        client.session.get = Mock(side_effect=app.requests.ConnectionError("down"))
        for _ in range(8):
            client._login_cooldown_until = 0.0
            self.assertFalse(client.login())
        self.assertEqual(alerts, [])
        self.assertLessEqual(client._login_cooldown_until - time.time(), 300)


if __name__ == "__main__":
    unittest.main()
