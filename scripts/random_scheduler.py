"""Persist a random mail plan separately from source; never retry uncertain mail."""

import argparse
import base64
import binascii
import copy
import json
import os
import random
import sys
from datetime import timedelta
from urllib.parse import quote

from scripts.github_api import GitHubAPI, GitHubAPIError
from scripts.time_utils import format_beijing, format_utc, parse_utc, utc_now


STATE_BRANCH = "mail-bot-state"
STATE_PATH = ".mail-bot/random.json"
WORKFLOW_FILE = "random.yml"
UNIFIED_WORKFLOW_NAME = "邮件机器人（统一入口）"
MAX_MINUTES = 90 * 24 * 60
HISTORY_LIMIT = 100


class SchedulerError(ValueError):
    """A safe, human-readable configuration or state error."""


def format_time(value):
    try:
        return format_utc(value)
    except (ValueError, OverflowError):
        raise SchedulerError("时间必须包含时区；未发送邮件。") from None


def parse_time(value):
    try:
        return parse_utc(value)
    except (TypeError, ValueError):
        raise SchedulerError("计划中的 UTC 时间无效；未发送邮件。") from None


def display_time(value):
    return format_beijing(parse_time(value)) + "（北京时间 UTC+8）"


def positive_integer(value):
    if isinstance(value, bool) or not str(value).isdigit():
        raise SchedulerError("随机时间上下限必须是正整数。")
    number = int(value)
    if number < 1:
        raise SchedulerError("随机时间上下限必须至少为 1 分钟。")
    return number


def validate_bounds(min_minutes, max_minutes):
    lower = positive_integer(min_minutes)
    upper = positive_integer(max_minutes)
    if lower > upper:
        raise SchedulerError("最短间隔不能大于最长间隔。")
    if upper > MAX_MINUTES:
        raise SchedulerError("最长间隔不能超过 90 天。")
    return lower, upper


def interval_minutes(value, unit):
    units = {"minutes": 1, "hours": 60, "days": 24 * 60}
    if unit not in units:
        raise SchedulerError("时间单位仅支持 minutes、hours、days。")
    return positive_integer(value) * units[unit]


def requested_bounds(environment):
    if "MIN_MINUTES" in environment or "MAX_MINUTES" in environment:
        return validate_bounds(environment.get("MIN_MINUTES", "1"),
                               environment.get("MAX_MINUTES", str(MAX_MINUTES)))
    return validate_bounds(
        interval_minutes(environment.get("MIN_INTERVAL", "1"),
                         environment.get("MIN_UNIT", "minutes")),
        interval_minutes(environment.get("MAX_INTERVAL", "90"),
                         environment.get("MAX_UNIT", "days")),
    )


def validate_state(state):
    if (not isinstance(state, dict) or type(state.get("version")) is not int
            or state.get("version") != 1
            or type(state.get("enabled")) is not bool
            or state.get("mode") not in ("once", "repeat")
            or state.get("state") not in (
                "scheduled", "sending", "completed", "paused", "disabled")):
        raise SchedulerError("持久化计划格式无效；请检查状态分支，未发送邮件。")
    validate_bounds(state.get("min_minutes"), state.get("max_minutes"))
    if state.get("next_send_at") is not None:
        parse_time(state["next_send_at"])
    if state["state"] in ("scheduled", "sending") and not state.get("next_send_at"):
        raise SchedulerError("计划缺少下次发送时间；未发送邮件。")
    if state["state"] == "sending" and not state.get("claim_run_id"):
        raise SchedulerError("发送中的计划缺少运行编号；未发送邮件。")
    history = state.get("history", [])
    if not isinstance(history, list) or len(history) > HISTORY_LIMIT:
        raise SchedulerError("计划的历史记录格式无效；未发送邮件。")
    entries = history + ([state["last_send"]] if state.get("last_send") is not None else [])
    for item in entries:
        required_fields = {"run_id", "run_attempt", "occurred_at", "status"}
        if (not isinstance(item, dict)
                or set(item) not in (required_fields, required_fields | {"time_source"})
                or not isinstance(item["run_id"], str) or not item["run_id"].isdigit()
                or type(item["run_attempt"]) is not int or item["run_attempt"] < 1
                or item["status"] not in ("accepted", "uncertain")):
            raise SchedulerError("计划的发送历史格式无效；未发送邮件。")
        parse_time(item["occurred_at"])
        if item.get("time_source", "state_recorded") not in ("smtp_accepted", "state_recorded"):
            raise SchedulerError("计划的历史时间来源无效；未发送邮件。")
        if item.get("time_source") == "smtp_accepted" and item["status"] != "accepted":
            raise SchedulerError("计划的历史时间与发送结果不一致；未发送邮件。")
    return state


