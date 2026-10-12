import base64
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import status_history as status
from scripts.github_api import GitHubAPIError


REPO = "example/mail-bot"
BASE = f"/repos/{REPO}"
NOW = datetime(2026, 10, 12, 5, 0, tzinfo=timezone.utc)


class APIError(GitHubAPIError):
    def __init__(self, code):
        super().__init__("fixed-safe-error", status=code)


class FakeAPI:
    def __init__(self, runs=()):
        self.calls = []
        self.responses = {
            f"{BASE}/actions/workflows/main.yml": {"state": "active"},
            f"{BASE}/actions/workflows/main.yml/runs?per_page=20": {"workflow_runs": list(runs)},
            f"{BASE}/actions/workflows/random.yml": APIError(404),
            f"{BASE}/contents/.mail-bot/random.json?ref=mail-bot-state": APIError(404),
        }

    def request(self, method, path, data=None):
        self.calls.append((method, path))
        value = self.responses.get(path, APIError(404))
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def jobs(self, run_id, steps, attempt=1, completed_at=None):
        self.responses[f"{BASE}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"] = {
            "jobs": [{"steps": steps, "completed_at": completed_at}]}

    def random_state(self, state):
        self.responses[f"{BASE}/actions/workflows/random.yml"] = {"state": "active"}
        data = base64.b64encode(json.dumps(state).encode()).decode()
        self.responses[f"{BASE}/contents/.mail-bot/random.json?ref=mail-bot-state"] = {
            "encoding": "base64", "content": data}


def run(run_id, attempt=1, state="completed", conclusion="success"):
    return {"id": run_id, "run_attempt": attempt, "status": state,
            "conclusion": conclusion, "event": "workflow_dispatch",
            "updated_at": "2026-10-12T04:00:00Z", "created_at": "2026-10-12T03:00:00Z",
            "html_url": "https://malicious.invalid/private-token"}


def plan(history=()):
    return {"version": 1, "enabled": True, "mode": "repeat", "state": "scheduled",
            "min_minutes": 10, "max_minutes": 30, "next_send_at": "2026-10-12T04:50:00Z",
            "history": list(history)}


