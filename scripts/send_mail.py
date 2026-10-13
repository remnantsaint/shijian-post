"""Send one UTF-8 message over verified QQ SMTP SSL; no third-party packages."""

import os
import random
import smtplib
import ssl
import sys
import html
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path

if __package__:
    from .time_utils import BEIJING, format_beijing, format_utc, parse_utc, utc_now
else:
    from time_utils import BEIJING, format_beijing, format_utc, parse_utc, utc_now


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_CUSTOM_BODY_CHARACTERS = 100000
MAX_CUSTOM_SUBJECT_CHARACTERS = 200


class ConfigurationError(ValueError):
    pass


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"缺少 GitHub Actions Secret：{name}")
    return value


def address(name):
    value = required(name)
    if (value.count("@") != 1 or any(c.isspace() for c in value)
            or any(c in value for c in '<>,;:"\\')
            or not all(value.split("@"))):
        raise ConfigurationError(f"{name} 必须是单个完整邮箱地址，不能包含显示名称或换行")
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        raise ConfigurationError(f"{name} 必须使用 ASCII 邮箱地址") from None
    return value


def custom_content():
    subject = os.environ.get("CUSTOM_MAIL_SUBJECT", os.environ.get("MAIL_SUBJECT", ""))
    body = os.environ.get("CUSTOM_MAIL_BODY", os.environ.get("MAIL_BODY", ""))
    if (len(subject) > MAX_CUSTOM_SUBJECT_CHARACTERS
            or any(not character.isprintable() for character in subject)):
        raise ConfigurationError("自定义邮件主题必须是单行文本，且不能超过 200 个字符")
    subject = subject if subject.strip() else "来自邮件机器人的消息"
    if not body.strip():
        raise ConfigurationError("自定义邮件正文不能为空")
    if len(body) > MAX_CUSTOM_BODY_CHARACTERS or "\0" in body:
        raise ConfigurationError("自定义邮件正文不能包含空字符，且不能超过 100000 个字符")
    try:
        subject.encode("utf-8")
        body.encode("utf-8")
    except UnicodeEncodeError:
        raise ConfigurationError("自定义邮件内容必须是合法的 UTF-8 文本") from None
    return subject, body


def build_message(sender, recipient, kind):
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = sender
    message["To"] = recipient
    message["Date"] = format_datetime(utc_now().astimezone(BEIJING))
    message["Message-ID"] = make_msgid(domain="qq.com")
    if kind == "test":
        message["Subject"] = "邮件机器人测试：你好！"
        body = "这是一封来自 GitHub Actions 的测试邮件。\n中文 UTF-8 编码正常。祝你今天顺利！"
    elif kind == "random":
        if os.environ.get("MAIL_BODY") and os.environ.get("MAIL_SUBJECT"):
            subject, body = os.environ["MAIL_SUBJECT"], os.environ["MAIL_BODY"]
        else:
            if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
                raise ConfigurationError("随机信件尚未预留，停止发送以避免重复")
            subject = "随机信件编码预览"
            body = "这是一段用于离线编码检查的随机信件预览。"
        message["Subject"] = subject
    elif kind == "custom":
        subject, body = custom_content()
        message["Subject"] = subject
    else:
        raise ConfigurationError("MESSAGE_TYPE 仅支持 test、random 或 custom")
    message.set_content(body, charset="utf-8")
    return message


def add_image_attachment(message, image_path, repository_root=None):
    if not image_path:
        return
    relative = Path(image_path)
    if (relative.is_absolute() or not relative.parts or relative.parts[0] != "images"
            or ".." in relative.parts or any(c in image_path for c in "\r\n\0")):
        raise ConfigurationError("IMAGE_PATH 必须是 images/ 下的相对图片路径，不能包含越界或换行")
    root = Path(repository_root or REPOSITORY_ROOT).resolve()
    try:
        path = (root / relative).resolve(strict=True)
        path.relative_to(root / "images")
        if not path.is_file():
            raise ConfigurationError("IMAGE_PATH 必须指向普通图片文件")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise ConfigurationError("图片附件不能超过 5 MiB")
        with path.open("rb") as image_file:
            data = image_file.read(MAX_IMAGE_BYTES + 1)
    except ConfigurationError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise ConfigurationError("IMAGE_PATH 图片不存在、无法读取或超出 images/ 目录") from None
    if len(data) > MAX_IMAGE_BYTES:
        raise ConfigurationError("图片附件不能超过 5 MiB")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        subtype = "png"
    elif data.startswith(b"\xff\xd8\xff"):
        subtype = "jpeg"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        subtype = "gif"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        subtype = "webp"
    else:
        raise ConfigurationError("图片附件仅支持 PNG、JPEG、GIF 或 WebP，不能仅修改文件扩展名")
    message.add_attachment(data, maintype="image", subtype=subtype, filename=relative.name)


