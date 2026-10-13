"""Atomically reserve a never-reused letter body before any SMTP attempt.

Version 1 numbers are decoded by an immutable digest snapshot, never by the
current array lengths. Version 2 identifies letters by the complete UTF-8 body;
changing a title or the position of a paragraph cannot make that body new.
"""
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sys
import uuid
from functools import lru_cache

from scripts.github_api import GitHubAPI, GitHubAPIError
from scripts.dragon_letters import OPENINGS, SCENES, THOUGHTS, EXTRAS, CLOSINGS, TITLES
from scripts.state_store import StateBranch

BRANCH = "mail-bot-state"
PATH = ".mail-bot/used-letters.json"
LEGACY_CATALOG = "dragon-letters-v1"
LEGACY_TOTAL = 1152
LEGACY_REVISIONS = (
    "d548616c8dcf9c20fd95f4c7317615414ecd5c10",  # Literal backslash separators.
    "58c02168365298bae2c0f484907e15f6c1fb078d",  # Four real-newline paragraphs.
    "d07e97d23658dfac434d3c2b8e29e90d4531e550",  # Current long letters.
)
LEGACY_MAPPING_SHA256 = "e30793d52c8cc788191d1a6d274a7ec427fc59edfbeaa0596d6e518b9414b57b"
MAX_ATTEMPTS = 4
TOTAL = len(OPENINGS) * len(SCENES) * len(THOUGHTS) * len(CLOSINGS)
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class ReservationError(RuntimeError):
    pass


