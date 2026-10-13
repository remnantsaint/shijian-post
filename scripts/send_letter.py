"""Send a custom UTF-8 letter through QQ SMTP using GitHub Actions secrets."""

import os
import smtplib
import ssl
import sys
import html
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid

if __package__:
    from .send_mail import ConfigurationError, address, required, failure_details, add_image_attachment
    from .time_utils import BEIJING, format_beijing, format_utc, parse_utc, utc_now
else:
    from send_mail import ConfigurationError, address, required, failure_details, add_image_attachment
    from time_utils import BEIJING, format_beijing, format_utc, parse_utc, utc_now


MAX_BODY_CHARS = 100_000
MAX_IMAGES = 4


def text_env(name, max_chars):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"缺少邮件字段：{name}；停止发送，不能绕过信件预留")
    if len(value) > max_chars:
        raise ConfigurationError(f"{name} 内容过长")
    if "\x00" in value:
        raise ConfigurationError(f"{name} 包含非法字符")
    return value


def header_text(name, value, max_chars):
    if len(value) > max_chars or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConfigurationError(f"{name} 必须是长度合适的单行文本，不能包含控制字符")
    return value


def image_paths_from_env():
    paths = []
    single = os.environ.get("IMAGE_PATH", "").strip()
    multiple = os.environ.get("IMAGE_PATHS", "").strip()
    if single:
        paths.append(single)
    if multiple:
        paths.extend(part.strip() for part in multiple.split(";") if part.strip())
    paths = list(dict.fromkeys(paths))
    if len(paths) > MAX_IMAGES:
        raise ConfigurationError(f"配图最多支持 {MAX_IMAGES} 张")
    return paths


def send(*, metadata=None):
    """Return the acceptance timestamp; optional metadata never contains the letter."""
    sender = address("QQ_EMAIL")
    password = required("QQ_SMTP_AUTH_CODE")
    recipient = address("MAIL_TO")
    if os.environ.get("MAIL_BODY", "").strip():
        subject = text_env("MAIL_SUBJECT", 120)
        body = text_env("MAIL_BODY", MAX_BODY_CHARS)
    elif os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        raise ConfigurationError("信件正文尚未预留，停止发送以避免未登记的信件")
    else:
        # Explicit local use keeps the existing preview behavior. Production
        # Actions must always use content reserved by the preceding step.
        if __package__:
            from .dragon_letters import make_letter
        else:
            from dragon_letters import make_letter
        subject, body = make_letter()
    subject = header_text("MAIL_SUBJECT", subject, 120)
    from_name = os.environ.get("MAIL_FROM_NAME", "小龙娘").strip() or "小龙娘"
    from_name = header_text("MAIL_FROM_NAME", from_name, 120)

    message = EmailMessage(policy=policy.SMTP)
    message["From"] = formataddr((from_name, sender))
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = format_datetime(utc_now().astimezone(BEIJING))
    message["Message-ID"] = make_msgid(domain="qq.com")
    message.set_content(body, charset="utf-8")

    for image_path in image_paths_from_env():
        add_image_attachment(message, image_path)
    if metadata is not None:
        metadata["attachment_count"] = sum(1 for _ in message.iter_attachments())

    client = None
    try:
        client = smtplib.SMTP_SSL(
            "smtp.qq.com", 465, timeout=30, context=ssl.create_default_context()
        )
        client.login(sender, password)
        message.replace_header("Date", format_datetime(utc_now().astimezone(BEIJING)))
        refused = client.send_message(message, from_addr=sender, to_addrs=[recipient])
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
        accepted_at = format_utc(utc_now())
        if metadata is not None:
            metadata["accepted_at"] = accepted_at
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass
    return accepted_at


def write_accepted_output(accepted_at):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as output:
            output.write(f"accepted_at={accepted_at}\n")
    except Exception:
        print("SMTP 已接受，但接受时间输出保存失败；不要因此重复发送。", file=sys.stderr)


def append_send_summary(status, metadata=None, explanation=""):
    """Record delivery evidence without exposing the reserved surprise content."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        labels = {
            "accepted": "SMTP 已接受",
            "failed": "发送失败或未发送",
            "uncertain": "发送结果不确定，需核验",
        }
        metadata = metadata or {}
        accepted_at = metadata.get("accepted_at") if status == "accepted" else None
        recorded_at = parse_utc(accepted_at) if accepted_at else utc_now()
        time_label = "SMTP 接受时间" if accepted_at else "结果检查时间"
        count = metadata.get("attachment_count")
        if type(count) is int and 0 <= count <= MAX_IMAGES:
            attachments = f"有（{count} 张）" if count else "无（0 张）"
        else:
            attachments = "未完成检查"
        summary = (
            "## 小龙娘来信发送记录\n\n"
            "| 项目 | 状态 |\n|---|---|\n"
            f"| 发送结果 | {labels.get(status, labels['uncertain'])} |\n"
            f"| {time_label}（北京时间 UTC+8） | {format_beijing(recorded_at)} |\n"
            f"| 图片附件 | {attachments} |\n\n"
            "标题和正文保留为邮件中的惊喜，不写入 Actions 摘要。\n\n"
        )
        if explanation:
            summary += f"{html.escape(explanation)}\n\n"
        if status == "accepted":
            summary += "SMTP 接受不等于最终投递或已读，请以收件邮箱为准。\n"
        elif status == "uncertain":
            summary += "邮件可能已发出；核对收件箱后再决定是否发送，不能自动重试。\n"
        summary += "\n本记录不包含邮箱地址、授权码、信件标题、正文或附件路径。\n"
        with open(path, "a", encoding="utf-8") as output:
            output.write(summary)
    except Exception:
        # Reporting errors cannot change SMTP acceptance or trigger a resend.
        print("发送摘要保存失败，请查看发送步骤日志；不要仅因此重复发送。", file=sys.stderr)


def main():
    metadata = {}
    try:
        accepted_at = send(metadata=metadata)
    except Exception as exc:
        status, explanation = failure_details(exc)
        print(explanation, file=sys.stderr)
        append_send_summary(status, metadata, explanation)
        return 1
    write_accepted_output(accepted_at)
    print(
        f"QQ SMTP 已接受小作文邮件，北京时间 {format_beijing(parse_utc(accepted_at))}；"
        "请以收件箱最终投递为准。"
    )
    append_send_summary("accepted", metadata)
    return 0


if __name__ == "__main__":
    sys.exit(main())