def send():
    sender = address("QQ_EMAIL")
    password = required("QQ_SMTP_AUTH_CODE")
    recipient = address("MAIL_TO")
    message = build_message(sender, recipient, os.environ.get("MESSAGE_TYPE", "test"))
    add_image_attachment(message, os.environ.get("IMAGE_PATH", "").strip())
    client = None
    try:
        client = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30,
                                  context=ssl.create_default_context())
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
    return {
        "subject": str(message["Subject"]),
        "body": message.get_body(preferencelist=("plain",)).get_content(),
        "has_image": any(message.iter_attachments()),
        "accepted_at": accepted_at,
    }


def failure_details(exc):
    if isinstance(exc, ConfigurationError):
        return "failed", f"配置错误：{exc}"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "failed", "SMTP 认证失败：请检查 QQ_EMAIL、SMTP 授权码及 QQ 邮箱 SMTP 服务开关。"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "failed", "SMTP 拒绝收件地址：请检查 MAIL_TO 和邮箱接收限制。"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "failed", "SMTP 拒绝发件地址：请检查 QQ_EMAIL 是否与授权码对应。"
    if isinstance(exc, ssl.SSLError):
        return "failed", "SMTP TLS 验证或握手失败：请检查证书和网络连接。"
    if isinstance(exc, smtplib.SMTPDataError):
        return "failed", "SMTP 拒绝邮件正文：请检查邮件内容或邮箱发送限制。"
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return "uncertain", "SMTP 连接中断，发送结果可能不确定；重试前请先检查收件箱。"
    if isinstance(exc, smtplib.SMTPException):
        return "uncertain", "SMTP 服务器拒绝发送或连接中断；若已接收邮件正文，请先检查收件箱再重试。"
    if isinstance(exc, (TimeoutError, OSError)):
        return "uncertain", "SMTP 网络错误或超时：请检查 smtp.qq.com:465；若发生在提交正文时，重试前请先检查收件箱。"
    return "uncertain", "发送发生未预期错误；为保护凭据，未输出底层异常内容。"


def append_send_summary(status, result=None, explanation=""):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    labels = {"accepted": "SMTP 已接受", "failed": "发送失败或未发送", "uncertain": "发送结果不确定，需核验"}
    kind = os.environ.get("MESSAGE_TYPE", "test")
    kind = {"test": "测试邮件", "random": "随机小消息", "custom": "自定义邮件"}.get(kind, "配置无效")
    recorded_at = parse_utc(result["accepted_at"]) if result else utc_now()
    time_label = "SMTP 接受时间" if result else "失败检查时间"
    summary = (
        "## 邮件发送记录\n\n"
        "| 项目 | 状态 |\n|---|---|\n"
        f"| 发送结果 | {labels[status]} |\n"
        f"| {time_label}（北京时间 UTC+8） | {format_beijing(recorded_at)} |\n"
        f"| 邮件类型 | {kind} |\n"
    )
    if result:
        summary += f"| 图片附件 | {'有' if result['has_image'] else '无'} |\n\n"
        summary += f"### {html.escape(result['subject'])}\n\n<pre>{html.escape(result['body'])}</pre>\n\n"
        summary += "SMTP 接受不等于最终投递或已读，请以收件邮箱为准。\n"
    else:
        summary += f"\n{html.escape(explanation)}\n"
    summary += "\n本记录不包含邮箱地址或授权码。\n"
    try:
        with open(summary_path, "a", encoding="utf-8") as output:
            output.write(summary)
    except OSError:
        print("发送摘要保存失败，请查看发送步骤日志；请勿仅因此重复发送。", file=sys.stderr)


def write_accepted_output(accepted_at):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as output:
            output.write(f"accepted_at={accepted_at}\n")
    except OSError:
        print("SMTP 已接受，但发送时间输出保存失败；请勿因此重复发送。", file=sys.stderr)


def main():
    try:
        result = send()
    except Exception as exc:
        status, explanation = failure_details(exc)
        print(explanation, file=sys.stderr)
        append_send_summary(status, explanation=explanation)
        return 1
    else:
        print(f"QQ SMTP 已接受邮件，北京时间 {format_beijing(parse_utc(result['accepted_at']))}；请检查收件箱或垃圾邮件文件夹。")
        write_accepted_output(result["accepted_at"])
        append_send_summary("accepted", result)
        return 0


if __name__ == "__main__":
    sys.exit(main())
