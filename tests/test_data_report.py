"""Tests for server/data_report.py's pure parts. Standard-library unittest, like
test_guardian.py. Each case is a way the daily data report could be silently wrong."""
import datetime as dt
import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))
spec = importlib.util.spec_from_file_location("data_report", REPO / "server" / "data_report.py")
dr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dr)


class TestDayNumbering(unittest.TestCase):
    def test_window_is_21_days_starting_10_06(self):
        self.assertEqual(dr.day_number(dt.date(2026, 10, 6)), 1)
        self.assertEqual(dr.day_number(dt.date(2026, 10, 26)), 21)


class TestStopDate(unittest.TestCase):
    def test_seventh_post_is_the_last(self):
        # scheduled posts 10-09 .. 10-15 report sessions 10-08 .. 10-14
        posts = [dt.date(2026, 10, 9) + dt.timedelta(days=i) for i in range(7)]
        self.assertEqual([dr.should_stop(d) for d in posts], [False] * 6 + [True])


class TestSegmentStats(unittest.TestCase):
    def test_steady_10hz_has_no_gaps(self):
        segs = [(t, 6000) for t in range(0, 6000, 600)]
        st = dr.segment_stats(segs, stop=6000)
        self.assertEqual(st["median_hz"], 10.0)
        self.assertEqual(st["gaps"], [])

    def test_missing_segment_is_a_gap(self):
        segs = [(0, 6000), (600, 6000), (1800, 6000)]       # 1200..1800 never recorded
        st = dr.segment_stats(segs, stop=2400)
        self.assertEqual(len(st["gaps"]), 1)

    def test_short_segment_is_a_gap_but_a_short_dip_is_not(self):
        st = dr.segment_stats([(0, 2000), (600, 6000)], stop=1200)   # 400 of 600 s missing
        self.assertEqual(len(st["gaps"]), 1)
        st = dr.segment_stats([(0, 5000), (600, 6000)], stop=1200)   # 100 s missing
        self.assertEqual(st["gaps"], [])

    def test_open_last_segment_without_stop_is_not_rated(self):
        st = dr.segment_stats([(0, 6000), (600, 10)], stop=None)
        self.assertEqual(st["median_hz"], 10.0)


if __name__ == "__main__":
    unittest.main()
