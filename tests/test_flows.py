from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

from test_regressions import app, response


def booking(
    res_id="100", machine="Washer", start=None, status="BookingReady", booking_nr="200"
):
    start = start or (datetime.now() + timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    end = start + timedelta(minutes=59)
    return dict(
        id=res_id,
        booking_nr=booking_nr,
        machine=machine,
        start=start,
        end=end,
        end_label=end.strftime("%H:%M"),
        status=status,
    )


def booking_html(rows):
    body = '<div id="BookingOverviewTable"><table><tr><th>Date</th></tr>'
    for b in rows:
        # The actual site puts both identifiers in comments, not visible cells.
        body += f"""<tr><!--<td onclick='RemoveBooking({b["id"]})'><p>{b["booking_nr"]}</p></td>-->
        <td>{b["start"]:%d-%m}</td><td>{b["start"]:%H:%M}-&gt;{b["end_label"]}</td>
        <td>ROOM</td><td><p class='{b["status"]}'>{"Washing mach" if b["machine"] == "Washer" else "Dryer"}</p></td></tr>"""
    return body + "</table></div>"


class Website:
    """Small stateful replay of the website's observed PHP workflow."""

    def __init__(self):
        self.mine = []
        self.room = [booking("999", booking_nr="888")]
        self.scope = "own"
        self.machine = None
        self.selected = None
        self.removing = None
        self.calls = []
        self.create_status = 200
        self.timeout_after_commit = False
        self.expire_at = None
        self.reject = False
        self.malformed_overview = False
        self.slot = booking()["start"]
        self.raw = f"45|{self.slot:%Y-%m-%d}|12:00:00|12:59"

    def get(self, url, **kwargs):
        path = urlparse(url).path.rsplit("/", 1)[-1]
        query = parse_qs(urlparse(url).query)
        self.calls.append(path)
        if path == self.expire_at:
            return response("<script>document.location='../index.html';</script>")
        if path == "UserLog.php":
            return response('<div id="UserLogTable"><table></table></div>')
        if path == "main.php":
            if query.get("page") == ["user.php"]:
                self.scope = "own"
            return response(
                "<script>var ParentFile='user.php';</script><span id='LblUserCredits'>81,00</span>"
            )
        if path == "findmachinetypes.php":
            self.scope = "room"
            return response("<script>var ParentFile='findmachinetypes.php';</script>")
        if path == "BookingOverview.php":
            if self.malformed_overview:
                return response("<h1>Error</h1>", 500)
            return response(
                booking_html(
                    self.mine if self.scope == "own" else self.mine + self.room
                )
            )
        if path == "FindAvailableFromMachineType.php":
            self.machine = int(query["ObjectMachineTypeID"][0])
            return response(f"""<div id='CalendarObjectKopTekst'>{self.slot:%d-%m-%Y}</div>
            <div class='DivCalendarObjectTijdsBlokNotBooked' name='{self.raw}'>Available for booking: 2 Pay on delivery: € 2.00</div>""")
        if path == "AnnouncmentBooking.php":
            if "ResNr" in query:
                self.removing = query["ResNr"][0]
                return response(
                    f"Remove Booking <button id='BtnRemoveBooking'>Confirm</button><script>var ResNr = '{self.removing}';</script>"
                )
            self.selected = query["value"][0]
            return response(
                f"<button id='BtnOkBooking'>Confirm</button><script>var Value = '{self.selected}';var ObjectMachineTypeID = '{self.machine}';</script>"
            )
        if path == "ConfirmCreateBooking.php":
            if self.reject:
                return response("Not available")
            return response(
                f"<script>var Value = '{self.selected}';var ObjectMachineTypeID = '{self.machine}';$('#x').load('CreateBooking.php?value='+Value);</script>"
            )
        if path == "CreateBooking.php":
            if not self.reject:
                _, day, start, end = self.selected.split("|")
                b = booking(
                    str(101 + len(self.mine)),
                    "Washer" if self.machine == 93 else "Dryer",
                    datetime.fromisoformat(day + "T" + start),
                    booking_nr=str(201 + len(self.mine)),
                )
                b["end_label"] = end
                b["end"] = datetime.fromisoformat(day + "T" + end)
                self.mine.append(b)
            if self.timeout_after_commit:
                raise app.requests.ReadTimeout("connection lost after commit")
            return response("", self.create_status)
        if path == "DeleteBooking.php":
            self.mine = [b for b in self.mine if b["id"] != self.removing]
            return response("")
        raise AssertionError("Unexpected endpoint: " + path)

    def client(self):
        c = app.DUWOClient()
        c.logged_in = True
        c.session.get = Mock(side_effect=self.get)
        return c


class BookingFlows(unittest.TestCase):
    def test_own_room_own_preserves_scope_without_new_login(self):
        site = Website()
        site.mine = [booking()]
        client = site.client()
        client.login = Mock(side_effect=AssertionError("Unexpected login"))
        self.assertEqual([b["id"] for b in client.get_own_bookings()], ["100"])
        self.assertEqual(len(client.get_location_bookings()), 2)
        self.assertEqual([b["id"] for b in client.get_own_bookings()], ["100"])
        self.assertEqual(site.scope, "own")

    def test_reads_are_fresh(self):
        site = Website()
        client = site.client()
        self.assertEqual(client.get_own_bookings(), [])
        site.mine.append(booking())
        self.assertEqual(len(client.get_own_bookings()), 1)

    def test_balance_forces_account_page_after_calendar(self):
        site = Website()
        client = site.client()
        client.get_booking_slots(app.DRYER_TYPE_ID)
        self.assertEqual(client.get_balance(), "81.00")
        self.assertEqual(site.scope, "own")

    def test_create_returns_cancel_id_for_exact_requested_machine(self):
        site = Website()
        client = site.client()
        client.get_booking_slots(app.DRYER_TYPE_ID)
        nr = client.create_timed_booking(
            app.WASHER_TYPE_ID, f"{site.slot:%Y-%m-%d}", "12:00", "12:59"
        )
        self.assertEqual(nr, "101")
        self.assertEqual(site.mine[0]["machine"], "Washer")
        self.assertEqual(site.calls.count("CreateBooking.php"), 1)

    def test_create_verifies_committed_http_500(self):
        site = Website()
        site.create_status = 500
        client = site.client()
        self.assertEqual(
            client.create_timed_booking(94, f"{site.slot:%Y-%m-%d}", "12:17", "12:57"),
            "101",
        )

    def test_create_verifies_timeout_without_resubmitting(self):
        site = Website()
        site.timeout_after_commit = True
        client = site.client()
        self.assertEqual(
            client.create_timed_booking(93, f"{site.slot:%Y-%m-%d}", "12:00", "12:59"),
            "101",
        )
        self.assertEqual(site.calls.count("CreateBooking.php"), 1)

    def test_failed_preflight_does_not_create(self):
        site = Website()
        site.reject = True
        client = site.client()
        self.assertIsNone(
            client.create_timed_booking(93, f"{site.slot:%Y-%m-%d}", "12:00", "12:59")
        )
        self.assertNotIn("CreateBooking.php", site.calls)

    def test_no_submission_without_baseline(self):
        site = Website()
        site.malformed_overview = True
        client = site.client()
        self.assertIsNone(
            client.create_timed_booking(93, f"{site.slot:%Y-%m-%d}", "12:00", "12:59")
        )
        self.assertNotIn("CreateBooking.php", site.calls)

    def test_cancel_own_booking_after_room_read(self):
        site = Website()
        site.mine = [booking()]
        client = site.client()
        client.get_location_bookings()
        self.assertTrue(client.cancel_booking("100"), client.last_error)
        self.assertEqual(site.mine, [])
        self.assertEqual(len(site.room), 1)

    def test_cancel_accepts_activity_log_booking_number(self):
        site = Website()
        site.mine = [booking()]
        client = site.client()
        self.assertTrue(client.cancel_booking("200"), client.last_error)
        self.assertEqual(site.mine, [])

    def test_foreign_and_running_bookings_cannot_be_cancelled(self):
        for nr, status in [("999", "BookingReady"), ("100", "BookingBusy")]:
            with self.subTest(nr=nr):
                site = Website()
                site.mine = [booking(status=status)]
                client = site.client()
                self.assertFalse(client.cancel_booking(nr))
                self.assertNotIn("DeleteBooking.php", site.calls)

    def test_cancel_rejects_injected_id(self):
        site = Website()
        client = site.client()
        self.assertFalse(client.cancel_booking("100&ResNr=999"))
        self.assertEqual(site.calls, [])

    def test_auth_expiry_during_confirmation_does_not_resume_transaction(self):
        site = Website()
        site.expire_at = "ConfirmCreateBooking.php"
        client = site.client()
        client.login = Mock(return_value=True)
        self.assertIsNone(
            client.create_timed_booking(93, f"{site.slot:%Y-%m-%d}", "12:00", "12:59")
        )
        self.assertNotIn("CreateBooking.php", site.calls)
        client.login.assert_not_called()

    def test_multiple_bookings_share_one_window(self):
        site = Website()
        client = site.client()
        result = client.book_multiple(93, 2)
        self.assertEqual(len(result), 2, client.last_error)
        self.assertEqual({b["start"] for b in site.mine}, {site.slot})

    def test_cancel_timeout_verifies_without_repeating_delete(self):
        site = Website()
        site.mine = [booking()]
        client = site.client()
        original = site.get

        def timeout(url, **kwargs):
            result = original(url, **kwargs)
            if "DeleteBooking.php" in url:
                raise app.requests.ReadTimeout("after delete")
            return result

        client.session.get.side_effect = timeout
        self.assertTrue(client.cancel_booking("100"), client.last_error)
        self.assertEqual(site.calls.count("DeleteBooking.php"), 1)

    def test_unrelated_new_booking_cannot_confirm_requested_booking(self):
        site = Website()
        client = site.client()
        original = site.get

        def wrong_machine(url, **kwargs):
            result = original(url, **kwargs)
            if "CreateBooking.php?" in url and "ConfirmCreateBooking" not in url:
                site.mine[-1]["machine"] = "Dryer"
            return result

        client.session.get.side_effect = wrong_machine
        self.assertIsNone(
            client.create_timed_booking(93, f"{site.slot:%Y-%m-%d}", "12:00", "12:59")
        )
        self.assertIn("unconfirmed", client.last_error)
        self.assertEqual(site.calls.count("CreateBooking.php"), 1)

    def test_cancel_preflight_must_match_requested_id(self):
        site = Website()
        site.mine = [booking()]
        client = site.client()
        original = site.get

        def wrong_id(url, **kwargs):
            if "AnnouncmentBooking.php" in url:
                return response(
                    "<button id='BtnRemoveBooking'></button><script>var ResNr='999';</script>"
                )
            return original(url, **kwargs)

        client.session.get.side_effect = wrong_id
        self.assertFalse(client.cancel_booking("100"))
        self.assertNotIn("DeleteBooking.php", site.calls)

    def test_room_page_never_exposes_cancellation_identifiers(self):
        site = Website()
        client = site.client()
        rows = client.get_location_bookings()
        self.assertTrue(rows)
        self.assertTrue(
            all("id" not in row and "booking_nr" not in row for row in rows)
        )

    def test_invalid_counts_and_past_dates_make_no_requests(self):
        site = Website()
        client = site.client()
        for count in [0, -1, 1000000]:
            self.assertEqual(client.book_multiple(93, count), [])
        self.assertIsNone(
            client.create_timed_booking(93, "2000-01-01", "12:00", "12:59")
        )
        self.assertEqual(site.calls, [])


class Reliability(unittest.TestCase):
    def test_failed_login_never_fetches_protected_resource(self):
        client = app.DUWOClient()
        client.login = Mock(return_value=False)
        client.session.get = Mock()
        with self.assertRaises(RuntimeError):
            client._get(app.BASE_URL + "/UserLog.php")
        client.session.get.assert_not_called()

    def test_http_500_balance_is_not_fresh(self):
        client = app.DUWOClient()
        client.logged_in = True
        client.last_known_balance = "8.00"
        client.session.get = Mock(
            return_value=response('<span id="LblUserCredits">3.00</span>', 500)
        )
        self.assertEqual(client.get_balance(), "8.00")
        self.assertTrue(client.balance_stale)

    def test_locale_amounts_and_negative_balance(self):
        client = app.DUWOClient()
        for text, expected in [
            ("1.234,56", "1234.56"),
            ("1,234.56", "1234.56"),
            ("-2,50", "-2.50"),
            ("0", "0.00"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(
                    client._extract_balance(f'<span id="LblUserCredits">{text}</span>'),
                    expected,
                )

    def test_timer_runs_even_when_website_is_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = app.CycleTracker(tmp + "/state.json")
            c = app.Cycle(
                "1",
                "Washer",
                datetime.now() - timedelta(minutes=56),
                notified_start=True,
            )
            tracker.cycles[c.booking_nr] = c
            duwo = Mock()
            duwo.get_recent_cycles.return_value = None
            bot = Mock()
            bot.send.return_value = True
            app.poll_cycles(duwo, bot, tracker, False)
            self.assertTrue(c.notified_done)
            self.assertTrue(any("ready" in x.args[0] for x in bot.send.call_args_list))

    def test_failed_notification_is_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = app.CycleTracker(tmp + "/state.json")
            c = app.Cycle(
                "1",
                "Washer",
                datetime.now() - timedelta(minutes=56),
                notified_start=True,
            )
            tracker.cycles[c.booking_nr] = c
            duwo = Mock()
            duwo.get_recent_cycles.return_value = []
            bot = Mock()
            bot.send.return_value = False
            app.poll_cycles(duwo, bot, tracker, False)
            self.assertFalse(c.notified_done)
            bot.send.return_value = True
            app.poll_cycles(duwo, bot, tracker, False)
            self.assertTrue(c.notified_done)

    def test_first_poll_still_schedules_running_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = app.CycleTracker(tmp + "/state.json")
            cycle = app.Cycle("1", "Washer", datetime.now() - timedelta(minutes=10))
            tracker.merge([cycle], suppress=True)
            self.assertTrue(cycle.notified_start)
            self.assertFalse(cycle.notified_done)

    def test_pruning_is_persisted_even_without_new_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = app.CycleTracker(tmp + "/state.json")
            tracker.cycles["1"] = app.Cycle(
                "1",
                "Washer",
                datetime.now() - timedelta(days=2),
                notified_start=True,
                notified_headsup=True,
                notified_done=True,
            )
            tracker.save()
            duwo = Mock()
            duwo.get_recent_cycles.return_value = []
            app.poll_cycles(duwo, Mock(), tracker, False)
            self.assertEqual(app.CycleTracker(tracker.path).cycles, {})

    def test_exact_paid_id_wins_over_unrelated_nearby_debit(self):
        now = datetime.now()
        rows = [
            dict(at=now, action="Depreciation on location", info="Washing mach"),
            dict(at=now, action="Booking Started", info="BookingNR : 23"),
            dict(at=now, action="Booking Payd", info="123 -> 200"),
            dict(at=now, action="Booking Payd", info="23 -> 100"),
        ]
        self.assertEqual(app.DUWOClient._machine_from_context(rows, 1, "23"), "Dryer")

    def test_bad_state_shape_does_not_crash_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("[]")
            tracker = app.CycleTracker(str(path))
            self.assertFalse(tracker.loaded)


if __name__ == "__main__":
    unittest.main()
