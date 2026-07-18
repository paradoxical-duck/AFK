"""Phase 5 statistics tests — isolated temp data dir, no models."""

import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))


class TestStats(unittest.TestCase):
    def setUp(self):
        os.environ["AFK_DATA_DIR"] = tempfile.mkdtemp()
        from afk_backend.statistics import StatsStore
        self.s = StatsStore()

    def test_basic_metrics(self):
        self.s.record_dictation(words=120, recording_seconds=60.0, transcribe_ms=1500)
        snap = self.s.snapshot()
        self.assertEqual(snap["words"]["lifetime"], 120)
        self.assertEqual(snap["words"]["today"], 120)
        self.assertEqual(snap["wpm_avg"], 120.0)          # 120 words / 1 min
        self.assertEqual(snap["typing_minutes_saved"], 3.0)  # 120 / 40 wpm
        self.assertEqual(snap["recordings"], 1)
        self.assertEqual(snap["avg_transcription_latency_ms"], 1500)

    def test_longest_and_avg_recording(self):
        self.s.record_dictation(10, 5.0, 100)
        self.s.record_dictation(20, 25.0, 200)
        snap = self.s.snapshot()
        self.assertEqual(snap["longest_recording_sec"], 25.0)
        self.assertEqual(snap["avg_recording_sec"], 15.0)

    def test_clarify_latency(self):
        self.s.record_clarification(1000)
        self.s.record_clarification(3000)
        snap = self.s.snapshot()
        self.assertEqual(snap["clarifications"], 2)
        self.assertEqual(snap["avg_clarify_latency_ms"], 2000)

    def test_streak_consecutive(self):
        today = date.today()
        # simulate activity yesterday with a 3-day streak, then today
        self.s._data["streak"] = {"current": 3, "longest": 3, "last_day": (today - timedelta(days=1)).isoformat()}
        self.s._update_streak(today.isoformat())
        self.assertEqual(self.s._data["streak"]["current"], 4)
        self.assertEqual(self.s._data["streak"]["longest"], 4)

    def test_streak_broken(self):
        today = date.today()
        self.s._data["streak"] = {"current": 5, "longest": 5, "last_day": (today - timedelta(days=3)).isoformat()}
        self.s._update_streak(today.isoformat())
        self.assertEqual(self.s._data["streak"]["current"], 1)
        self.assertEqual(self.s._data["streak"]["longest"], 5)

    def test_streak_not_current_if_stale(self):
        today = date.today()
        self.s._data["streak"] = {"current": 5, "longest": 5, "last_day": (today - timedelta(days=4)).isoformat()}
        self.assertEqual(self.s.snapshot()["streak_current"], 0)   # stale
        self.assertEqual(self.s.snapshot()["streak_longest"], 5)

    def test_persistence(self):
        self.s.record_dictation(50, 30.0, 500)
        from afk_backend.statistics import StatsStore
        s2 = StatsStore()  # reload from disk
        self.assertEqual(s2.snapshot()["words"]["lifetime"], 50)

    def test_reset(self):
        self.s.record_dictation(50, 30.0, 500)
        self.s.reset()
        self.assertEqual(self.s.snapshot()["words"]["lifetime"], 0)

    def test_activity_is_continuous_and_includes_today(self):
        self.s.record_dictation(50, 30.0, 500)
        activity = self.s.snapshot()["activity"]
        self.assertEqual(len(activity), 14)
        self.assertEqual(activity[-1]["date"], date.today().isoformat())
        self.assertEqual(activity[-1]["words"], 50)
        self.assertEqual(activity[-1]["recordings"], 1)
        self.assertEqual(activity[-1]["recording_seconds"], 30.0)

        dates = [date.fromisoformat(item["date"]) for item in activity]
        self.assertTrue(all((b - a).days == 1 for a, b in zip(dates, dates[1:])))


if __name__ == "__main__":
    unittest.main()
