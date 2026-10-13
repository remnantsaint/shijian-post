"""Use the actual system clock, store UTC instants and display Beijing time."""

from datetime import datetime, timedelta, timezone


UTC = timezone.utc
BEIJING = timezone(timedelta(hours=8))


class DeadlineExpired(ValueError):
    pass


def check_deadline(deadline, at):
    if deadline and parse_utc(deadline) < aware(at):
        raise DeadlineExpired('任务超过最晚发送时间，未提交邮件。')


def utc_now():
    return datetime.now(UTC)


def aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须包含明确的时区")
    return value


def parse_utc(value):
    if not isinstance(value, str):
        raise ValueError("时间格式无效")
    try:
        return aware(datetime.fromisoformat(value.replace("Z", "+00:00"))).astimezone(UTC)
    except (ValueError, OverflowError):
        raise ValueError("时间格式无效或缺少时区") from None


def format_utc(value):
    value = aware(value).astimezone(UTC)
    precision = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=precision).replace("+00:00", "Z")


def format_beijing(value):
    return aware(value).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def letter_with_time(body, written_at):
    """Append the actual composition time; callers keep it out of dedup hashes."""
    return body + "\n\n" + f"写信时间（北京时间 UTC+8）：{format_beijing(written_at)}"
