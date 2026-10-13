"""Control mail workflows from an authenticated terminal; default to read-only status.

Reuse an existing GH_TOKEN or GITHUB_TOKEN binding. This command does not give
ChatGPT Tasks terminal access and does not create a ChatGPT scheduled task.
"""

import argparse
import os
import re
import sys

from scripts.github_api import GitHubAPI, GitHubAPIError
from scripts.random_scheduler import (
    SchedulerError, StateStore, closed_plan_state, interval_minutes, validate_bounds, UNIFIED_WORKFLOW_NAME,
)
from scripts.status_history import ReportError, make_report


SEND_WORKFLOW = "main.yml"
PLAN_WORKFLOW = "random.yml"
LETTER_WORKFLOW = "dragon-letter.yml"
UNITS = {"minutes": "分钟", "hours": "小时", "days": "天"}
DELAYS = {"immediate": "立即发送", "random_3min": "3 分钟内随机发送"}
MODES = {"once": "安排一次", "repeat": "循环随机发送"}
CANCELLABLE = {"queued", "in_progress", "waiting", "requested", "pending"}


class ControlError(ValueError):
    """A fixed diagnostic that never contains user inputs or credentials."""


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's normal errors echo unknown arguments and invalid values.
        raise ControlError("命令参数无效；请使用 --help 查看用法。")


def positive_run_id(value):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ControlError("运行编号必须是正整数。")
    return int(value)


