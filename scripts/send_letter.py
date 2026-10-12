"""Send a custom UTF-8 letter through QQ SMTP using GitHub Actions secrets."""

import os
import smtplib
import ssl
import sys
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid

if __package__:
    from .send_mail import address, required, failure_details, add_image_attachment
    from .time_utils import BEIJING, format_beijing, format_utc, parse_utc, utc_now
else:
    from send_mail import address, required, failure_details, add_image_attachment
    from time_utils import BEIJING, format_beijing, format_utc, parse_utc, utc_now


MAX_BODY_CHARS = 100_000
MAX_IMAGES = 4


def text_env(name, max_chars):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"缺少邮件字段：{name}")
    if len(value) > max_chars:
        raise ValueError(f"{name} 内容过长")
    if "\x00" in value:
        raise ValueError(f"{name} 包含非法字符")
    return value


def image_paths_from_env():
    paths = []
    single = os.environ.get("IMAGE_PATH", "").strip()
    multiple = os.environ.get("IMAGE_PATHS", "").strip()
    if single:
        paths.append(single)
    if multiple:
        paths.extend(part.strip() for part in multiple.split(";") if part.strip())
    # Preserve order while avoiding duplicate attachments.
    paths = list(dict.fromkeys(paths))
    if len(paths) > MAX_IMAGES:
        raise ValueError(f"配图最多支持 {MAX_IMAGES} 张")
    return paths


def send():
    sender = address("QQ_EMAIL")
    password = required("QQ_SMTP_AUTH_CODE")
    recipient = address("MAIL_TO")
    subject = text_env("MAIL_SUBJECT", 120)
    body = text_env("MAIL_BODY", MAX_BODY_CHARS)
    from_name = os.environ.get("MAIL_FROM_NAME", "小龙娘").strip() or "小龙娘"

    message = EmailMessage(policy=policy.SMTP)
    message["From"] = formataddr((from_name, sender))
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = format_datetime(utc_now().astimezone(BEIJING))
    message["Message-ID"] = make_msgid(domain="qq.com")
    message.set_content(body, charset="utf-8")

    for image_path in image_paths_from_env():
        add_image_attachment(message, image_path)

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
    finally:
        if client is not None:
            try:
                client.quit()
            except (OSError, smtplib.SMTPException):
                try:
                    client.close()
                except (OSError, smtplib.SMTPException):
                    pass
    return accepted_at


def main():
    try:
        accepted_at = send()
    except Exception as exc:
        status, explanation = failure_details(exc)
        if isinstance(exc, ValueError) and status == "uncertain":
            explanation = "邮件文案或配图配置错误；未尝试发送。"
        print(explanation, file=sys.stderr)
        return 1
    print(
        f"QQ SMTP 已接受小作文邮件，北京时间 {format_beijing(parse_utc(accepted_at))}；"
        "请以收件箱最终投递为准。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