def closed_plan_state(state, *, now=None):
    """Return a closed copy without inventing a run or discarding history."""
    validate_state(state)
    closed = copy.deepcopy(state)
    if closed["state"] == "sending":
        item = {
            "run_id": closed["claim_run_id"],
            "run_attempt": closed.get("claim_run_attempt", 1),
            "occurred_at": format_time(utc_now() if now is None else now),
            "status": "uncertain", "time_source": "state_recorded",
        }
        closed["last_send"] = item
        closed["history"] = (closed.get("history", []) + [item])[-HISTORY_LIMIT:]
    closed.update(enabled=False, state="disabled", next_send_at=None,
                  claim_run_id=None, claim_run_attempt=None)
    return validate_state(closed)


class StateStore:
    """GitHub Contents writes use SHA to prevent a stale caller claiming mail."""

    def __init__(self, api):
        self.api = api
        self.prefix = f"/repos/{api.repo}"
        self.sha = None

    def load(self):
        try:
            result = self.api.request(
                "GET", f"{self.prefix}/contents/{STATE_PATH}?ref={STATE_BRANCH}")
        except GitHubAPIError as exc:
            if exc.status == 404:
                self.sha = None
                return None
            raise
        try:
            if not isinstance(result, dict) or result.get("encoding") != "base64":
                raise ValueError
            self.sha = result["sha"]
            content = base64.b64decode(result["content"], validate=False)
            state = json.loads(content.decode("utf-8"))
        except (KeyError, ValueError, TypeError, UnicodeError, binascii.Error):
            raise SchedulerError("状态文件无法解析；未发送邮件。") from None
        return validate_state(state)

    def ensure_branch(self):
        try:
            self.api.request("GET", f"{self.prefix}/git/ref/heads/{STATE_BRANCH}")
            return
        except GitHubAPIError as exc:
            if exc.status != 404:
                raise
        repository = self.api.request("GET", self.prefix)
        default_branch = quote(repository["default_branch"], safe="")
        reference = self.api.request("GET", f"{self.prefix}/git/ref/heads/{default_branch}")
        try:
            self.api.request("POST", f"{self.prefix}/git/refs", {
                "ref": f"refs/heads/{STATE_BRANCH}",
                "sha": reference["object"]["sha"],
            })
        except GitHubAPIError as exc:
            # A concurrent creator is harmless; verify the branch really exists.
            if exc.status != 422:
                raise
            self.api.request("GET", f"{self.prefix}/git/ref/heads/{STATE_BRANCH}")

    def save(self, state, message):
        validate_state(state)
        self.ensure_branch()
        content = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        data = {
            "message": message,
            "branch": STATE_BRANCH,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        if self.sha is not None:
            data["sha"] = self.sha
        result = self.api.request("PUT", f"{self.prefix}/contents/{STATE_PATH}", data)
        self.sha = result["content"]["sha"]


class Scheduler:
    def __init__(self, api, run_id, *, run_attempt=1, now=None, choose_delay=None, output=None):
        self.api = api
        self.store = StateStore(api)
        self.run_id = str(run_id)
        if not self.run_id.isdigit():
            raise SchedulerError("缺少有效的 GitHub Actions 运行编号。")
        self.run_attempt = positive_integer(run_attempt)
        self.now = now or utc_now
        self.choose_delay = choose_delay or random.SystemRandom().randint
        self.output = output or (lambda due: None)

    def disable_workflow(self):
        if os.environ.get("MAIL_BOT_UNIFIED") == "true":
            print("随机邮件计划已关闭，历史保留；统一入口仍可手动发送、查询与检查。")
            return
        self.api.request("PUT", f"/repos/{self.api.repo}/actions/workflows/{WORKFLOW_FILE}/disable")
        print("随机计划及后台检查已关闭，工作流已停用；历史保留。")

    def keep_audit_enabled(self):
        print("目前没有可执行的邮件计划；保留按配置频率运行的后台检查，不发送邮件。")

    def next_time(self, lower, upper):
        return format_time(self.now() + timedelta(minutes=self.choose_delay(lower, upper)))

    def record(self, state, status, accepted_at=None):
        occurred_at = parse_time(accepted_at) if accepted_at else self.now()
        item = {"run_id": state["claim_run_id"],
                "run_attempt": state.get("claim_run_attempt", 1),
                "occurred_at": format_time(occurred_at), "status": status,
                "time_source": "smtp_accepted" if accepted_at else "state_recorded"}
        state["last_send"] = item
        state["history"] = (state.get("history", []) + [item])[-HISTORY_LIMIT:]

    def prepare(self, action, *, min_minutes=1, max_minutes=MAX_MINUTES):
        state = self.store.load()
        if action in ("plan_once", "plan_repeat"):
            lower, upper = validate_bounds(min_minutes, max_minutes)
            if state is not None and state["state"] == "sending":
                raise SchedulerError("上一次发送结果尚未确认，不能覆盖。请先核对运行和收件箱，再停用旧计划。")
            if state is not None and state["state"] == "paused":
                print("旧计划已暂停；此操作只安排一次新的未来发送，不会重试旧邮件。")
            state = {
                "version": 1, "enabled": True,
                "mode": "once" if action == "plan_once" else "repeat",
                "min_minutes": lower, "max_minutes": upper,
                "next_send_at": self.next_time(lower, upper), "state": "scheduled",
                "claim_run_id": None, "claim_run_attempt": None,
                "last_send": state.get("last_send") if state else None,
                "history": state.get("history", []) if state else [],
            }
            self.store.save(state, "Schedule future random mail")
            print(f"随机计划已保存，下次目标时间：{display_time(state['next_send_at'])}。")
            print("时间由后续定时检查判断，不保证准点；本次不发送邮件。")
            self.output(False)
            return state
        if action == "status":
            if state is None:
                print("尚未配置随机计划。")
            else:
                target = display_time(state['next_send_at']) if state.get('next_send_at') else '无'
                print(f"计划状态：{state['state']}；下次目标时间：{target}。")
            self.output(False)
            return state
        if action == "disable":
            if state is not None:
                state = closed_plan_state(state, now=self.now())
                self.store.save(state, "Disable random mail plan")
            self.output(False)
            self.disable_workflow()
            return state
        if action != "poll":
            raise SchedulerError("随机计划操作无效；未发送邮件。")
        if state is None or not state["enabled"] or state["state"] in ("completed", "paused", "disabled"):
            self.output(False)
            self.keep_audit_enabled()
            return state
        if state["state"] == "sending":
            self.record(state, "uncertain")
            state.update(enabled=False, state="paused", next_send_at=None)
            self.store.save(state, "Pause random mail after uncertain result")
            print("上次运行未完成结果确认，已暂停；请核对收件箱与运行记录，不会自动重发。")
            self.output(False)
            self.keep_audit_enabled()
            return state
        if parse_time(state["next_send_at"]) > self.now():
            print(f"尚未到发送时间，下次目标：{display_time(state['next_send_at'])}。")
            self.output(False)
            return state
        state.update(state="sending", claim_run_id=self.run_id,
                     claim_run_attempt=self.run_attempt)
        # This durable claim MUST succeed before any send_due=true is exposed.
        self.store.save(state, "Claim due random mail before SMTP send")
        print("随机计划到期，已持久记录本次发送编号；开始一次发送。")
        self.output(True)
        return state

    def finalize(self, mail_outcome, accepted_at=None):
        state = self.store.load()
        if (state is None or state["state"] != "sending"
                or state.get("claim_run_id") != self.run_id
                or state.get("claim_run_attempt", 1) != self.run_attempt):
            raise SchedulerError("本次运行未持有发送记录，不能更新结果；未重复发送。")
        success = mail_outcome == "success"
        self.record(state, "accepted" if success else "uncertain", accepted_at if success else None)
        state["claim_run_id"] = None
        state["claim_run_attempt"] = None
        if success and state["mode"] == "repeat":
            state.update(state="scheduled", next_send_at=self.next_time(
                state["min_minutes"], state["max_minutes"]))
        else:
            state.update(enabled=False, state="completed" if success else "paused", next_send_at=None)
        self.store.save(state, "Record random mail send result")
        if state["enabled"]:
            print(f"本次 SMTP 步骤成功，下次目标时间：{display_time(state['next_send_at'])}。")
        else:
            if success:
                print("单次计划的 SMTP 步骤成功，计划已结束。")
            else:
                print("SMTP 步骤失败或结果不明，计划已暂停；请先核对收件箱，不会自动重试。")
            self.keep_audit_enabled()
        return state


def write_output(due):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        raise SchedulerError("缺少 GitHub Actions 输出文件；未发送邮件。")
    with open(path, "a", encoding="utf-8") as output:
        output.write(f"send_due={'true' if due else 'false'}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "finalize"))
    args = parser.parse_args(argv)
    try:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
        if not repo or not token:
            raise SchedulerError("缺少 GitHub 仓库身份或 Actions 内置令牌；未发送邮件。")
        scheduler = Scheduler(GitHubAPI(repo, token), os.environ.get("GITHUB_RUN_ID", ""),
                              run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
                              output=write_output)
        if args.command == "finalize":
            scheduler.finalize(os.environ.get("MAIL_SEND_OUTCOME", "unknown"), os.environ.get("SMTP_ACCEPTED_AT") or None)
        else:
            action = "poll" if os.environ.get("GITHUB_EVENT_NAME") == "schedule" else os.environ.get("ACTION", "status")
            lower, upper = requested_bounds(os.environ) if action.startswith("plan_") else (1, MAX_MINUTES)
            scheduler.prepare(action, min_minutes=lower, max_minutes=upper)
    except SchedulerError as exc:
        print(f"随机计划错误：{exc}", file=sys.stderr)
        print(f"::error title=随机计划错误::{exc}", file=sys.stderr)
        return 1
    except GitHubAPIError as exc:
        print(f"GitHub API 操作失败（HTTP {exc.status or '网络错误'}）；为避免重复发送，未自动重试。", file=sys.stderr)
        print(f"::error title=随机计划 API 错误::HTTP {exc.status or '网络错误'}，请检查工作流写入权限。", file=sys.stderr)
        return 1
    except Exception:
        print("随机计划发生未预期错误，未输出底层错误以保护凭据；请检查运行和状态，不会自动重发。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
