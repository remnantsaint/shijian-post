import base64
import contextlib
import copy
import io
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from scripts.control_bot import ControlError, Controller, image_path, main
from scripts.github_api import GitHubAPIError
from scripts.random_scheduler import MAX_MINUTES, STATE_PATH, SchedulerError


def plan_state(status="scheduled", enabled=True):
    return {
        "version": 1, "enabled": enabled, "mode": "once",
        "min_minutes": 1, "max_minutes": MAX_MINUTES,
        "next_send_at": "2026-10-14T01:00:00Z" if status in ("scheduled", "sending") else None,
        "state": status, "claim_run_id": "42" if status == "sending" else None,
        "claim_run_attempt": 1 if status == "sending" else None,
        "last_send": None, "history": [],
    }


class FakeAPI:
    repo = "owner/mail-bot"

    def __init__(self, state=None, branch="develop"):
        self.calls = []
        self.state = copy.deepcopy(state)
        self.branch = branch
        self.run = {
            "id": 42, "workflow_id": 101, "status": "in_progress",
            "repository": {"full_name": self.repo},
        }
        self.fail_dispatch = False
        self.variable = None
        self.workflow_state = "disabled_manually"
        self.state_sha = "state-head" if state is not None else None
        self.fail_write = False
        self.fail_disable = False
        self.letter_workflow = GitHubAPIError("Not found", status=404)

    def request(self, method, path, data=None):
        self.calls.append((method, path, copy.deepcopy(data)))
        if path.endswith("/dispatches"):
            if self.fail_dispatch:
                raise GitHubAPIError("private-secret-response", status=403)
            return None
        if path.endswith("/enable"):
            self.workflow_state = "active"
            return None
        if path.endswith("/disable"):
            if self.fail_disable:
                raise GitHubAPIError("private-disable-response", status=403)
            self.workflow_state = "disabled_manually"
            return None
        if path.endswith("/cancel"):
            return None
        if method == "PUT" and path.endswith(f"/contents/{STATE_PATH}"):
            if self.fail_write or data.get("sha") != self.state_sha:
                raise GitHubAPIError("private-conflict-response", status=409)
            self.state = json.loads(base64.b64decode(data["content"]))
            self.state_sha = "new-state-head"
            return {"content": {"sha": self.state_sha}}
        if method != "GET":
            raise AssertionError("Unexpected mutation")
        if path == f"/repos/{self.repo}":
            return {"default_branch": self.branch}
        if path.endswith("/actions/variables/MAIL_BOT_POLL_MINUTES"):
            if self.variable is None:
                raise GitHubAPIError("Not found", status=404)
            return copy.deepcopy(self.variable)
        if f"/contents/{STATE_PATH}?" in path:
            if self.state is None:
                raise GitHubAPIError("Not found", status=404)
            return {"encoding": "base64", "sha": self.state_sha,
                    "content": base64.b64encode(json.dumps(self.state).encode()).decode()}
        if path.endswith("/git/ref/heads/mail-bot-state"):
            return {"object": {"sha": "branch-head"}}
        if path.endswith("/actions/runs/42"):
            return copy.deepcopy(self.run)
        if path.endswith("/workflows/main.yml"):
            return {"id": 101}
        if path.endswith("/workflows/random.yml"):
            return {"id": 102, "state": self.workflow_state}
        if path.endswith("/workflows/dragon-letter.yml"):
            if isinstance(self.letter_workflow, Exception):
                raise self.letter_workflow
            return copy.deepcopy(self.letter_workflow)
        raise AssertionError("Unexpected read")

    def mutations(self):
        return [call for call in self.calls if call[0] != "GET"]


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.output = []
        self.controller = Controller(self.api, self.output.append)

    def test_default_command_reads_status_without_mutations(self):
        def report(api, repo, check_interval):
            self.assertEqual(repo, self.api.repo)
            self.assertIsNone(check_interval)
            api.request("GET", f"/repos/{repo}/actions/workflows/main.yml")
            return "只读状态"

        with patch.dict("os.environ", {"GH_TOKEN": "private-token"}, clear=True), \
                patch("scripts.control_bot.GitHubAPI", return_value=self.api), \
                patch("scripts.control_bot.make_report", side_effect=report), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--repo", self.api.repo]), 0)
        self.assertIn("只读状态", output.getvalue())
        self.assertFalse(self.api.mutations())

    def test_status_uses_only_verified_polling_configuration(self):
        for value, expected in (("5", "5"), ("60", "60"), ("other", "60")):
            with self.subTest(value=value):
                self.api.variable = {"name": "MAIL_BOT_POLL_MINUTES", "value": value}
                with patch("scripts.control_bot.make_report", return_value="状态") as report:
                    self.controller.status()
                report.assert_called_once_with(self.api, self.api.repo, check_interval=expected)
        self.assertFalse(self.api.mutations())

    def test_status_does_not_guess_frequency_on_unavailable_configuration(self):
        for variable in (None, {}, {"name": "unrelated", "value": "5"}):
            with self.subTest(variable=variable):
                self.api.variable = variable
                with patch("scripts.control_bot.make_report", return_value="状态") as report:
                    self.controller.status()
                report.assert_called_once_with(self.api, self.api.repo, check_interval=None)
        self.assertFalse(self.api.mutations())

    def test_send_payload_maps_chinese_delay_and_current_default_branch(self):
        self.controller.send("random", "random_3min", "images/photo.jpg")
        self.assertEqual(self.api.mutations(), [(
            "POST", f"/repos/{self.api.repo}/actions/workflows/main.yml/dispatches",
            {"ref": "develop", "inputs": {"message_type": "random",
             "send_delay": "3 分钟内随机发送", "image_path": "images/photo.jpg"}},
        )])
        self.assertIn("不能据此确认", self.output[0])

    def test_send_defaults_to_random_and_immediate(self):
        self.controller.send()
        inputs = self.api.mutations()[0][2]["inputs"]
        self.assertEqual(inputs, {"message_type": "random", "send_delay": "立即发送", "image_path": ""})

    def test_custom_send_preserves_requested_chinese_text(self):
        self.controller.send("custom", subject="我在这里", body="我在这里")
        inputs = self.api.mutations()[0][2]["inputs"]
        self.assertEqual(inputs["custom_body"], "我在这里")
        self.assertEqual(inputs["custom_subject"], "我在这里")
        self.assertEqual(inputs["message_type"], "custom")

    def test_invalid_custom_message_does_not_call_api_or_echo_values(self):
        for subject, body in (("", ""), ("private-value\n", "消息"),
                              ("", "private-value\x00"), ("", "文" * 100001)):
            with self.subTest(subject=subject[:20]), self.assertRaises(ControlError) as caught:
                self.controller.send("custom", subject=subject, body=body)
            self.assertNotIn("private-value", str(caught.exception))
        with self.assertRaises(ControlError):
            self.controller.send("random", body="private-value")
        self.assertFalse(self.api.calls)

    def test_invalid_image_is_rejected_before_network_and_never_echoed(self):
        for value in ("/images/private-value", "images/../private-value", "images/./private-value",
                      "images//private-value", "images\\private-value", "images/private-value\n", "private-value"):
            with self.subTest(value=value), self.assertRaises(ControlError) as caught:
                self.controller.send(image=value)
            self.assertNotIn("private-value", str(caught.exception))
        self.assertFalse(self.api.calls)
        self.assertEqual(image_path("images/中文 图片.png"), "images/中文 图片.png")

    def test_plan_enables_then_dispatches_chinese_inputs(self):
        self.controller.plan("repeat", "2", "hours", "3", "days")
        self.assertEqual(self.api.mutations(), [
            ("PUT", f"/repos/{self.api.repo}/actions/workflows/random.yml/enable", None),
            ("POST", f"/repos/{self.api.repo}/actions/workflows/random.yml/dispatches", {
                "ref": "develop", "inputs": {"action": "循环随机发送",
                "min_interval": "2", "min_unit": "小时", "max_interval": "3", "max_unit": "天"},
            }),
        ])
        self.assertIn("确认计划已保存", self.output[0])

    def test_once_mode(self):
        self.controller.plan("once", 1, "minutes", 90, "days")
        self.assertEqual(self.api.mutations()[-1][2]["inputs"]["action"], "安排一次")

    def test_plan_does_not_resume_stopped_old_plan(self):
        for workflow_state in ("disabled_manually", "disabled_inactivity", "disabled_fork"):
            with self.subTest(workflow_state=workflow_state):
                api = FakeAPI(plan_state())
                api.workflow_state = workflow_state
                original = copy.deepcopy(api.state)
                with self.assertRaises(ControlError) as error:
                    Controller(api).plan("once", 1, "minutes", 2, "minutes")
                self.assertIn("先关闭旧计划", str(error.exception))
                self.assertEqual(api.state, original)
                self.assertEqual(api.workflow_state, workflow_state)
                self.assertFalse(api.mutations())

    def test_plan_rejects_unconfirmed_send_before_any_mutation(self):
        for workflow_state in ("active", "disabled_manually"):
            with self.subTest(workflow_state=workflow_state):
                api = FakeAPI(plan_state("sending"))
                api.workflow_state = workflow_state
                with self.assertRaises(ControlError):
                    Controller(api).plan("repeat", 1, "minutes", 2, "minutes")
                self.assertFalse(api.mutations())

    def test_failed_new_plan_dispatch_does_not_reenable_or_overwrite_active_old_plan(self):
        api = FakeAPI(plan_state())
        api.workflow_state = "active"
        api.fail_dispatch = True
        original = copy.deepcopy(api.state)
        with self.assertRaises(GitHubAPIError):
            Controller(api).plan("once", 1, "minutes", 2, "minutes")
        self.assertEqual(api.state, original)
        self.assertEqual(api.workflow_state, "active")
        self.assertEqual(len(api.mutations()), 1)
        self.assertTrue(api.mutations()[0][1].endswith("/dispatches"))

    def test_partial_plan_submission_cannot_resume_previously_closed_plan(self):
        api = FakeAPI(plan_state("disabled", False))
        api.fail_dispatch = True
        original = copy.deepcopy(api.state)
        with self.assertRaises(GitHubAPIError):
            Controller(api).plan("once", 1, "minutes", 2, "minutes")
        self.assertEqual(api.workflow_state, "active")
        self.assertEqual(api.state, original)
        self.assertFalse(api.state["enabled"])
        self.assertEqual(len(api.mutations()), 2)
        self.assertTrue(api.mutations()[0][1].endswith("/enable"))
        self.assertTrue(api.mutations()[1][1].endswith("/dispatches"))

    def test_all_bounds_are_validated_before_enable(self):
        cases = [(2, "days", 1, "days"), (1, "minutes", 91, "days"),
                 (0, "minutes", 1, "hours"), ("private-value", "minutes", 1, "hours"),
                 (1, "private-value", 1, "days")]
        for lower, lower_unit, upper, upper_unit in cases:
            with self.subTest(case=(lower, lower_unit, upper, upper_unit)), \
                    self.assertRaises(SchedulerError) as caught:
                self.controller.plan("once", lower, lower_unit, upper, upper_unit)
            self.assertNotIn("private-value", str(caught.exception))
        self.assertFalse(self.api.calls)

    def test_pause_disables_before_state_write_without_enabling_or_dispatching(self):
        for status, enabled in (("scheduled", True), ("sending", True), ("paused", False)):
            with self.subTest(status=status):
                self.api = FakeAPI(plan_state(status, enabled))
                self.api.workflow_state = "active"
                old_history = {"run_id": "10", "run_attempt": 1,
                               "occurred_at": "2026-10-12T01:00:00Z", "status": "accepted"}
                self.api.state["history"] = [old_history]
                original = copy.deepcopy(self.api.state)
                now = datetime(2026, 10, 13, 2, 30, tzinfo=timezone.utc)
                with patch("scripts.random_scheduler.utc_now", return_value=now):
                    Controller(self.api, self.output.append).pause()
                mutations = self.api.mutations()
                self.assertEqual(mutations[0], (
                    "PUT", f"/repos/{self.api.repo}/actions/workflows/random.yml/disable", None))
                self.assertEqual(mutations[1][:2], (
                    "PUT", f"/repos/{self.api.repo}/contents/{STATE_PATH}"))
                self.assertEqual(mutations[1][2]["sha"], "state-head")
                self.assertEqual(len(mutations), 2)
                self.assertFalse(self.api.state["enabled"])
                self.assertEqual(self.api.state["state"], "disabled")
                self.assertIsNone(self.api.state["next_send_at"])
                self.assertIsNone(self.api.state["claim_run_id"])
                self.assertEqual(self.api.state["history"][0], original["history"][0])
                if status == "sending":
                    self.assertEqual(self.api.state["last_send"], {
                        "run_id": "42", "run_attempt": 1,
                        "occurred_at": "2026-10-13T02:30:00Z", "status": "uncertain",
                        "time_source": "state_recorded",
                    })
                else:
                    self.assertEqual(self.api.state["history"], original["history"])
                self.assertIn("仍可能已被 SMTP 接受", self.output[-1])

    def test_pause_write_conflict_keeps_workflow_stopped_and_preserves_state(self):
        self.api = FakeAPI(plan_state("sending"))
        self.api.workflow_state = "active"
        self.api.fail_write = True
        original = copy.deepcopy(self.api.state)
        with self.assertRaises(ControlError) as error:
            Controller(self.api).pause()
        self.assertIn("并发更新", str(error.exception))
        self.assertNotIn("private-conflict-response", str(error.exception))
        self.assertEqual(self.api.workflow_state, "disabled_manually")
        self.assertEqual(self.api.state, original)
        self.assertEqual(len(self.api.mutations()), 2)
        self.assertTrue(self.api.mutations()[0][1].endswith("/disable"))
        self.assertTrue(self.api.mutations()[1][1].endswith(f"/contents/{STATE_PATH}"))

    def test_pause_does_not_write_state_if_workflow_disable_fails(self):
        self.api = FakeAPI(plan_state())
        self.api.workflow_state = "active"
        self.api.fail_disable = True
        original = copy.deepcopy(self.api.state)
        with self.assertRaises(GitHubAPIError):
            Controller(self.api).pause()
        self.assertEqual(self.api.workflow_state, "active")
        self.assertEqual(self.api.state, original)
        self.assertEqual(len(self.api.mutations()), 1)

    def test_pause_missing_disabled_or_completed_plan_does_not_dispatch(self):
        for state in (None, plan_state("disabled", False), plan_state("completed", False)):
            for workflow_state in ("active", "disabled_manually", "disabled_inactivity"):
                with self.subTest(state=state, workflow_state=workflow_state):
                    api = FakeAPI(state)
                    api.workflow_state = workflow_state
                    Controller(api, self.output.append).pause()
                    self.assertEqual(api.state, state)
                    expected = [
                        ("PUT", f"/repos/{api.repo}/actions/workflows/random.yml/disable", None),
                    ] if workflow_state == "active" else []
                    self.assertEqual(api.mutations(), expected)

    def test_pause_already_disabled_workflow_closes_state_without_management_write(self):
        for workflow_state in ("disabled_manually", "disabled_inactivity"):
            with self.subTest(workflow_state=workflow_state):
                api = FakeAPI(plan_state())
                api.workflow_state = workflow_state
                api.fail_disable = True  # A redundant management write would fail.
                original = copy.deepcopy(api.state)
                Controller(api, self.output.append).pause()
                self.assertEqual(len(api.mutations()), 1)
                self.assertEqual(api.mutations()[0][:2], (
                    "PUT", f"/repos/{api.repo}/contents/{STATE_PATH}"))
                self.assertEqual(api.mutations()[0][2]["sha"], "state-head")
                self.assertFalse(api.state["enabled"])
                self.assertEqual(api.state["state"], "disabled")
                self.assertIsNone(api.state["next_send_at"])
                self.assertEqual(api.state["history"], original["history"])
                self.assertEqual(api.workflow_state, workflow_state)

    def test_pause_unknown_workflow_state_has_no_writes(self):
        for workflow_state in (None, "", "unknown", "deleted", "disabled_fork"):
            with self.subTest(workflow_state=workflow_state):
                api = FakeAPI(plan_state())
                api.workflow_state = workflow_state
                original = copy.deepcopy(api.state)
                with self.assertRaises(ControlError) as error:
                    Controller(api).pause()
                self.assertIn("未修改计划状态", str(error.exception))
                self.assertEqual(api.state, original)
                self.assertFalse(api.mutations())

    def test_pause_corrupt_state_does_not_disable_or_overwrite(self):
        self.api.state = {"history": "private-value"}
        with self.assertRaises(SchedulerError):
            self.controller.pause()
        self.assertFalse(self.api.mutations())

    def test_cancel_active_mail_run_only(self):
        for workflow_id in (101, 102):
            for status in ("queued", "in_progress", "waiting", "requested", "pending"):
                with self.subTest(workflow_id=workflow_id, status=status):
                    api = FakeAPI()
                    api.run.update(workflow_id=workflow_id, status=status)
                    output = []
                    Controller(api, output.append).cancel_run("42")
                    self.assertEqual(api.mutations(), [
                        ("POST", f"/repos/{api.repo}/actions/runs/42/cancel", None),
                    ])
                    self.assertIn("仍可能已被 SMTP 接受", output[0])

    def test_cancel_letter_run_only_when_optional_workflow_identity_matches(self):
        self.api.letter_workflow = {"id": 104, "state": "active"}
        self.api.run["workflow_id"] = 104
        self.controller.cancel_run(42)
        self.assertEqual(self.api.mutations(), [
            ("POST", f"/repos/{self.api.repo}/actions/runs/42/cancel", None),
        ])
        self.assertIn("尚未确认运行已停止", self.output[0])

    def test_optional_letter_404_still_allows_existing_mail_workflows(self):
        self.controller.cancel_run(42)
        self.assertIn(("GET", f"/repos/{self.api.repo}/actions/workflows/dragon-letter.yml", None),
                      self.api.calls)
        self.assertEqual(len(self.api.mutations()), 1)

    def test_optional_letter_errors_or_invalid_identity_do_not_cancel_any_run(self):
        for response, error in ((GitHubAPIError("private-letter-response", status=403), GitHubAPIError),
                                (None, ControlError), ({}, ControlError),
                                ({"id": True}, ControlError), ({"id": "private-value"}, ControlError)):
            with self.subTest(response=response):
                api = FakeAPI()
                api.letter_workflow = response
                with self.assertRaises(error):
                    Controller(api).cancel_run(42)
                self.assertFalse(api.mutations())

    def test_cancel_does_not_affect_readonly_ci_or_another_repository(self):
        cases = ({"workflow_id": 103}, {"repository": {"full_name": "other/repo"}},
                 {"status": "unknown"}, {"workflow_id": True}, {"id": 43})
        for change in cases:
            with self.subTest(change=change):
                api = FakeAPI()
                api.run.update(change)
                with self.assertRaises(ControlError):
                    Controller(api, self.output.append).cancel_run(42)
                self.assertFalse(api.mutations())

    def test_cancel_completed_mail_run_does_not_mutate_or_rerun(self):
        self.api.run["status"] = "completed"
        self.controller.cancel_run(42)
        self.assertFalse(self.api.mutations())
        self.assertIn("没有提交取消或重跑", self.output[0])

    def test_cancel_invalid_run_id_has_no_network_calls(self):
        for run_id in (0, -1, True, "private-value", "42\n"):
            with self.subTest(run_id=run_id), self.assertRaises(ControlError):
                self.controller.cancel_run(run_id)
        self.assertFalse(self.api.calls)

    def test_failed_dispatch_is_not_retried_and_error_does_not_echo_response(self):
        self.api.fail_dispatch = True
        with patch.dict("os.environ", {"GH_TOKEN": "private-token"}, clear=True), \
                patch("scripts.control_bot.GitHubAPI", return_value=self.api), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(["--repo", self.api.repo, "send"]), 1)
        self.assertEqual(len(self.api.mutations()), 1)
        combined = output.getvalue() + error.getvalue()
        self.assertNotIn("private-token", combined)
        self.assertNotIn("private-secret-response", combined)
        self.assertIn("403", combined)
        self.assertIn("未自动重试", combined)

    def test_missing_credentials_do_not_make_api_calls(self):
        with patch.dict("os.environ", {}, clear=True), \
                patch("scripts.control_bot.GitHubAPI") as api_constructor, \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(["--repo", self.api.repo, "status"]), 1)
        api_constructor.assert_not_called()
        self.assertIn("安全绑定", error.getvalue())

    def test_existing_github_token_fallback_is_reused(self):
        with patch.dict("os.environ", {"GITHUB_TOKEN": "private-token"}, clear=True), \
                patch("scripts.control_bot.GitHubAPI", return_value=self.api) as api_constructor, \
                patch("scripts.control_bot.make_report", return_value="状态"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--repo", self.api.repo]), 0)
        api_constructor.assert_called_once_with(self.api.repo, "private-token")

    def test_no_implicit_original_author_repository(self):
        with patch.dict("os.environ", {"GH_TOKEN": "private-token"}, clear=True), \
                patch("scripts.control_bot.GitHubAPI") as api_constructor, \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(["send"]), 1)
        api_constructor.assert_not_called()
        self.assertIn("--repo", error.getvalue())

    def test_actions_default_repository_is_own_repository(self):
        with patch.dict("os.environ", {"GH_TOKEN": "private-token",
                                        "GITHUB_REPOSITORY": self.api.repo}, clear=True), \
                patch("scripts.control_bot.GitHubAPI", return_value=self.api) as api_constructor, \
                patch("scripts.control_bot.make_report", return_value="状态"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["status"]), 0)
        api_constructor.assert_called_once_with(self.api.repo, "private-token")

    def test_unified_send_uses_chinese_action_and_preserves_requested_text(self):
        original = self.api.request
        def request(method, path, data=None):
            result = original(method, path, data)
            if path.endswith('/workflows/random.yml'):
                result['name'] = '邮件机器人（统一入口）'
            return result
        with patch.object(self.api, 'request', side_effect=request):
            self.controller.send('custom', subject='标题', body='我在这里')
        mutations = self.api.mutations()
        self.assertEqual(len(mutations), 1)
        self.assertTrue(mutations[0][1].endswith('/workflows/random.yml/dispatches'))
        self.assertEqual(mutations[0][2]['inputs']['action'], '发送自定义邮件')
        self.assertEqual(mutations[0][2]['inputs']['custom_body'], '我在这里')
        self.assertNotIn('message_type', mutations[0][2]['inputs'])

    def test_unified_pause_preserves_manual_entry_and_history(self):
        api = FakeAPI(plan_state())
        api.workflow_state = 'active'
        original = api.request
        def request(method, path, data=None):
            result = original(method, path, data)
            if path.endswith('/workflows/random.yml'):
                result['name'] = '邮件机器人（统一入口）'
            return result
        with patch.object(api, 'request', side_effect=request):
            Controller(api, self.output.append).pause()
        self.assertFalse(api.state['enabled'])
        self.assertEqual(api.workflow_state, 'active')
        self.assertEqual(len(api.mutations()), 1)
        self.assertNotIn('/disable', api.mutations()[0][1])

    def test_unified_code_checks_cannot_be_cancelled_as_mail(self):
        self.api.run.update(workflow_id=102, event='push', display_title='代码检查')
        original = self.api.request
        def request(method, path, data=None):
            result = original(method, path, data)
            if path.endswith('/workflows/random.yml'):
                result['name'] = '邮件机器人（统一入口）'
            return result
        with patch.object(self.api, 'request', side_effect=request), self.assertRaises(ControlError):
            self.controller.cancel_run(42)
        self.assertFalse(self.api.mutations())

    def test_argparse_errors_never_echo_invalid_arguments(self):
        cases = [["send", "--type", "private-secret"], ["private-secret"],
                 ["send", "--private-secret"], ["plan", "--mode", "private-secret"],
                 ["send", "--delay", "private-secret"]]
        for arguments in cases:
            with self.subTest(arguments=arguments), \
                    patch("scripts.control_bot.GitHubAPI") as api_constructor, \
                    contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(main(arguments), 1)
            api_constructor.assert_not_called()
            self.assertNotIn("private-secret", error.getvalue())


if __name__ == "__main__":
    unittest.main()
