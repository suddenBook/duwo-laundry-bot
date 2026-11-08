from datetime import datetime, timedelta
import unittest
from unittest.mock import Mock

from test_regressions import app, response
from test_flows import Website, booking, userlog_html


def start(at, number, machine, paid=True, debit=True):
    """Activity log rows of one start at the payment terminal, oldest first."""
    price = "2,00" if machine == "Washer" else "1,00"
    label = "Washing Mach." if machine == "Washer" else "Dryer"
    rows = [(at, "Request balance", "VM_DuwoSCP2", "")]
    if debit:
        rows.append((at, "Depreciation on location", f"VM_DuwoSCP2 &middot; {label}", f"-{price}"))
    rows += [
        (at, "Create booking", f"VM_DuwoSCP2 &middot; Booking {number}", ""),
        (at, "Booking started", f"VM_DuwoSCP2 &middot; Booking {number}", ""),
    ]
    if paid:
        rows.append(
            (at + timedelta(minutes=1), "Booking paid", f"VM_DuwoSCP2 &middot; Booking {number}: {price}", "")
        )
    return rows


class ActivityLog(unittest.TestCase):
    def client(self, entries):
        site = Website()
        site.log = entries
        return site.client()

    def test_starts_are_read_with_their_machine_and_minute(self):
        at = datetime.now().replace(second=0, microsecond=0) - timedelta(minutes=20)
        login = (at - timedelta(minutes=5), "Login", "Website &middot; IP address 192.0.2.1", "")
        entries = [login] + start(at, "8170533", "Washer") + start(at, "8170534", "Washer")
        entries += start(at + timedelta(minutes=2), "8170797", "Dryer", paid=False)
        cycles = self.client(entries).get_recent_cycles()
        self.assertEqual(
            [(c.booking_nr, c.machine, c.started_at) for c in cycles],
            [
                ("8170533", "Washer", at),
                ("8170534", "Washer", at),
                ("8170797", "Dryer", at + timedelta(minutes=2)),
            ],
        )

    def test_start_across_midnight_keeps_its_own_day(self):
        at = datetime.now().replace(second=0, microsecond=0) - timedelta(hours=1)
        yesterday = (at - timedelta(days=1), "Login", "Website", "")
        cycles = self.client([yesterday] + start(at, "1", "Dryer")).get_recent_cycles()
        self.assertEqual(cycles[0].started_at, at)

    def test_unknown_price_without_a_named_debit_is_skipped(self):
        at = datetime.now().replace(second=0, microsecond=0) - timedelta(minutes=5)
        entries = start(at, "77", "Washer", debit=False)
        entries[-1] = (entries[-1][0], "Booking paid", "VM_DuwoSCP2 &middot; Booking 77: 2,25", "")
        self.assertEqual(self.client(entries).get_recent_cycles(), [])

    def test_empty_log_is_not_an_error(self):
        self.assertEqual(self.client([]).get_recent_cycles(), [])

    def test_unrecognised_layout_fails_loudly_and_alerts_once(self):
        alerts = []
        client = app.DUWOClient(notify_callback=alerts.append)
        client.logged_in = True
        page = "<table id='UserLogTable'><tr><td>2026-11-08 16:12:28</td><td>Booking Started</td><td>x</td></tr></table>"
        client.session.get = Mock(return_value=response(page))
        self.assertIsNone(client.get_recent_cycles())
        self.assertIsNone(client.get_recent_cycles())
        self.assertEqual(len(alerts), 1)
        self.assertIn("activity log", alerts[0])

    def test_day_headers_parse_english_and_dutch_months(self):
        self.assertEqual(app.DUWOClient._log_day("Sun 8 Nov 2026"), datetime(2026, 11, 8))
        self.assertEqual(app.DUWOClient._log_day("zo 25 okt 2026"), datetime(2026, 10, 25))
        with self.assertRaises(ValueError):
            app.DUWOClient._log_day("Today")


class Pages(unittest.TestCase):
    def test_availability_counts(self):
        rows = "".join(
            f"<tr><td>ROOM</td><td>{kind}</td><td><p class='{cls}'>{text}</p></td></tr>"
            for kind, cls, text in [
                ("Washing mach", "TxtAvailable", "1 available"),
                ("Dryer", "TxtNotAvailable", "In use"),
                ("Washing mach", "", "Available : 3"),
                ("Dryer", "", "Not Available"),
                ("Dryer", "", "Offline"),
            ]
        )
        client = app.DUWOClient()
        client.logged_in = True
        client.session.get = Mock(
            return_value=response(f"<div id='MachineAvailabilityTable'><table>{rows}</table></div>")
        )
        self.assertEqual(
            [m.available_count for m in client.get_availability()], [1, 0, 3, 0, None]
        )

    def test_empty_booking_table_is_an_empty_schedule(self):
        self.assertEqual(Website().client().get_own_bookings(include_inactive=True), [])

    def test_booking_rows_keep_both_identifiers(self):
        site = Website()
        site.mine = [booking("8211870", "Dryer", booking_nr="8170994")]
        rows = site.client().get_own_bookings()
        self.assertEqual((rows[0]["id"], rows[0]["booking_nr"], rows[0]["machine"]), ("8211870", "8170994", "Dryer"))

    def test_machine_names_match_whole_words(self):
        name = app.DUWOClient._machine_name
        self.assertEqual(name("Washing Mach."), "Washer")
        self.assertEqual(name("Dryer"), "Dryer")
        self.assertIsNone(name("VM_Laundry2"))
        self.assertIsNone(name("VM_DuwoSCP2"))

    def test_qr_text_comes_from_the_code_holder(self):
        client = app.DUWOClient()
        client.logged_in = True
        page = (
            '<div id="QR_container" data-qrtext="&lt;ID&gt;42&lt;/ID&gt;"></div>'
            '<script>$("#QR_container").qrcode({"size": 116, "text": "<ID>42</ID>"});</script>'
        )
        client.session.get = Mock(return_value=response(page))
        self.assertEqual(client.get_qr_text(), "<ID>42</ID>")


if __name__ == "__main__":
    unittest.main()
