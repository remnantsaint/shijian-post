"""Send one UTF-8 message over verified QQ SMTP SSL; no third-party packages."""

import os
import random
import smtplib
import ssl
import sys
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid


class ConfigurationError(ValueError):
    pass


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"缺少 GitHub Actions Secret：{name}")
    return value


def address(name):
    value = required(name)
    # Accept one bare mailbox only; avoid ambiguous recipients and header injection.
    if (value.count("@") != 1 or any(c.isspace() for c in value)
            or any(c in value for c in '<>,;:"\\')
            or not all(value.split("@"))):
        raise ConfigurationError(f"{name} 必须是单个完整邮箱地址，不能包含显示名称或换行")
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        raise ConfigurationError(f"{name} 必须使用 ASCII 邮箱地址") from None
    return value


def build_message(sender, recipient, kind):
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = sender
    message["To"] = recipient
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain="qq.com")
    if kind == "test":
        message["Subject"] = "邮件机器人测试：你好！"
        body = "这是一封来自 GitHub Actions 的测试邮件。\n中文 UTF-8 编码正常。祝你今天顺利！"
    elif kind == "random":
        message["Subject"] = "今日随机小消息"
        body = random.choice([
            "记得喝水，也给自己一点休息的时间。",
            "今天也可以从一件小事开始，慢慢向前。",
            "愿你今天遇到一件让你微笑的小事。",
        ])
    else:
        raise ConfigurationError("MESSAGE_TYPE 仅支持 test 或 random")
    message.set_content(body, charset="utf-8")
    return message


def send():
    sender = address("QQ_EMAIL")
    password = required("QQ_SMTP_AUTH_CODE")
    recipient = address("MAIL_TO")
    message = build_message(sender, recipient, os.environ.get("MESSAGE_TYPE", "test"))
    client = None
    try:
        client = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30,
                                  context=ssl.create_default_context())
        client.login(sender, password)
        refused = client.send_message(message, from_addr=sender, to_addrs=[recipient])
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    finally:
        if client is not None:
            # Once DATA was accepted, a QUIT failure must not suggest resending.
            try:
                client.quit()
            except (OSError, smtplib.SMTPException):
                client.close()


def main():
    try:
        send()
    except ConfigurationError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
    except smtplib.SMTPAuthenticationError:
        print("SMTP 认证失败：请检查 QQ_EMAIL、SMTP 授权码及 QQ 邮箱 SMTP 服务开关。", file=sys.stderr)
    except smtplib.SMTPRecipientsRefused:
        print("SMTP 拒绝收件地址：请检查 MAIL_TO 和邮箱接收限制。", file=sys.stderr)
    except smtplib.SMTPSenderRefused:
        print("SMTP 拒绝发件地址：请检查 QQ_EMAIL 是否与授权码对应。", file=sys.stderr)
    except ssl.SSLError:
        print("SMTP TLS 验证或握手失败：请检查证书和网络连接。", file=sys.stderr)
    except smtplib.SMTPException:
        print("SMTP 服务器拒绝发送或连接中断；若已接收邮件正文，请先检查收件箱再重试。", file=sys.stderr)
    except (TimeoutError, OSError):
        print("SMTP 网络连接失败或超时：请检查 smtp.qq.com:465 的连接。", file=sys.stderr)
    except Exception:
        # Never log raw exception strings: servers may echo credentials/addresses.
        print("发送发生未预期错误；为保护凭据，未输出底层异常内容。", file=sys.stderr)
    else:
        print("QQ SMTP 已接受邮件，请检查收件箱或垃圾邮件文件夹。")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
