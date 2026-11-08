import time
import unittest
from unittest.mock import Mock

from test_regressions import app
from test_flows import Website


class SessionSharing(unittest.TestCase):
    """DUWO keeps one session per account; the owner's own login wins."""

    def setUp(self):
        self.site = Website()
        self.client = self.site.client()
        self.logins = 0

        def login():
            self.logins += 1
            self.client.logged_in = True
            return True

        self.client.login = Mock(side_effect=login)

    def test_background_check_leaves_the_session_to_a_login_elsewhere(self):
        self.site.expire_at = "UserLog.php"
        with self.client.in_background():
            self.assertIsNone(self.client.get_recent_cycles())
            self.assertIsNone(self.client.get_recent_cycles())
        self.assertEqual(self.logins, 0)
        self.assertEqual(self.site.calls.count("UserLog.php"), 1)
        self.assertIn("signed in elsewhere", self.client.last_error)

    def test_a_command_signs_back_in_at_once(self):
        self.site.expire_at = "UserLog.php"
        with self.client.in_background():
            self.client.get_recent_cycles()
        self.site.expire_at = None
        self.assertEqual(self.client.get_own_bookings(), [])
        self.assertEqual(self.logins, 1)

    def test_background_checks_resume_after_the_pause(self):
        self.site.expire_at = "UserLog.php"
        with self.client.in_background():
            self.client.get_recent_cycles()
        self.site.expire_at = None
        self.client._yield_until = time.time() - 1
        with self.client.in_background():
            self.assertEqual(self.client.get_recent_cycles(), [])
        self.assertEqual(self.logins, 1)

    def test_pause_can_be_switched_off(self):
        self.site.expire_at = "UserLog.php"
        original = app.SESSION_YIELD_MINUTES
        app.SESSION_YIELD_MINUTES = 0
        try:
            with self.client.in_background():
                self.client.get_recent_cycles()
        finally:
            app.SESSION_YIELD_MINUTES = original
        self.assertEqual(self.logins, 1)


if __name__ == "__main__":
    unittest.main()
