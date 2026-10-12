"""Optional short delay after the GitHub runner starts; no credentials needed."""

import os
import random
import sys
import time
from datetime import timedelta

if __package__:
    from .time_utils import format_beijing, utc_now
else:
    from time_utils import format_beijing, utc_now


def choose_delay(mode):
    if mode == "立即发送":
        return 0
    if mode == "3 分钟内随机发送":
        return random.randint(1, 180)
    raise ValueError("SEND_DELAY 仅支持立即发送或 3 分钟内随机发送")


def main():
    try:
        seconds = choose_delay(os.environ.get("SEND_DELAY", "立即发送"))
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1
    if seconds:
        target = utc_now() + timedelta(seconds=seconds)
        print(f"等待 {seconds} 秒，计划北京时间 {format_beijing(target)} 开始发送。", flush=True)
        time.sleep(seconds)
        print(f"等待实际结束：北京时间 {format_beijing(utc_now())}。", flush=True)
    else:
        print("立即发送。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