def image_path(value):
    if not isinstance(value, str):
        raise ControlError("图片路径必须位于仓库 images/ 目录内。")
    if not value:
        return ""
    parts = value.split("/")
    if (not value.startswith("images/") or len(parts) < 2
            or any(part in ("", ".", "..") for part in parts)
            or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ControlError("图片路径必须是 images/ 内的相对路径，不能包含路径跳转或控制字符。")
    # The runner checks existence, format and size; never read local attachments.
    return value


class Controller:
    def __init__(self, api, output=print):
        self.api = api
        self.repo = api.repo
        self.prefix = f"/repos/{self.repo}"
        self.output = output

    def default_branch(self):
        repository = self.api.request("GET", self.prefix)
        branch = repository.get("default_branch") if isinstance(repository, dict) else None
        if (not isinstance(branch, str) or not branch
                or any(ord(char) < 32 or ord(char) == 127 for char in branch)):
            raise ControlError("无法确认仓库默认分支；未提交操作。")
        return branch

    def dispatch(self, workflow, branch, inputs):
        # Never retry: a failed response can follow an accepted dispatch.
        self.api.request("POST", f"{self.prefix}/actions/workflows/{workflow}/dispatches", {
            "ref": branch, "inputs": inputs,
        })

    def enable_plan_workflow(self):
        self.api.request("PUT", f"{self.prefix}/actions/workflows/{PLAN_WORKFLOW}/enable")

    def status(self):
        interval = None
        try:
            variable = self.api.request(
                "GET", f"{self.prefix}/actions/variables/MAIL_BOT_POLL_MINUTES")
            if (isinstance(variable, dict)
                    and variable.get("name") == "MAIL_BOT_POLL_MINUTES"
                    and isinstance(variable.get("value"), str)):
                interval = "5" if variable["value"] == "5" else "60"
        except GitHubAPIError:
            # A missing variable and insufficient permission can both return 404.
            # Never turn an unverified configuration into a timing guarantee.
            pass
        self.output(make_report(self.api, self.repo, check_interval=interval))

    def send(self, message_type="random", delay="immediate", image="", *, subject="", body=""):
        if message_type not in ("test", "random", "custom") or delay not in DELAYS:
            raise ControlError("邮件类型或发送时机无效；未提交发送。")
        if message_type == "custom":
            if (not isinstance(body, str) or not body.strip() or len(body) > 100_000
                    or "\x00" in body):
                raise ControlError("自定义正文必须非空且不超过 100000 字符，不能包含空字符；未提交发送。")
            if (not isinstance(subject, str) or len(subject) > 200
                    or any(ord(char) < 32 or ord(char) == 127 for char in subject)):
                raise ControlError("自定义主题必须是长度合适的单行文本；未提交发送。")
        elif subject or body:
            raise ControlError("自定义主题和正文仅用于 custom 邮件；未提交发送。")
        path = image_path(image)
        branch = self.default_branch()
        inputs = {
            "message_type": message_type,
            "send_delay": DELAYS[delay],
            "image_path": path,
        }
        if message_type == "custom":
            inputs.update(custom_subject=subject, custom_body=body)
        workflow = self.api.request("GET", f"{self.prefix}/actions/workflows/{PLAN_WORKFLOW}")
        if isinstance(workflow, dict) and workflow.get("name") == UNIFIED_WORKFLOW_NAME:
            inputs.pop("message_type")
            inputs["action"] = {"test": "发送测试邮件", "random": "发送随机来信",
                                "custom": "发送自定义邮件"}[message_type]
            self.dispatch(PLAN_WORKFLOW, branch, inputs)
        else:
            self.dispatch(SEND_WORKFLOW, branch, inputs)
        self.output("发送请求已提交；工作流尚未确认完成，不能据此确认 SMTP 接受或邮件投递。请查看 status 和收件箱；未自动重试。")

    def plan(self, mode, min_interval, min_unit, max_interval, max_unit):
        if mode not in MODES:
            raise ControlError("计划模式无效；未提交计划。")
        lower = interval_minutes(min_interval, min_unit)
        upper = interval_minutes(max_interval, max_unit)
        validate_bounds(lower, upper)
        # Validate every input before enabling or dispatching any workflow.
        state = StateStore(self.api).load()
        workflow = self.api.request("GET", f"{self.prefix}/actions/workflows/{PLAN_WORKFLOW}")
        workflow_state = workflow.get("state") if isinstance(workflow, dict) else None
        if not isinstance(workflow_state, str) or not workflow_state:
            raise ControlError("无法确认随机计划工作流状态；未启用或提交计划。")
        if state is not None and state["state"] == "sending":
            raise ControlError("上次发送结果尚未确认；请先核对历史和收件箱并关闭旧计划，未启用或提交新计划。")
        if state is not None and state["enabled"] and workflow_state != "active":
            raise ControlError("旧计划仍标记启用，但工作流已停用；请先关闭旧计划，防止恢复旧目标时间。未启用或提交新计划。")
        branch = self.default_branch()
        if workflow_state != "active":
            self.enable_plan_workflow()
        self.dispatch(PLAN_WORKFLOW, branch, {
            "action": MODES[mode],
            "min_interval": str(min_interval), "min_unit": UNITS[min_unit],
            "max_interval": str(max_interval), "max_unit": UNITS[max_unit],
        })
        self.output("随机计划请求已提交；请查看 status 确认计划已保存和下次目标时间。本次提交不等于发送，GitHub 定时检查可能延迟；未自动重试。")

    def pause(self):
        store = StateStore(self.api)
        state = store.load()
        actual_plan = state is not None and (
            state["enabled"] or state["state"] in {"scheduled", "sending", "paused"}
        )
        # Stop future polls before changing state; never enable a stale plan merely
        # to dispatch its closure. The SHA guards concurrent claims/finalization.
        workflow = self.api.request("GET", f"{self.prefix}/actions/workflows/{PLAN_WORKFLOW}")
        workflow_state = workflow.get("state") if isinstance(workflow, dict) else None
        unified = isinstance(workflow, dict) and workflow.get("name") == UNIFIED_WORKFLOW_NAME
        if unified:
            if workflow_state not in {"active", "disabled_manually", "disabled_inactivity"}:
                raise ControlError("无法确认统一入口状态；未修改计划，请查看 status。")
            if actual_plan:
                try:
                    store.save(closed_plan_state(state), "Disable random mail plan from assistant")
                except GitHubAPIError:
                    raise ControlError("计划关闭状态未获确认；未停用统一入口或自动重试，请查看 status 和收件箱。") from None
            self.output("随机计划已关闭，历史保留；统一入口仍可手动发送和查询。已开始的邮件仍可能发出，请核对收件箱。")
            return
        if workflow_state == "active":
            self.api.request("PUT", f"{self.prefix}/actions/workflows/{PLAN_WORKFLOW}/disable")
        elif workflow_state not in {"disabled_manually", "disabled_inactivity"}:
            raise ControlError("无法确认随机工作流已停用；未修改计划状态，请核验工作流状态和权限。")
        if actual_plan:
            try:
                store.save(closed_plan_state(state), "Disable random mail plan from assistant")
            except GitHubAPIError as exc:
                if exc.status == 409:
                    raise ControlError("随机工作流已停用，但计划被并发更新，关闭状态未保存；未覆盖新状态或自动重试，请查看 status 和收件箱。已开始的邮件仍可能成功。") from None
                raise ControlError("随机工作流已停用，但持久化关闭状态未获确认；未自动重试，请检查写入权限、网络、status 和收件箱。已开始的邮件仍可能成功。") from None
            self.output("随机计划已关闭并保存，工作流已停用，历史保留。没有提交发送或重试；正在执行的邮件仍可能已被 SMTP 接受，请核对状态和收件箱。")
        else:
            self.output("随机计划工作流已停用；没有提交发送请求，现有持久化历史保留。已开始的运行仍需单独核查。")

    def cancel_run(self, run_id):
        run_id = positive_run_id(run_id)
        run = self.api.request("GET", f"{self.prefix}/actions/runs/{run_id}")
        if not isinstance(run, dict) or run.get("id") != run_id:
            raise ControlError("无法确认运行身份；未提交取消。")
        repository = run.get("repository")
        if (not isinstance(repository, dict)
                or not isinstance(repository.get("full_name"), str)
                or repository["full_name"].casefold() != self.repo.casefold()):
            raise ControlError("运行不属于当前仓库；未提交取消。")
        allowed = set()
        unified_id = None
        for workflow in (SEND_WORKFLOW, PLAN_WORKFLOW):
            try:
                data = self.api.request("GET", f"{self.prefix}/actions/workflows/{workflow}")
            except GitHubAPIError as exc:
                if workflow == SEND_WORKFLOW and exc.status == 404:
                    continue
                raise
            if not isinstance(data, dict):
                raise ControlError("无法确认邮件工作流身份；未提交取消。")
            allowed.add(positive_run_id(data.get("id")))
            if data.get("name") == UNIFIED_WORKFLOW_NAME:
                unified_id = positive_run_id(data.get("id"))
        try:
            letter = self.api.request(
                "GET", f"{self.prefix}/actions/workflows/{LETTER_WORKFLOW}")
        except GitHubAPIError as exc:
            if exc.status != 404:
                raise
        else:
            if not isinstance(letter, dict):
                raise ControlError("无法确认来信工作流身份；未提交取消。")
            allowed.add(positive_run_id(letter.get("id")))
        if type(run.get("workflow_id")) is not int or run["workflow_id"] not in allowed:
            raise ControlError("仅允许取消邮件发送、来信或随机计划工作流；未提交取消。")
        if run["workflow_id"] == unified_id and run.get("event") != "schedule":
            if run.get("display_title") not in {"发送测试邮件", "发送随机来信", "发送小龙娘来信",
                                               "发送自定义邮件", "发送漫画邮件", "安排一次", "循环随机发送", "关闭计划"}:
                raise ControlError("统一入口的代码检查或只读查询不属于邮件发送；未提交取消。")
        if run.get("status") == "completed":
            self.output("该运行已结束，没有提交取消或重跑。邮件仍可能已被 SMTP 接受，请查看历史和收件箱。")
            return
        if run.get("status") not in CANCELLABLE:
            raise ControlError("运行状态无法确认；未提交取消。")
        self.api.request("POST", f"{self.prefix}/actions/runs/{run_id}/cancel")
        self.output("取消请求已提交；尚未确认运行已停止，没有重跑。邮件仍可能已被 SMTP 接受，请查看 status 和收件箱。")


def build_parser():
    parser = SafeArgumentParser(
        prog="python -m scripts.control_bot",
        description="通过 GitHub Actions 查询和控制邮件机器人，默认仅查看状态。",
        epilog="认证使用已有 GH_TOKEN 或 GITHUB_TOKEN 安全绑定；不要把令牌作为命令参数。这不是 ChatGPT Tasks 连接器。",
    )
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"),
                        help="目标仓库 owner/name；Actions 内默认当前仓库，本地必须指定")
    parser.set_defaults(command="status")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("status", help="只读查询状态和历史")
    send = commands.add_parser("send", help="提交一次发送请求")
    send.add_argument("--type", dest="message_type", choices=("test", "random", "custom"), default="random")
    send.add_argument("--delay", choices=tuple(DELAYS), default="immediate")
    send.add_argument("--image", default="", help="可选，仓库 images/ 内的图片路径")
    send.add_argument("--subject", default="", help="自定义主题，仅 custom 使用")
    send.add_argument("--body", default="", help="自定义正文，custom 必填")
    plan = commands.add_parser("plan", help="安排单次或循环随机计划")
    plan.add_argument("--mode", choices=tuple(MODES), required=True)
    plan.add_argument("--min", dest="min_interval", required=True)
    plan.add_argument("--min-unit", choices=tuple(UNITS), required=True)
    plan.add_argument("--max", dest="max_interval", required=True)
    plan.add_argument("--max-unit", choices=tuple(UNITS), required=True)
    commands.add_parser("pause", help="保留历史并关闭随机计划")
    cancel = commands.add_parser("cancel-run", help="取消指定邮件工作流运行，不重跑")
    cancel.add_argument("--run-id", required=True)
    return parser


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        if not args.repo:
            raise ControlError("请用 --repo 明确指定自己的仓库；不会默认操作原作者的仓库。")
        token = os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise ControlError("缺少 GitHub API 凭据；请在运行环境安全绑定 GH_TOKEN 或 GITHUB_TOKEN，不要发送到聊天或写入代码。")
        controller = Controller(GitHubAPI(args.repo, token))
        command = args.command or "status"
        if command == "status":
            controller.status()
        elif command == "send":
            controller.send(args.message_type, args.delay, args.image,
                            subject=args.subject, body=args.body)
        elif command == "plan":
            controller.plan(args.mode, args.min_interval, args.min_unit,
                            args.max_interval, args.max_unit)
        elif command == "pause":
            controller.pause()
        elif command == "cancel-run":
            controller.cancel_run(args.run_id)
    except (ControlError, SchedulerError, ReportError) as exc:
        print(f"控制操作未完成：{exc}", file=sys.stderr)
        return 1
    except GitHubAPIError as exc:
        status = exc.status if type(exc.status) is int and 100 <= exc.status <= 599 else "网络错误或响应无效"
        print(f"GitHub API 操作未获确认（HTTP {status}）；请检查权限、网络和 status，不能确认任务完成或邮件投递。为避免重复发送，未自动重试。", file=sys.stderr)
        return 1
    except Exception:
        print("控制操作发生未预期错误；未输出底层错误以保护凭据。请查看 status 和收件箱，未自动重试。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
