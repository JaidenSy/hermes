"""
test_scheduler.py — the self-scheduler: when-parsing, add/list/remove, and the
due→fire→advance logic. SCHEDULES_FILE is patched to a tmp; `now` is injected so
time-based assertions are deterministic (no wall-clock flakiness).
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scheduler  # noqa: E402

NOW = datetime(2026, 7, 22, 12, 0, 0)


class TestParseWhen(unittest.TestCase):
    def test_in_delay(self):
        kind, nxt, interval = scheduler.parse_when("in 2h", NOW)
        self.assertEqual((kind, interval), ("once", None))
        self.assertEqual(nxt, NOW + timedelta(hours=2))

    def test_every_interval(self):
        kind, nxt, interval = scheduler.parse_when("every 30m", NOW)
        self.assertEqual((kind, interval), ("repeat", 1800))
        self.assertEqual(nxt, NOW + timedelta(minutes=30))

    def test_daily_rolls_to_tomorrow_if_past(self):
        kind, nxt, interval = scheduler.parse_when("daily 09:00", NOW)  # 09:00 < 12:00
        self.assertEqual((kind, interval), ("repeat", 86400))
        self.assertEqual(nxt, NOW.replace(hour=9, minute=0) + timedelta(days=1))

    def test_at_later_today(self):
        _, nxt, _ = scheduler.parse_when("at 15:30", NOW)
        self.assertEqual(nxt, NOW.replace(hour=15, minute=30))

    def test_bad_spec_raises(self):
        with self.assertRaises(ValueError):
            scheduler.parse_when("sometime soon", NOW)
        with self.assertRaises(ValueError):
            scheduler.parse_when("daily 33:00", NOW)


class TestCommands(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._f = Path(self._tmp.name) / "schedules.json"
        self._p = patch.object(scheduler, "SCHEDULES_FILE", self._f)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def test_add_then_list_then_remove(self):
        out = scheduler.add_schedule("every 1h | on alphabot, check", NOW)
        self.assertIn("Scheduled", out)
        self.assertEqual(len(scheduler.load_schedules()), 1)
        self.assertIn("alphabot", scheduler.list_schedules())
        self.assertIn("Removed", scheduler.remove_schedule("1"))
        self.assertEqual(scheduler.load_schedules(), [])

    def test_add_missing_delimiter_is_usage(self):
        self.assertIn("Usage", scheduler.add_schedule("in 2h do the thing", NOW))
        self.assertEqual(scheduler.load_schedules(), [])

    def test_add_bad_spec_is_error(self):
        self.assertIn("can't read", scheduler.add_schedule("whenever | do it", NOW))
        self.assertEqual(scheduler.load_schedules(), [])

    def test_remove_out_of_range(self):
        scheduler.add_schedule("in 1h | x", NOW)
        self.assertIn("No schedule matches", scheduler.remove_schedule("9"))


class TestDueAndTick(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._f = Path(self._tmp.name) / "schedules.json"
        self._p = patch.object(scheduler, "SCHEDULES_FILE", self._f)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def _s(self, sid, kind, next_run, interval=None, task="t"):
        return {
            "id": sid,
            "when": "x",
            "kind": kind,
            "next_run": next_run.isoformat(),
            "interval_s": interval,
            "task": task,
        }

    def test_due_split_advances_repeat_drops_once(self):
        scheds = [
            self._s("a", "once", NOW - timedelta(minutes=1)),  # due, one-shot
            self._s("b", "repeat", NOW - timedelta(minutes=5), 60),  # due, repeat
            self._s("c", "once", NOW + timedelta(hours=1)),  # not due
        ]
        fired, remaining = scheduler.due_schedules(scheds, NOW)
        self.assertEqual({s["id"] for s in fired}, {"a", "b"})
        ids = {s["id"] for s in remaining}
        self.assertEqual(ids, {"b", "c"})  # once 'a' dropped, repeat 'b' kept
        b = next(s for s in remaining if s["id"] == "b")
        self.assertGreater(datetime.fromisoformat(b["next_run"]), NOW)  # advanced past now

    def test_tick_dispatches_and_persists(self):
        scheduler.save_schedules(
            [
                self._s("a", "once", NOW - timedelta(seconds=1), task="do a"),
                self._s("b", "once", NOW + timedelta(hours=2), task="do b"),
            ]
        )
        fired_tasks = []
        sched = scheduler.Scheduler(lambda t: fired_tasks.append(t))
        n = sched.tick(NOW)
        self.assertEqual(n, 1)
        self.assertEqual(fired_tasks, ["do a"])
        remaining = scheduler.load_schedules()
        self.assertEqual([s["id"] for s in remaining], ["b"])  # 'a' fired+dropped


if __name__ == "__main__":
    unittest.main()
