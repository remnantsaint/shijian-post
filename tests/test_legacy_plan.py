import base64
import contextlib
import copy
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scripts.github_api import GitHubAPIError
from scripts.legacy_plan import (
    MAX_MINUTES, STATE_BRANCH, STATE_PATH, SchedulerError,
    format_time, interval_minutes, parse_time, requested_bounds,
    validate_bounds,
)


NOW = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)


def state_at(target=NOW, mode="once"):
    return {
        "version": 1, "enabled": True, "mode": mode,
        "min_minutes": 1, "max_minutes": MAX_MINUTES,
        "next_send_at": format_time(target), "state": "scheduled",
        "claim_run_id": None, "claim_run_attempt": None,
        "last_send": None, "history": [],
    }


class BoundsTests(unittest.TestCase):
    def test_full_supported_range(self):
        self.assertEqual(validate_bounds(1, 129600), (1, 129600))
        self.assertEqual(validate_bounds("1", "1"), (1, 1))

    def test_reject_invalid_bounds_without_echoing_input(self):
        for lower, upper in ((0, 1), (-1, 1), (2, 1), (1, 129601),
                             (True, 4), ("private-value", 4), (1.5, 4)):
            with self.subTest(lower=lower, upper=upper):
                with self.assertRaises(SchedulerError) as result:
                    validate_bounds(lower, upper)
                self.assertNotIn("private-value", str(result.exception))

    def test_units_and_default_range(self):
        self.assertEqual(interval_minutes(2, "hours"), 120)
        self.assertEqual(interval_minutes(90, "days"), 129600)
        self.assertEqual(requested_bounds({}), (1, 129600))
        self.assertEqual(requested_bounds({
            "MIN_INTERVAL": "3", "MIN_UNIT": "hours",
            "MAX_INTERVAL": "2", "MAX_UNIT": "days",
        }), (180, 2880))
        self.assertEqual(requested_bounds({"MIN_MINUTES": "5", "MAX_MINUTES": "6"}), (5, 6))
        with self.assertRaises(SchedulerError):
            interval_minutes(1, "private-value")
        with self.assertRaises(SchedulerError):
            requested_bounds({"MIN_INTERVAL": "2", "MIN_UNIT": "days",
                              "MAX_INTERVAL": "1", "MAX_UNIT": "days"})

    def test_utc_aware_timestamps_required(self):
        self.assertEqual(parse_time("2026-10-12T18:00:00+08:00"), NOW)
        self.assertEqual(parse_time(format_time(NOW)), NOW)
        with self.assertRaises(SchedulerError):
            parse_time("2026-10-12T10:00:00")