class StatusTests(unittest.TestCase):
    def test_accepted_step_survives_overall_failure_and_uses_latest_attempt(self):
        api = FakeAPI([run(11, attempt=2, conclusion="failure")])
        api.jobs(11, [{"name": status.SEND_STEP, "status": "completed", "conclusion": "success"}], 2)
        report = status.make_report(api, REPO, now=NOW)
        self.assertIn("SMTP 已接受", report)
        self.assertIn("/actions/runs/11/attempts/2", report)
        self.assertIn(("GET", f"{BASE}/actions/runs/11/attempts/2/jobs?per_page=100"), api.calls)
        self.assertNotIn("malicious.invalid", report)
        self.assertNotIn("private-token", report)

    def test_old_environment_run_is_not_sent(self):
        api = FakeAPI([run(10)])
        api.jobs(10, [{"name": "Test", "status": "completed", "conclusion": "success"}])
        report = status.make_report(api, REPO, now=NOW)
        self.assertIn("旧版环境测试（无发送步骤）", report)
        self.assertNotIn("| SMTP 已接受 |", report)

    def test_queued_run_is_reported_without_querying_steps(self):
        api = FakeAPI([run(12, state="queued", conclusion=None)])
        report = status.make_report(api, REPO, now=NOW)
        self.assertIn("排队或等待启动", report)
        self.assertFalse(any("/jobs?" in path for _, path in api.calls))

    def test_each_rerun_attempt_has_its_own_outcome_and_completion_time(self):
        api = FakeAPI([run(16, attempt=3, conclusion="cancelled")])
        outcomes = [(3, "cancelled", "2026-10-12T04:03:00Z"),
                    (2, "success", "2026-10-12T04:02:00Z"),
                    (1, "failure", "2026-10-12T04:01:00Z")]
        for attempt, outcome, ended in outcomes:
            api.jobs(16, [{"name": status.SEND_STEP, "status": "completed",
                           "conclusion": outcome, "completed_at": ended}], attempt)
        _, entries = status.read_main_runs(api, REPO)
        self.assertEqual([entry["run_attempt"] for entry in entries], [3, 2, 1])
        self.assertEqual(entries[0]["result"], "发送被取消，结果不确定")
        self.assertEqual(entries[1]["result"], "SMTP 已接受")
        self.assertIn("发送失败", entries[2]["result"])
        self.assertEqual(entries[1]["date"], status.timestamp("2026-10-12T04:02:00Z"))
        report = status.make_report(api, REPO, now=NOW)
        self.assertIn("/runs/16/attempts/1", report)
        self.assertIn("/runs/16/attempts/2", report)
        self.assertIn("/runs/16/attempts/3", report)

    def test_queued_latest_attempt_still_reads_the_previous_finished_attempt(self):
        api = FakeAPI([run(17, attempt=2, state="queued", conclusion=None)])
        api.jobs(17, [{"name": status.SEND_STEP, "conclusion": "success",
                       "completed_at": "2026-10-12T03:59:00Z"}], 1)
        _, entries = status.read_main_runs(api, REPO)
        self.assertEqual(entries[0]["result"], "排队或等待启动")
        self.assertEqual(entries[1]["result"], "SMTP 已接受")
        self.assertIn(("GET", f"{BASE}/actions/runs/17/attempts/1/jobs?per_page=100"), api.calls)
        self.assertNotIn(("GET", f"{BASE}/actions/runs/17/attempts/2/jobs?per_page=100"), api.calls)

    def test_latest_status_uses_record_time_not_created_run_list_order(self):
        newer_created = run(19)
        older_rerun = run(18)
        older_rerun["created_at"] = "2026-10-11T04:00:00Z"
        older_rerun["updated_at"] = "2026-10-12T04:59:00Z"
        api = FakeAPI([newer_created, older_rerun])
        api.jobs(19, [{"name": status.SEND_STEP, "conclusion": "success",
                       "completed_at": "2026-10-12T04:10:00Z"}])
        api.jobs(18, [{"name": status.SEND_STEP, "conclusion": "failure",
                       "completed_at": "2026-10-12T04:50:00Z"}])
        report = status.make_report(api, REPO, now=NOW)
        latest = next(line for line in report.splitlines() if line.startswith("最近一次主工作流："))
        self.assertIn("发送失败", latest)
        self.assertIn("/runs/18/attempts/1", latest)

    def test_attempt_query_budget_is_bounded_to_twenty(self):
        api = FakeAPI([run(20, attempt=1000), run(21)])
        _, entries = status.read_main_runs(api, REPO)
        self.assertEqual(len(entries), 20)
        self.assertEqual(sum("/jobs?" in path for _, path in api.calls), 20)

    def test_record_time_falls_back_from_send_to_job_to_run_update(self):
        fallback = status.timestamp("2026-10-12T04:59:00Z")
        jobs = [{"completed_at": "2026-10-12T04:20:00Z", "steps": [
            {"name": status.SEND_STEP, "completed_at": "2026-10-12T04:10:00Z"}]}]
        self.assertEqual(status.record_time(jobs, fallback), status.timestamp("2026-10-12T04:10:00Z"))
        jobs[0]["steps"][0]["completed_at"] = None
        self.assertEqual(status.record_time(jobs, fallback), status.timestamp("2026-10-12T04:20:00Z"))
        jobs[0]["completed_at"] = None
        self.assertEqual(status.record_time(jobs, fallback), fallback)

    def test_running_wait_and_send_are_distinct(self):
        current = run(13, state="in_progress", conclusion=None)
        self.assertEqual(status.step_result(current, [{"steps": [
            {"name": status.WAIT_STEP, "status": "in_progress"},
            {"name": status.SEND_STEP, "status": "queued"}]}]), "正在倒计时等待")
        self.assertEqual(status.step_result(current, [{"steps": [
            {"name": status.SEND_STEP, "status": "in_progress"}]}]), "正在发送")

    def test_jobs_permission_error_is_unknown_not_unsent(self):
        api = FakeAPI([run(14)])
        api.responses[f"{BASE}/actions/runs/14/attempts/1/jobs?per_page=100"] = APIError(403)
        report = status.make_report(api, REPO, now=NOW)
        self.assertIn("状态未知（步骤查询失败）", report)
        self.assertNotIn("旧版环境测试", report)

    def test_404_means_no_random_plan_but_other_errors_fail(self):
        api = FakeAPI()
        self.assertIn("随机计时：尚未启用", status.make_report(api, REPO, now=NOW))
        for endpoint in (f"{BASE}/actions/workflows/random.yml",
                         f"{BASE}/contents/.mail-bot/random.json?ref=mail-bot-state"):
            with self.subTest(endpoint=endpoint):
                other = FakeAPI()
                other.responses[endpoint] = APIError(403)
                with self.assertRaises(GitHubAPIError):
                    status.make_report(other, REPO, now=NOW)

    def test_random_send_history_is_sorted_with_manual_runs_not_polling_runs(self):
        history = [{"run_id": str(100 + index), "run_attempt": 1,
                    "occurred_at": f"2026-10-12T04:{index:02}:00Z", "status": "accepted"}
                   for index in range(25)]
        api = FakeAPI([run(15)])
        api.jobs(15, [{"name": status.SEND_STEP, "conclusion": "success"}])
        api.random_state(plan(history))
        report = status.make_report(api, REPO, now=NOW, check_interval="5")
        rows = [line for line in report.splitlines() if line.startswith("| 2026")]
        self.assertEqual(len(rows), 20)
        self.assertIn("运行 #124", rows[0])
        self.assertNotIn("运行 #100", report)
        self.assertIn("计划时间已到", report)
        self.assertIn("检查间隔约 5 分钟", report)
        self.assertIn("循环", report)

    def test_invalid_random_state_fails_without_echoing_file_content(self):
        api = FakeAPI()
        invalid = plan()
        invalid["next_send_at"] = "private-mail@example.com"
        api.random_state(invalid)
        with self.assertRaises(status.ReportError) as error:
            status.make_report(api, REPO, now=NOW)
        self.assertNotIn("private-mail", str(error.exception))

    def test_markdown_escapes_html_links_and_table_separators(self):
        result = status.markdown("<script>[click](https://evil.invalid)|`secret`\nvalue")
        self.assertNotIn("<script>", result)
        self.assertNotIn("[click]", result)
        self.assertNotIn("|", result)
        self.assertNotIn("`", result)
        self.assertIn("&lt;script&gt;", result)
        self.assertIn("&#91;", result)

    def test_main_writes_summary_without_logging_token(self):
        api = FakeAPI()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.md"
            env = {"GITHUB_REPOSITORY": REPO, "GH_TOKEN": "private-token",
                   "GITHUB_STEP_SUMMARY": str(path)}
            output = io.StringIO()
            with patch.dict(os.environ, env, clear=True), patch.object(status, "GitHubAPI", return_value=api), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = status.main()
            self.assertEqual(result, 0)
            self.assertIn("邮件机器人状态", path.read_text())
            self.assertNotIn("private-token", output.getvalue() + path.read_text())

    def test_read_only_summary_write_failure_returns_failure(self):
        api = FakeAPI()
        with tempfile.TemporaryDirectory() as directory:
            env = {"GITHUB_REPOSITORY": REPO, "GH_TOKEN": "private-token",
                   "GITHUB_STEP_SUMMARY": str(Path(directory) / "missing" / "summary.md")}
            output = io.StringIO()
            with patch.dict(os.environ, env, clear=True), patch.object(status, "GitHubAPI", return_value=api), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                self.assertEqual(status.main(), 1)
            self.assertIn("无法写入", output.getvalue())
            self.assertNotIn("private-token", output.getvalue())


if __name__ == "__main__":
    unittest.main()
