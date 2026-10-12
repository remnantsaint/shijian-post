import base64
import contextlib
import copy
import io
import json
import unittest
from datetime import datetime, timedelta, timezone

from scripts.github_api import GitHubAPIError
from scripts.random_scheduler import (
    MAX_MINUTES, STATE_BRANCH, STATE_PATH, Scheduler, SchedulerError,
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


class MockAPI:
    repo = "owner/mail-bot"

    def __init__(self, state=None, *, branch_exists=True):
        self.state = copy.deepcopy(state)
        self.branch_exists = branch_exists
        self.sha = "state-1" if state is not None else None
        self.calls = []
        self.events = []
        self.fail_write = False
        self.fail_disable = False

    def request(self, method, path, data=None):
        self.calls.append((method, path, copy.deepcopy(data)))
        if path.endswith("/disable"):
            self.events.append("disable")
            if self.fail_disable:
                raise GitHubAPIError("forbidden", status=403)
            return None
        if f"/contents/{STATE_PATH}" in path:
            if method == "GET":
                if self.state is None:
                    raise GitHubAPIError("not found", status=404)
                encoded = base64.b64encode(json.dumps(self.state).encode()).decode()
                return {"encoding": "base64", "content": encoded, "sha": self.sha}
            if self.fail_write or data.get("sha") != self.sha:
                raise GitHubAPIError("conflict", status=409)
            self.state = json.loads(base64.b64decode(data["content"]))
            self.sha = f"state-{len(self.calls)}"
            self.events.append(f"persist:{self.state['state']}")
            return {"content": {"sha": self.sha}}
        if path.endswith(f"/git/ref/heads/{STATE_BRANCH}"):
            if not self.branch_exists:
                raise GitHubAPIError("not found", status=404)
            return {"object": {"sha": "state-head"}}
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "main-head"}}
        if path.endswith("/git/refs") and method == "POST":
            self.branch_exists = True
            self.events.append("create-branch")
            return {"object": {"sha": data["sha"]}}
        if path == f"/repos/{self.repo}":
            return {"default_branch": "main"}
        raise AssertionError(f"Unexpected mock API request: {method} {path}")


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


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()

    def tearDown(self):
        self.quiet.__exit__(None, None, None)

    def scheduler(self, api, run_id="42", run_attempt=1, minutes=5):
        return Scheduler(api, run_id, run_attempt=run_attempt,
                         now=lambda: NOW, choose_delay=lambda lower, upper: minutes,
                         output=lambda due: api.events.append(f"send_due:{str(due).lower()}"))

    def puts(self, api):
        return [call for call in api.calls if call[0] == "PUT" and "/contents/" in call[1]]

    def test_new_plan_creates_separate_branch_and_does_not_send(self):
        api = MockAPI(branch_exists=False)
        state = self.scheduler(api).prepare("plan_once", min_minutes=2, max_minutes=10)
        self.assertEqual(state["next_send_at"], format_time(NOW + timedelta(minutes=5)))
        self.assertEqual((state["min_minutes"], state["max_minutes"]), (2, 10))
        self.assertEqual(state["mode"], "once")
        self.assertEqual(api.events, ["create-branch", "persist:scheduled", "send_due:false"])
        creation = next(call for call in api.calls if call[0] == "POST")
        self.assertEqual(creation[2], {"ref": "refs/heads/mail-bot-state", "sha": "main-head"})
        self.assertEqual(self.puts(api)[0][2]["branch"], "mail-bot-state")

    def test_status_does_not_create_or_send(self):
        api = MockAPI()
        self.assertIsNone(self.scheduler(api).prepare("status"))
        self.assertEqual(api.events, ["send_due:false"])
        self.assertFalse(self.puts(api))

    def test_no_plan_poll_disables_workflow(self):
        api = MockAPI()
        self.assertIsNone(self.scheduler(api).prepare("poll"))
        self.assertEqual(api.events, ["send_due:false", "disable"])

    def test_future_plan_does_not_claim_or_send(self):
        api = MockAPI(state_at(NOW + timedelta(seconds=1)))
        self.scheduler(api).prepare("poll")
        self.assertEqual(api.events, ["send_due:false"])
        self.assertFalse(self.puts(api))

    def test_due_at_boundary_persists_claim_before_send_output(self):
        api = MockAPI(state_at())
        self.scheduler(api, run_attempt=2).prepare("poll")
        self.assertEqual(api.events, ["persist:sending", "send_due:true"])
        self.assertEqual(api.state["claim_run_id"], "42")
        self.assertEqual(api.state["claim_run_attempt"], 2)
        self.assertEqual(self.puts(api)[0][2]["sha"], "state-1")

    def test_claim_write_conflict_never_exposes_send_output(self):
        api = MockAPI(state_at())
        api.fail_write = True
        with self.assertRaises(GitHubAPIError):
            self.scheduler(api).prepare("poll")
        self.assertNotIn("send_due:true", api.events)
        self.assertEqual(api.state["state"], "scheduled")

    def test_finalize_once_finishes_and_disables_after_persist(self):
        api = MockAPI(state_at())
        scheduler = self.scheduler(api)
        scheduler.prepare("poll")
        state = scheduler.finalize("success")
        self.assertEqual(api.events[-2:], ["persist:completed", "disable"])
        self.assertFalse(state["enabled"])
        self.assertIsNone(state["next_send_at"])
        self.assertEqual(state["last_send"], {
            "run_id": "42", "run_attempt": 1,
            "occurred_at": format_time(NOW), "status": "accepted",
            "time_source": "state_recorded",
        })
        self.assertEqual(state["history"], [state["last_send"]])

    def test_finalize_repeat_schedules_new_future_and_clears_claim(self):
        api = MockAPI(state_at(mode="repeat"))
        scheduler = self.scheduler(api)
        scheduler.prepare("poll")
        state = scheduler.finalize("success")
        self.assertTrue(state["enabled"])
        self.assertEqual(state["state"], "scheduled")
        self.assertEqual(state["next_send_at"], format_time(NOW + timedelta(minutes=5)))
        self.assertIsNone(state["claim_run_id"])
        self.assertNotIn("disable", api.events)

    def test_failed_or_unknown_smtp_pauses_without_retry(self):
        for outcome in ("failure", "cancelled", "skipped", "unknown"):
            with self.subTest(outcome=outcome):
                api = MockAPI(state_at(mode="repeat"))
                scheduler = self.scheduler(api)
                scheduler.prepare("poll")
                state = scheduler.finalize(outcome)
                self.assertFalse(state["enabled"])
                self.assertEqual(state["state"], "paused")
                self.assertEqual(state["history"][-1]["status"], "uncertain")
                scheduler.prepare("poll")
                self.assertEqual(api.events.count("send_due:true"), 1)
                self.assertEqual(api.events[-2:], ["send_due:false", "disable"])

    def test_failed_disable_retries_only_disable_on_later_poll(self):
        api = MockAPI(state_at())
        scheduler = self.scheduler(api)
        scheduler.prepare("poll")
        api.fail_disable = True
        with self.assertRaises(GitHubAPIError):
            scheduler.finalize("success")
        self.assertEqual(api.state["state"], "completed")
        writes = len(self.puts(api))
        api.fail_disable = False
        scheduler.prepare("poll")
        self.assertEqual(len(self.puts(api)), writes)
        self.assertEqual(api.events.count("send_due:true"), 1)
        self.assertEqual(api.events[-1], "disable")

    def test_stale_sending_result_is_uncertain_and_never_resent(self):
        api = MockAPI(state_at())
        self.scheduler(api).prepare("poll")
        api.events.clear()
        state = self.scheduler(api, run_id="43").prepare("poll")
        self.assertEqual(api.events, ["persist:paused", "send_due:false", "disable"])
        self.assertFalse(state["enabled"])
        self.assertEqual(state["history"][-1]["run_id"], "42")
        self.assertEqual(state["history"][-1]["status"], "uncertain")

    def test_sending_plan_cannot_be_overwritten_but_paused_can_be_explicitly_replaced(self):
        api = MockAPI(state_at())
        scheduler = self.scheduler(api)
        scheduler.prepare("poll")
        with self.assertRaises(SchedulerError):
            scheduler.prepare("plan_repeat")
        self.scheduler(api, run_id="43").prepare("poll")
        state = self.scheduler(api, run_id="44").prepare("plan_repeat")
        self.assertEqual(state["state"], "scheduled")
        self.assertEqual(state["history"][-1]["status"], "uncertain")
        self.assertGreater(parse_time(state["next_send_at"]), NOW)

    def test_finalize_requires_same_run_and_attempt(self):
        api = MockAPI(state_at())
        self.scheduler(api).prepare("poll")
        writes = len(self.puts(api))
        for run_id, attempt in (("43", 1), ("42", 2)):
            with self.subTest(run_id=run_id, attempt=attempt):
                with self.assertRaises(SchedulerError):
                    self.scheduler(api, run_id, attempt).finalize("success")
        self.assertEqual(len(self.puts(api)), writes)

    def test_final_result_write_failure_keeps_claim_to_prevent_repetition(self):
        api = MockAPI(state_at())
        scheduler = self.scheduler(api)
        scheduler.prepare("poll")
        api.fail_write = True
        with self.assertRaises(GitHubAPIError):
            scheduler.finalize("success")
        self.assertEqual(api.state["state"], "sending")
        api.fail_write = False
        self.scheduler(api, "43").prepare("poll")
        self.assertEqual(api.state["state"], "paused")
        self.assertEqual(api.events.count("send_due:true"), 1)

    def test_history_is_bounded_and_contains_only_send_metadata(self):
        state = state_at()
        state["history"] = [
            {"run_id": str(number), "run_attempt": 1,
             "occurred_at": format_time(NOW), "status": "accepted"}
            for number in range(100)
        ]
        api = MockAPI(state)
        scheduler = self.scheduler(api)
        scheduler.prepare("poll")
        scheduler.finalize("success")
        self.assertEqual(len(api.state["history"]), 100)
        self.assertEqual(api.state["history"][0]["run_id"], "1")
        required = {"run_id", "run_attempt", "occurred_at", "status"}
        self.assertTrue(all(set(item) in (required, required | {"time_source"})
                            for item in api.state["history"]))

    def test_saved_accepted_time_is_the_smtp_time_not_finalize_time(self):
        api = MockAPI(state_at())
        scheduler = self.scheduler(api)
        scheduler.prepare("poll")
        actual_acceptance = NOW - timedelta(seconds=12, microseconds=345678)
        state = scheduler.finalize("success", format_time(actual_acceptance))
        self.assertEqual(parse_time(state["last_send"]["occurred_at"]), actual_acceptance)
        self.assertEqual(state["last_send"]["time_source"], "smtp_accepted")

    def test_next_time_preserves_fractional_second_interval(self):
        api = MockAPI()
        now = NOW.replace(microsecond=999999)
        scheduler = Scheduler(api, '42', now=lambda: now, choose_delay=lambda lower, upper: 1)
        target = parse_time(scheduler.next_time(1, 1))
        self.assertEqual(target - now, timedelta(minutes=1))

    def test_format_time_rejects_missing_timezone(self):
        with self.assertRaises(SchedulerError):
            format_time(NOW.replace(tzinfo=None))

    def test_disabled_plan_never_sends(self):
        state = state_at()
        state.update(enabled=False, state="disabled", next_send_at=None)
        api = MockAPI(state)
        self.scheduler(api).prepare("poll")
        self.assertEqual(api.events, ["send_due:false", "disable"])

    def test_corrupt_state_does_not_send(self):
        state = state_at()
        state["next_send_at"] = "not-a-time"
        api = MockAPI(state)
        with self.assertRaises(SchedulerError):
            self.scheduler(api).prepare("poll")
        self.assertNotIn("send_due:true", api.events)

    def test_unexpected_history_fields_are_rejected_before_sending(self):
        state = state_at()
        state["history"] = [{"run_id": "1", "run_attempt": 1,
                             "occurred_at": format_time(NOW), "status": "accepted",
                             "QQ_SMTP_AUTH_CODE": "private-value"}]
        api = MockAPI(state)
        with self.assertRaises(SchedulerError) as result:
            self.scheduler(api).prepare("poll")
        self.assertNotIn("private-value", str(result.exception))
        self.assertNotIn("send_due:true", api.events)


if __name__ == "__main__":
    unittest.main()
