"""Read-only v1 plan decoder for migration and historical reports; no scheduler."""

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