def body_digest(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def reservation_id(digest):
    return "body-sha256:" + digest


@lru_cache(maxsize=1)
def legacy_digests():
    # v1 used the same numbers across three known rendering/text revisions and
    # did not record which revision sent a number. Reserve every possible body.
    # Never regenerate this frozen mapping when changing the active catalog.
    try:
        snapshot = json.loads(Path(__file__).with_name(
            "legacy_letter_body_sha256.json").read_text(encoding="utf-8"))
        digests = snapshot["body_sha256"]
        if (set(snapshot) != {"version", "catalog", "revisions", "body_sha256"}
                or type(snapshot["version"]) is not int or snapshot["version"] != 2
                or snapshot["catalog"] != LEGACY_CATALOG
                or snapshot["revisions"] != list(LEGACY_REVISIONS)
                or not isinstance(digests, list) or len(digests) != LEGACY_TOTAL
                or any(not isinstance(row, list) or len(row) != len(LEGACY_REVISIONS)
                       or any(not isinstance(d, str) or not DIGEST_PATTERN.fullmatch(d) for d in row)
                       for row in digests)):
            raise ValueError
        flattened = [digest for row in digests for digest in row]
        if (len(set(flattened)) != LEGACY_TOTAL * len(LEGACY_REVISIONS)
                or hashlib.sha256("".join(flattened).encode("ascii")).hexdigest() != LEGACY_MAPPING_SHA256):
            raise ValueError
        return tuple(tuple(row) for row in digests)
    except (OSError, KeyError, TypeError, ValueError, UnicodeError):
        raise ReservationError("旧文案编号映射无法校验；已阻止发送。") from None


def validate_catalog():
    for parts in (OPENINGS, SCENES, THOUGHTS, EXTRAS, CLOSINGS, TITLES):
        if (not isinstance(parts, (list, tuple)) or not parts
                or any(not isinstance(part, str) or not part.strip() for part in parts)):
            raise ReservationError("文案素材配置无效；已阻止发送。")
    if (any("\x00" in part for parts in (OPENINGS, SCENES, THOUGHTS, EXTRAS, CLOSINGS)
            for part in parts)
            or any(len(title.strip()) > 120
                   or any(ord(char) < 32 or ord(char) == 127 for char in title.strip())
                   for title in TITLES)):
        raise ReservationError("文案包含无效控制字符或邮件标题过长；已阻止发送。")


def letter(number):
    total = len(OPENINGS) * len(SCENES) * len(THOUGHTS) * len(CLOSINGS)
    if type(number) is not int or not 0 <= number < total:
        raise ReservationError("文案候选编号无效；已阻止发送。")
    d = number
    closing = CLOSINGS[d % len(CLOSINGS)]
    d //= len(CLOSINGS)
    thought = THOUGHTS[d % len(THOUGHTS)]
    d //= len(THOUGHTS)
    scene = SCENES[d % len(SCENES)]
    d //= len(SCENES)
    opening = OPENINGS[d]
    rng = random.Random(number ^ 0x5A17)
    extra_count = rng.randint(2, 5)
    extras = rng.sample(EXTRAS, k=min(extra_count, len(EXTRAS)))
    subject = TITLES[number % len(TITLES)].strip()
    # send_letter.text_env strips the outer whitespace. Hash the same body that
    # SMTP sends so a whitespace-only catalog edit cannot defeat deduplication.
    return subject, "\n\n".join((opening, scene, *extras, thought, closing)).strip()


def candidates():
    validate_catalog()
    total = len(OPENINGS) * len(SCENES) * len(THOUGHTS) * len(CLOSINGS)
    # Multiple candidates can contain the same body after a catalog edit.
    # They share one identity and are reserved only once.
    result = {}
    for number in range(total):
        digest = body_digest(letter(number)[1])
        result.setdefault(digest, number)
    if not result:
        raise ReservationError("没有可用文案；已阻止发送。")
    return result


def validate_legacy_used(used):
    if (not isinstance(used, list)
            or any(type(n) is not int or not 0 <= n < LEGACY_TOTAL for n in used)
            or len(set(used)) != len(used)):
        raise ReservationError("旧文案账本编号无效；已阻止发送。")
    return used.copy()


def validate_ledger(data):
    if not isinstance(data, dict) or type(data.get("version")) is not int:
        raise ReservationError("文案账本版本无效；已阻止发送。")
    if data["version"] == 1:
        if set(data) != {"version", "used"}:
            raise ReservationError("旧文案账本字段无效；已阻止发送。")
        legacy_used = validate_legacy_used(data["used"])
        frozen = legacy_digests()
        used = [{"id": reservation_id(digest), "body_sha256": digest}
                for n in legacy_used for digest in frozen[n]]
        return {"version": 2, "legacy_catalog": LEGACY_CATALOG,
                "legacy_used": legacy_used, "used": used}
    if data["version"] != 2:
        raise ReservationError("不支持的文案账本版本；已阻止发送。")
    if (set(data) != {"version", "legacy_catalog", "legacy_used", "used"}
            or data["legacy_catalog"] != LEGACY_CATALOG):
        raise ReservationError("文案账本字段或旧文案版本无效；已阻止发送。")
    legacy_used = validate_legacy_used(data["legacy_used"])
    used = data["used"]
    if not isinstance(used, list):
        raise ReservationError("文案账本记录无效；已阻止发送。")
    digests = set()
    for entry in used:
        if (not isinstance(entry, dict) or set(entry) != {"id", "body_sha256"}
                or not isinstance(entry["body_sha256"], str)
                or not DIGEST_PATTERN.fullmatch(entry["body_sha256"])
                or entry["id"] != reservation_id(entry["body_sha256"])
                or entry["body_sha256"] in digests):
            raise ReservationError("文案账本身份或摘要无效；已阻止发送。")
        digests.add(entry["body_sha256"])
    frozen = legacy_digests()
    if any(digest not in digests for n in legacy_used for digest in frozen[n]):
        raise ReservationError("旧文案预留记录缺失；已阻止发送。")
    return {"version": 2, "legacy_catalog": LEGACY_CATALOG,
            "legacy_used": legacy_used, "used": [entry.copy() for entry in used]}


def load_ledger(api):
    endpoint = f"/repos/{api.repo}/contents/{PATH}"
    try:
        item = api.request("GET", endpoint + "?ref=" + BRANCH)
    except GitHubAPIError as exc:
        if exc.status != 404:
            raise
        return validate_ledger({"version": 1, "used": []}), None
    try:
        if (not isinstance(item, dict) or item.get("encoding") != "base64"
                or not isinstance(item.get("sha"), str) or not item["sha"]
                or not isinstance(item.get("content"), str)):
            raise ValueError
        content = base64.b64decode("".join(item["content"].split()), validate=True)
        data = json.loads(content.decode("utf-8"))
    except (KeyError, TypeError, ValueError, UnicodeError, binascii.Error):
        raise ReservationError("文案账本无法解析；已阻止发送。") from None
    return validate_ledger(data), item["sha"]


def reserve(api, *, choose=None, max_attempts=MAX_ATTEMPTS):
    """Retry only definite reservation conflicts, never mail or uncertain writes.

    Returns (stable_id, subject, body) only after a successful durable PUT.
    Existing v1 numbers remain in legacy_used; no ledger or history is reset.
    """
    if type(max_attempts) is not int or not 1 <= max_attempts <= MAX_ATTEMPTS:
        raise ReservationError("预留重试次数配置无效；已阻止发送。")
    catalog = candidates()
    choose = choose or random.SystemRandom().choice
    StateBranch(api).ensure_branch()
    ledger, sha = load_ledger(api)
    endpoint = f"/repos/{api.repo}/contents/{PATH}"
    for attempt in range(max_attempts):
        used = {entry["body_sha256"] for entry in ledger["used"]}
        available = [digest for digest in catalog if digest not in used]
        if not available:
            raise ReservationError("所有信件组合已经用完，停止发送；需要补充新素材。")
        digest = choose(available)
        if digest not in available:
            raise ReservationError("文案随机选择结果无效；已阻止发送。")
        stable_id = reservation_id(digest)
        subject, body = letter(catalog[digest])
        if body_digest(body) != digest:
            raise ReservationError("文案素材在预留期间发生变化；已阻止发送。")
        updated = {**ledger, "used": ledger["used"] + [
            {"id": stable_id, "body_sha256": digest}]}
        payload = {"message": "Reserve unique letter body before sending",
                   "branch": BRANCH, "content": base64.b64encode(
                       (json.dumps(updated, ensure_ascii=False) + "\n").encode("utf-8")
                   ).decode("ascii")}
        if sha is not None:
            payload["sha"] = sha
        try:
            api.request("PUT", endpoint, payload)
            return stable_id, subject, body
        except GitHubAPIError as exc:
            if exc.status not in (409, 422):
                raise
            refreshed, new_sha = load_ledger(api)
            # A 422 without a changed file is a validation/permission problem,
            # not evidence that another caller won a compare-and-swap.
            if exc.status == 422 and new_sha == sha:
                raise
            if attempt + 1 == max_attempts:
                raise ReservationError("文案账本持续发生预留竞争；已阻止发送，请稍后检查。") from None
            ledger, sha = refreshed, new_sha
    raise ReservationError("文案预留未完成；已阻止发送。")


def mask_for_actions(value):
    if not value:
        return
    safe = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::add-mask::{safe}")


def export_for_actions(stable_id, subject, body, env_path):
    if "\n" in subject or "\r" in subject:
        raise ReservationError("邮件标题包含无效换行；已阻止发送。")
    mask_for_actions(subject)
    for line in body.splitlines():
        if line.strip():
            mask_for_actions(line)
    marker = "DRAGON_LETTER_END_" + uuid.uuid4().hex
    while marker in body.splitlines():
        marker = "DRAGON_LETTER_END_" + uuid.uuid4().hex
    with open(env_path, "a", encoding="utf-8") as env_file:
        env_file.write(f"MAIL_SUBJECT={subject}\nMAIL_BODY<<{marker}\n{body}\n{marker}\n")
    print("已预留一封信件；发送失败或结果不明时也不会再次使用。")


def main():
    # Validate export configuration before consuming a reservation.
    env_path = os.environ.get("GITHUB_ENV")
    if not env_path:
        raise ReservationError("缺少 GitHub Actions 文案导出配置；已阻止发送。")
    api = GitHubAPI(os.environ.get("GITHUB_REPOSITORY", ""),
                    os.environ.get("GH_TOKEN", ""))
    stable_id, subject, body = reserve(api)
    export_for_actions(stable_id, subject, body, env_path)


def cli():
    try:
        main()
    except ReservationError as exc:
        # ReservationError is constructed only from fixed local diagnostics.
        print(f"信件预留失败，已阻止发送：{exc}", file=sys.stderr)
        return 1
    except GitHubAPIError as exc:
        # Raw API messages can contain private text. Only numeric HTTP status
        # is safe to expose; a missing status may be an uncertain write.
        if type(exc.status) is int and 100 <= exc.status <= 599:
            reason = f"GitHub API 请求失败（HTTP {exc.status}）；请检查权限或预留竞争。"
        else:
            reason = "GitHub API 连接失败或结果不确定；停止发送，不能自动重试。"
        print("信件预留失败，已阻止发送：" + reason, file=sys.stderr)
        return 1
    except Exception:
        # Unexpected error details might contain environment/letter values.
        print("信件预留失败，已阻止发送：程序发生内部错误，请检查配置和运行记录。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(cli())
