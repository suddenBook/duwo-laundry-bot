from datetime import datetime, timedelta
import json
import os
import tempfile
import time
import unittest
from unittest.mock import Mock

from test_regressions import app


class Done(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = app.CycleTracker(self.tmp.name + "/state.json")
        self.now = datetime.now()
        self.estimate = self.tracker.minutes_for("Washer")

    def tearDown(self):
        self.tmp.cleanup()

    def washer(self, number, minutes_ago):
        cycle = app.Cycle(number, "Washer", self.now - timedelta(minutes=minutes_ago))
        self.tracker.cycles[number] = cycle
        return cycle

    def test_done_closes_the_load_that_is_due_not_the_newest(self):
        first = self.washer("1", self.estimate + 1)
        second = self.washer("2", self.estimate - 20)
        batch, observed, learned = self.tracker.record_collected(self.now)
        self.assertEqual(batch, [first])
        self.assertAlmostEqual(observed, self.estimate + 1, places=3)
        self.assertIsNotNone(learned)
        self.assertIsNone(second.collected_at)

    def test_back_to_back_loads_are_emptied_together(self):
        loads = [self.washer("1", self.estimate + 3), self.washer("2", self.estimate)]
        loads.append(self.washer("3", self.estimate - 3))
        batch, _, _ = self.tracker.record_collected(self.now)
        self.assertEqual(batch, loads)

    def test_a_load_just_restarted_does_not_hide_the_finished_one(self):
        finished = self.washer("1", self.estimate + 5)
        restarted = self.washer("2", 2)
        batch, _, _ = self.tracker.record_collected(self.now)
        self.assertEqual(batch, [finished])
        self.assertIsNone(restarted.collected_at)

    def test_implausible_time_closes_without_learning(self):
        self.washer("1", self.estimate * 2)
        batch, observed, learned = self.tracker.record_collected(self.now)
        self.assertEqual(len(batch), 1)
        self.assertIsNone(learned)
        self.assertEqual(self.tracker.minutes_for("Washer"), self.estimate)


class Merging(unittest.TestCase):
    def test_machine_seen_long_after_it_finished_is_closed_quietly(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = app.CycleTracker(tmp + "/state.json")
            old = app.Cycle("1", "Washer", datetime.now() - timedelta(minutes=120))
            fresh = app.Cycle("2", "Washer", datetime.now() - timedelta(minutes=10))
            duwo = Mock()
            duwo.get_recent_cycles.return_value = [old, fresh]
            bot = Mock()
            bot.send.return_value = True
            app.poll_cycles(duwo, bot, tracker, suppress=False)
        self.assertIsNotNone(old.collected_at)
        sent = [call.args[0] for call in bot.send.call_args_list]
        self.assertEqual(len(sent), 1)
        self.assertIn("Washer started", sent[0])


class LearnedDuration(unittest.TestCase):
    def write(self, path, **data):
        with open(path, "w") as handle:
            json.dump(data, handle)

    def test_learned_value_survives_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + "/state.json"
            base = float(app.WASHER_CYCLE_MINUTES)
            self.write(path, duration={"Washer": base + 3.5}, duration_base={"Washer": base})
            self.assertEqual(app.CycleTracker(path).minutes_for("Washer"), base + 3.5)

    def test_state_without_a_base_keeps_its_learned_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + "/state.json"
            self.write(path, duration={"Washer": 58.4})
            self.assertEqual(app.CycleTracker(path).minutes_for("Washer"), 58.4)

    def test_changing_the_configured_duration_starts_learning_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + "/state.json"
            self.write(path, duration={"Washer": 70.0}, duration_base={"Washer": 30.0})
            tracker = app.CycleTracker(path)
            self.assertEqual(tracker.minutes_for("Washer"), float(app.WASHER_CYCLE_MINUTES))
            tracker.save()
            with open(path) as handle:
                saved = json.load(handle)
            self.assertEqual(saved["duration_base"], {"Washer": float(app.WASHER_CYCLE_MINUTES)})

    def test_help_shows_the_estimate_in_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = app.CycleTracker(tmp + "/state.json")
            tracker.duration["Washer"] = 61.6
            self.assertIn("washer 62 min", app.help_text(tracker))


class UnreadableLog(unittest.TestCase):
    def test_cycles_says_when_it_only_has_saved_timers(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = app.CycleTracker(tmp + "/state.json")
            tracker.cycle_baseline_set = True
            duwo = Mock()
            duwo.get_recent_cycles.return_value = None
            bot = Mock()
            app.handle_command("/cycles", duwo, bot, tracker)
        text = bot.send.call_args.args[0]
        self.assertIn("could not be read", text)
        self.assertIn("Nothing of yours is running", text)


class DaylightSaving(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Amsterdam"
        time.tzset()

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.saved
        time.tzset()

    def test_estimate_counts_real_minutes_across_the_autumn_change(self):
        # 02:30 summer time plus 55 real minutes is 02:25 winter time.
        start = datetime(2026, 10, 25, 2, 30)
        self.assertEqual(app._after(start, 55), datetime(2026, 10, 25, 2, 25))
        self.assertEqual(app._minutes_between(datetime(2027, 3, 28, 1, 30), datetime(2027, 3, 28, 3, 0)), 30)


if __name__ == "__main__":
    unittest.main()
