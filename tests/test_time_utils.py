import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scripts.time_utils import BEIJING, UTC, format_beijing, format_utc, parse_utc


class TimeTests(unittest.TestCase):
    def test_beijing_crosses_midnight_month_year_and_leap_day_correctly(self):
        cases = [
            ("2026-10-12T16:00:00Z", "2026-10-13 00:00:00"),
            ("2026-10-31T16:00:00Z", "2026-11-01 00:00:00"),
            ("2026-12-31T16:00:00Z", "2027-01-01 00:00:00"),
            ("2028-02-28T16:00:00Z", "2028-02-29 00:00:00"),
        ]
        for utc, expected in cases:
            with self.subTest(utc=utc):
                self.assertEqual(format_beijing(parse_utc(utc)), expected)

    def test_offsets_describe_the_same_real_instant(self):
        utc = parse_utc("2026-10-12T16:00:00Z")
        local = parse_utc("2026-10-13T00:00:00+08:00")
        self.assertEqual(utc, local)
        self.assertEqual(format_utc(local), "2026-10-12T16:00:00Z")

    def test_storage_does_not_round_timestamps_or_shorten_intervals(self):
        instant = datetime(2026, 10, 13, 0, 0, 0, 987654, tzinfo=BEIJING)
        self.assertEqual(parse_utc(format_utc(instant)), instant)
        self.assertIn(".987654Z", format_utc(instant))

    def test_missing_timezone_is_rejected_instead_of_guessing_host_timezone(self):
        naive = datetime(2026, 10, 13, 0, 0)
        with self.assertRaises(ValueError):
            parse_utc("2026-10-13T00:00:00")
        for formatter in (format_utc, format_beijing):
            with self.subTest(formatter=formatter.__name__), self.assertRaises(ValueError):
                formatter(naive)

    @unittest.skipUnless(hasattr(time, "tzset"), "Host timezone switching needs tzset")
    def test_host_timezone_does_not_change_beijing_display(self):
        instant = datetime(2026, 10, 12, 16, 0, tzinfo=UTC)
        try:
            for host_timezone in ("UTC", "Asia/Tokyo", "America/New_York"):
                with self.subTest(host_timezone=host_timezone), patch.dict(os.environ, {"TZ": host_timezone}):
                    time.tzset()
                    self.assertEqual(format_beijing(instant), "2026-10-13 00:00:00")
        finally:
            time.tzset()
