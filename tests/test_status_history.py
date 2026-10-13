import base64
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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

    def letters(self, runs=()):
        self.responses[f"{BASE}/actions/workflows/dragon-letter.yml"] = {"state": "active"}
        self.responses[f"{BASE}/actions/workflows/dragon-letter.yml/runs?per_page=20"] = {
            "workflow_runs": list(runs)}


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
    def test_unified_history_excludes_status_checks_and_recognizes_mail_steps(self):
        api = FakeAPI()
        api.responses[f'{BASE}/actions/workflows/main.yml'] = APIError(404)
        api.responses[f'{BASE}/actions/workflows/random.yml'] = {
            'state': 'active', 'name': '邮件机器人（统一入口）'}
        mail = dict(run(70), display_title='发送随机来信')
        query = dict(run(71), display_title='查看状态')
        check = dict(run(72), display_title='代码检查', event='push')
        api.responses[f'{BASE}/actions/workflows/random.yml/runs?per_page=100&event=workflow_dispatch'] = {
            'workflow_runs': [mail, query, check]}
        api.jobs(70, [{'name': status.SEND_STEP, 'conclusion': 'success',
                       'completed_at': '2026-10-12T04:05:00Z'}])
        report = status.make_report(api, REPO, now=NOW)
        self.assertIn('SMTP 已接受', report)
        self.assertIn('运行 #70', report)
        self.assertNotIn('运行 #71', report)
        self.assertNotIn('运行 #72', report)
        self.assertNotIn('旧版环境测试', report)
    def test_letter_smtp_outcomes_use_real_steps_not_overall_result(self):
        for name in status.SEND_STEPS:
            for conclusion, expected in (("success", "SMTP 已接受"),
                                         ("failure", "发送失败或结果不确定；查看摘要"),
                                         ("cancelled", "发送被取消，结果不确定"),
                                         ("skipped", "未发送（发送步骤未执行）")):
                with self.subTest(name=name, conclusion=conclusion):
                    api = FakeAPI()
                    api.letters([run(50, conclusion="success" if conclusion != "success" else "failure")])
                    api.jobs(50, [{"name": name, "status": "completed",
                                   "conclusion": conclusion, "completed_at": "2026-10-12T04:05:00Z"}])
                    report = status.make_report(api, REPO, now=NOW)
                    latest = next(line for line in report.splitlines()
                                  if line.startswith("最近一次小龙娘来信："))
                    self.assertIn(expected, latest)
                    self.assertIn("/runs/50/attempts/1", latest)
                    row = next(line for line in report.splitlines() if line.startswith("|") and "运行 #50" in line)
                    self.assertIn("| 2026-10-12 12:05:00 | 发送步骤结束 |", row)
                    self.assertIn("| 小龙娘来信 |", row)

    def test_letter_running_and_queued_are_current_without_false_acceptance(self):
        for name in ("Send long personal letter through QQ SMTP", "Send personal letter via QQ SMTP"):
            for run_status, step_status, expected in (("in_progress", "in_progress", "正在发送"),
                                                     ("queued", "queued", "排队或等待启动")):
                with self.subTest(name=name, state=run_status):
                    api = FakeAPI()
                    api.letters([run(51, state=run_status, conclusion=None)])
                    api.jobs(51, [{"name": name, "status": step_status, "conclusion": None}])
                    report = status.make_report(api, REPO, now=NOW)
                    self.assertIn(f"最近一次小龙娘来信：{expected}", report)
                    self.assertNotIn("SMTP 已接受", report)
                    if run_status == "queued":
                        self.assertFalse(any("/runs/51/attempts/1/jobs?" in path for _, path in api.calls))

    def test_letter_old_attempt_keeps_its_own_time_or_unknown(self):
        api = FakeAPI()
        api.letters([run(52, attempt=3, state="queued", conclusion=None)])
        api.jobs(52, [{"name": "Send personal letter via QQ SMTP", "conclusion": "failure",
                       "completed_at": "2026-10-12T03:45:00Z"}], attempt=2)
        api.jobs(52, [{"name": "Send long personal letter through QQ SMTP",
                       "conclusion": "success"}], attempt=1)
        report = status.make_report(api, REPO, now=NOW)
        old_row = next(line for line in report.splitlines() if line.startswith("|") and "第 1 次" in line)
        self.assertIn("| 时间未知 | 未知 |", old_row)
        self.assertIn("SMTP 已接受", old_row)
        middle_row = next(line for line in report.splitlines() if line.startswith("|") and "第 2 次" in line)
        self.assertIn("2026-10-12 11:45:00", middle_row)
        self.assertIn("发送失败", middle_row)
        self.assertIn("最近一次小龙娘来信：排队或等待启动", report)

    def test_missing_optional_letter_workflow_is_compatible_but_forbidden_is_error(self):
        api = FakeAPI()
        report = status.make_report(api, REPO, now=NOW)
        self.assertIn("小龙娘来信工作流：尚未配置", report)
        self.assertFalse(any("dragon-letter.yml/runs" in path for _, path in api.calls))
        api.responses[f"{BASE}/actions/workflows/dragon-letter.yml"] = APIError(403)
        with self.assertRaises(GitHubAPIError):
            status.make_report(api, REPO, now=NOW)
        self.assertTrue(all(method == "GET" for method, _ in api.calls))

    def test_main_letter_and_persistent_random_history_are_merged_without_poll_runs(self):
        api = FakeAPI([run(53)])
        api.jobs(53, [{"name": status.SEND_STEP, "conclusion": "success",
                       "completed_at": "2026-10-12T04:10:00Z"}])
        api.letters([run(54)])
        api.jobs(54, [{"name": "Send personal letter via QQ SMTP", "conclusion": "success",
                       "completed_at": "2026-10-12T04:30:00Z"}])
        api.random_state(plan([{"run_id": "55", "run_attempt": 1,
                                "occurred_at": "2026-10-12T04:20:00Z", "status": "accepted"}]))
        report = status.make_report(api, REPO, now=NOW)
        rows = [line for line in report.splitlines() if line.startswith("| 2026")]
        self.assertEqual(len(rows), 3)
        self.assertIn("运行 #54", rows[0])
        self.assertIn("运行 #55", rows[1])
        self.assertIn("运行 #53", rows[2])
        self.assertFalse(any("random.yml/runs" in path for _, path in api.calls))
        self.assertTrue(all(method == "GET" for method, _ in api.calls))

    def test_enabled_plan_with_stopped_or_missing_workflow_does_not_await_poll(self):
        for workflow_state in ("disabled_manually", "disabled_inactivity", "deleted", "unknown", None):
            with self.subTest(workflow_state=workflow_state):
                api = FakeAPI()
                api.random_state(plan())
                api.responses[f"{BASE}/actions/workflows/random.yml"] = (
                    APIError(404) if workflow_state is None else {"state": workflow_state})
                report = status.make_report(api, REPO, now=NOW)
                self.assertIn("无法", report)
                self.assertIn("随机工作流未启用定时检查", report)
                self.assertNotIn("正在等待下次检查", report)
                self.assertNotIn("检查间隔约", report)
                self.assertNotIn("检查间隔未确认", report)
                self.assertTrue(all(method == "GET" for method, _ in api.calls))

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

    def test_time_sources_describe_the_actual_available_timestamp(self):
        fallback = status.timestamp("2026-10-12T04:59:00Z")
        jobs = [{"completed_at": "2026-10-12T04:20:00Z", "steps": [
            {"name": status.SEND_STEP, "completed_at": "2026-10-12T04:10:00Z"}]}]
        self.assertEqual(status.record_details(jobs, fallback)[1], "发送步骤结束")
        jobs[0]["steps"][0]["completed_at"] = None
        self.assertEqual(status.record_details(jobs, fallback)[1], "任务结束")
        jobs[0]["completed_at"] = None
        self.assertEqual(status.record_details(jobs, fallback), (fallback, "运行更新时间"))
        self.assertEqual(status.record_details(jobs), (None, "未知"))

    def test_old_attempt_without_its_own_completion_time_stays_unknown(self):
        for jobs in (None, [], [{"steps": [{"name": status.SEND_STEP,
                                             "completed_at": "invalid"}],
                              "completed_at": None}]):
            with self.subTest(jobs=jobs):
                api = FakeAPI([run(30, attempt=2)])
                api.jobs(30, [{"name": status.SEND_STEP, "conclusion": "success",
                               "completed_at": "2026-10-12T04:30:00Z"}], attempt=2)
                if jobs is not None:
                    api.responses[f"{BASE}/actions/runs/30/attempts/1/jobs?per_page=100"] = {
                        "jobs": jobs}
                _, entries = status.read_main_runs(api, REPO)
                previous = next(entry for entry in entries if entry["run_attempt"] == 1)
                self.assertIsNone(previous["date"])
                self.assertEqual(previous["time_source"], "未知")
                report = status.make_report(api, REPO, now=NOW)
                previous_row = next(line for line in report.splitlines()
                                    if line.startswith("|") and "第 1 次" in line)
                self.assertIn("| 时间未知 | 未知 |", previous_row)
                self.assertNotIn("2026-10-12", previous_row)

    def test_beijing_time_crosses_days_and_years(self):
        for value, expected in [
                ("2026-10-12T16:00:00Z", "2026-10-13 00:00:00"),
                ("2026-12-31T16:00:00Z", "2027-01-01 00:00:00"),
                ("2027-01-01T00:00:00+08:00", "2027-01-01 00:00:00")]:
            with self.subTest(value=value):
                self.assertEqual(status.beijing(status.timestamp(value)), expected)

    def test_naive_dates_are_rejected_without_assuming_runner_timezone(self):
        with self.assertRaises(status.ReportError):
            status.timestamp("2026-10-12T05:00:00")
        naive = datetime(2026, 10, 12, 5)
        with self.assertRaises(status.ReportError):
            status.beijing(naive)
        api = FakeAPI()
        with self.assertRaises(status.ReportError):
            status.make_report(api, REPO, now=naive)
        self.assertEqual(api.calls, [])

    def test_query_completion_uses_clock_after_api_reads(self):
        api = FakeAPI()
        started = NOW
        completed = NOW + timedelta(seconds=42)

        def clock():
            if not api.calls:
                return started
            return completed

        with patch.object(status, "utc_now", side_effect=clock) as mocked_clock:
            report = status.make_report(api, REPO)
        self.assertEqual(mocked_clock.call_count, 2)
        self.assertIn("查询开始：北京时间 2026-10-12 13:00:00", report)
        self.assertIn("查询完成：北京时间 2026-10-12 13:00:42", report)
        with patch.object(status, "utc_now") as mocked_clock:
            fixed_report = status.make_report(FakeAPI(), REPO, now=NOW)
        mocked_clock.assert_not_called()
        self.assertIn("查询完成：北京时间 2026-10-12 13:00:00", fixed_report)

    def test_past_target_reports_real_lateness_but_future_target_does_not(self):
        api = FakeAPI()
        due = plan()
        due["next_send_at"] = "2026-10-12T04:52:30Z"
        api.random_state(due)
        report = status.make_report(api, REPO, now=NOW)
        self.assertIn("已晚 7 分 30 秒", report)
        due["next_send_at"] = "2026-10-12T05:00:01Z"
        api.random_state(due)
        report = status.make_report(api, REPO, now=NOW)
        self.assertNotIn("已晚", report)
        self.assertNotIn("计划时间已到", report)

    def test_random_history_distinguishes_acceptance_from_state_recording(self):
        history = [
            {"run_id": str(40 + index), "run_attempt": 1,
             "occurred_at": "2026-10-12T04:30:00Z", "status": "accepted", **extra}
            for index, extra in enumerate(({}, {"time_source": "state_recorded"},
                                           {"time_source": "smtp_accepted"}))]
        api = FakeAPI()
        api.random_state(plan(history))
        _, _, entries = status.read_random_state(api, REPO)
        self.assertEqual([entry["time_source"] for entry in entries],
                         ["状态记录（旧记录）", "状态记录", "SMTP 接受"])
        report = status.make_report(api, REPO, now=NOW)
        self.assertIn("| 时间来源 |", report)
        self.assertIn("不代表实际收件时间", report)
        history[-1]["status"] = "uncertain"
        api.random_state(plan(history))
        with self.assertRaises(status.ReportError):
            status.read_random_state(api, REPO)

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

    def test_unknown_poll_frequency_is_not_reported_as_default(self):
        api = FakeAPI()
        api.random_state(plan())
        report = status.make_report(api, REPO, now=NOW, check_interval=None)
        self.assertIn("检查间隔未确认", report)
        self.assertNotIn("检查间隔约 60", report)

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
