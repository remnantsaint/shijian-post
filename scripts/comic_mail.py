"""Authenticated draft-release upload and at-most-once comic mail delivery.

No public HTTP server, SMTP retries, arbitrary download URLs or image commits.
Only metadata and hashes are persisted on the existing state branch.
"""

import argparse
import base64
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import stat
import struct
import sys
import uuid
import zlib

from .github_api import GitHubAPI, GitHubAPIError
from .random_scheduler import STATE_BRANCH, StateStore
from .time_utils import format_utc, utc_now
from . import send_mail


RELEASE_TAG = "mail-bot-temporary-images-v1"
MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_BODY = 10_000
WORKFLOW = "random.yml"
ACTION = "发送漫画邮件"
STATUSES = {"uploading", "upload_failed", "upload_uncertain", "dispatching",
            "claimed", "accepted", "failed", "uncertain"}


class ComicError(ValueError):
    """Only static diagnostics may be displayed."""


def request_id(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise ComicError("请求编号必须是规范的小写 UUID；同一次任务必须复用同一编号") from None
    return value


def dimensions(width, height):
    if not 0 < width <= 8192 or not 0 < height <= 8192 or width * height > MAX_PIXELS:
        raise ComicError("图片尺寸超过限制（单边 8192，最多 4000 万像素）")


def image_type(data):
    """Validate container structure, MIME, dimensions and bounded byte size."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
        raise ComicError("图片为空或超过 5 MiB")
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            offset, found_data, first = 8, False, True
            while offset + 12 <= len(data):
                size = int.from_bytes(data[offset:offset + 4], "big")
                end = offset + 12 + size
                if end > len(data):
                    raise ValueError
                tag = data[offset + 4:offset + 8]
                payload = data[offset + 8:end - 4]
                if zlib.crc32(tag + payload) != int.from_bytes(data[end - 4:end], "big"):
                    raise ValueError
                if first:
                    if tag != b"IHDR" or size != 13:
                        raise ValueError
                    dimensions(*struct.unpack(">II", payload[:8]))
                    depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8},
                              4: {8, 16}, 6: {8, 16}}
                    if (payload[8] not in depths.get(payload[9], set())
                            or payload[10:12] != b"\0\0" or payload[12] not in (0, 1)):
                        raise ValueError
                    first = False
                found_data |= tag == b"IDAT" and size > 0
                if tag == b"IEND":
                    if size or end != len(data) or not found_data:
                        raise ValueError
                    return "png", "image/png"
                offset = end
            raise ValueError
        if data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
            offset, sized = 2, False
            while offset < len(data) - 2:
                if data[offset] != 0xFF:
                    raise ValueError
                while data[offset] == 0xFF:
                    offset += 1
                marker = data[offset]
                offset += 1
                if marker in {0xD8, 0xD9, 0x00}:
                    raise ValueError
                if marker == 0xDA:  # Entropy-coded scan is opaque, never executed.
                    if not sized or offset + 2 > len(data):
                        raise ValueError
                    length = int.from_bytes(data[offset:offset + 2], "big")
                    if length < 2 or offset + length >= len(data):
                        raise ValueError
                    return "jpg", "image/jpeg"
                size = int.from_bytes(data[offset:offset + 2], "big")
                if size < 2 or offset + size > len(data):
                    raise ValueError
                if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                    if size < 8:
                        raise ValueError
                    height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
                    dimensions(width, height)
                    sized = True
                offset += size
            raise ValueError
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            if int.from_bytes(data[4:8], "little") + 8 != len(data):
                raise ValueError
            offset, sized, found = 12, False, False
            while offset + 8 <= len(data):
                tag = data[offset:offset + 4]
                size = int.from_bytes(data[offset + 4:offset + 8], "little")
                end = offset + 8 + size
                if end > len(data):
                    raise ValueError
                payload = data[offset + 8:end]
                if tag == b"VP8X":
                    if size != 10 or payload[0] & 2:
                        raise ValueError
                    dimensions(1 + int.from_bytes(payload[4:7], "little"),
                               1 + int.from_bytes(payload[7:10], "little"))
                    sized = True
                if tag == b"VP8 " and size >= 10 and payload[3:6] == b"\x9d\x01\x2a":
                    dimensions(int.from_bytes(payload[6:8], "little") & 0x3FFF,
                               int.from_bytes(payload[8:10], "little") & 0x3FFF)
                    sized = found = True
                if tag == b"VP8L" and size >= 5 and payload[0] == 0x2F:
                    packed = int.from_bytes(payload[1:5], "little")
                    dimensions(1 + (packed & 0x3FFF), 1 + ((packed >> 14) & 0x3FFF))
                    sized = found = True
                offset = end + (size % 2)
            if offset == len(data) and sized and found:
                return "webp", "image/webp"
        raise ValueError
    except (ValueError, IndexError, struct.error):
        raise ComicError("图片格式或结构无效；仅支持 PNG、JPEG、静态 WebP") from None


def content(subject, body):
    if (not isinstance(subject, str) or len(subject) > 200
            or any(not char.isprintable() for char in subject)
            or not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY or "\0" in body):
        raise ComicError("邮件主题或正文无效（主题 200 字以内，正文非空且 10000 字以内）")
    try:
        subject.encode("utf-8")
        body.encode("utf-8")
    except UnicodeError:
        raise ComicError("邮件内容必须是合法 UTF-8") from None
    return subject, body


def digest_payload(subject, body, image_sha):
    payload = json.dumps([subject, body, image_sha], ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class Requests:
    def __init__(self, api):
        self.api = api

    def path(self, rid):
        return f"/repos/{self.api.repo}/contents/.mail-bot/image-requests/{request_id(rid)}.json"

    def load(self, rid):
        try:
            file = self.api.request("GET", self.path(rid) + "?ref=" + STATE_BRANCH)
        except GitHubAPIError as exc:
            if exc.status == 404:
                return None, None
            raise
        try:
            record = json.loads(base64.b64decode(file["content"], validate=False))
            if (type(record.get("version")) is not int or record["version"] != 1
                    or record["id"] != rid or record["status"] not in STATUSES
                    or not isinstance(file["sha"], str) or not file["sha"]):
                raise ValueError
            for field in ("image_sha256", "payload_sha256"):
                value = record[field]
                if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                    raise ValueError
            for field in ("release_id", "asset_id"):
                value = record.get(field)
                if value is not None and (type(value) is not int or value <= 0):
                    raise ValueError
            if record["status"] in {"dispatching", "claimed", "accepted", "failed", "uncertain"}:
                if not record.get("asset_id") or not record.get("release_id"):
                    raise ValueError
            if record["mime"] not in {"image/png", "image/jpeg", "image/webp"}:
                raise ValueError
            if type(record["size"]) is not int or not 0 < record["size"] <= MAX_BYTES:
                raise ValueError
            return record, file["sha"]
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise ComicError("图片请求记录无效；已阻止发送") from None

    def save(self, record, sha=None):
        payload = {"message": "Update comic mail request metadata", "branch": STATE_BRANCH,
                   "content": base64.b64encode((json.dumps(record, ensure_ascii=False)
                                               + "\n").encode("utf-8")).decode("ascii")}
        if sha is not None:
            payload["sha"] = sha
        result = self.api.request("PUT", self.path(record["id"]), payload)
        return result["content"]["sha"]


def draft_release(api):
    releases = api.request("GET", f"/repos/{api.repo}/releases?per_page=100")
    if not isinstance(releases, list):
        raise ComicError("无法确认临时图片草稿存储")
    matches = [item for item in releases if item.get("tag_name") == RELEASE_TAG]
    if matches:
        if len(matches) != 1 or matches[0].get("draft") is not True:
            raise ComicError("临时图片 Release 不是唯一草稿；已阻止上传")
        result = matches[0]
    else:
        result = api.request("POST", f"/repos/{api.repo}/releases", {
            "tag_name": RELEASE_TAG, "name": "Mail bot temporary images (keep draft)",
            "draft": True, "body": "Temporary authenticated mail attachments. Do not publish."})
    if (result.get("draft") is not True or result.get("tag_name") != RELEASE_TAG
            or type(result.get("id")) is not int or result["id"] <= 0):
        raise ComicError("临时图片存储未获确认；已停止操作")
    return result["id"]


def upload(api, image, subject, body, rid):
    request_id(rid)
    content(subject, body)
    path = Path(image)
    if path.is_symlink() or not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ComicError("上传路径必须是非符号链接的 PNG/JPEG/WebP 普通文件")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ComicError("上传对象不是普通文件")
        data = stream.read(MAX_BYTES + 1)
    extension, mime = image_type(data)
    expected = {".png": "png", ".jpg": "jpg", ".jpeg": "jpg", ".webp": "webp"}
    if expected[path.suffix.lower()] != extension:
        raise ComicError("图片扩展名与实际格式不一致")
    store = Requests(api)
    previous, _ = store.load(rid)
    if previous is not None:
        raise ComicError("请求编号已存在；已阻止再次上传或发送，请先查询结果")
    StateStore(api).ensure_branch()
    sha256 = hashlib.sha256(data).hexdigest()
    record = {"version": 1, "id": rid, "status": "uploading", "created_at": format_utc(utc_now()),
              "image_sha256": sha256, "payload_sha256": digest_payload(subject, body, sha256),
              "mime": mime, "size": len(data), "cleanup": "pending"}
    sha = store.save(record)  # A failed or ambiguous creation is never retried.
    try:
        release_id = draft_release(api)
        record["release_id"] = release_id
        sha = store.save(record, sha)
        asset = api.upload_release_asset(release_id, f"mailbot-{rid}.{extension}", mime, data)
        if (type(asset.get("id")) is not int or asset.get("size") != len(data)
                or asset.get("name") != f"mailbot-{rid}.{extension}" or asset.get("state") != "uploaded"
                or asset.get("content_type") != mime):
            raise ComicError("图片上传响应无效；不能确认上传结果")
        record.update(asset_id=asset["id"], status="dispatching")
        sha = store.save(record, sha)
    except Exception as exc:
        # Preserve a tombstone; uncertain upload writes must not be retried.
        record["status"] = "upload_failed" if isinstance(exc, GitHubAPIError) and exc.status in (401, 403, 413, 422) else "upload_uncertain"
        try:
            store.save(record, sha)
        except Exception:
            pass
        raise
    dispatch(api, rid, subject, body)
    return rid


def dispatch(api, rid, subject, body):
    repo = api.request("GET", f"/repos/{api.repo}")
    branch = repo.get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise ComicError("无法确认默认分支；没有提交发送")
    # The tombstone precedes dispatch. A lost response cannot cause a resend.
    api.request("POST", f"/repos/{api.repo}/actions/workflows/{WORKFLOW}/dispatches", {
        "ref": branch, "inputs": {"action": ACTION, "request_id": rid,
                                    "custom_subject": subject, "custom_body": body}})


def adopt(api, source, subject, body):
    """Register an authenticated, manually uploaded draft asset once by asset ID."""
    content(subject, body)
    if not isinstance(source, str):
        raise ComicError("附件来源必须为 draft:文件名 或 asset:正整数编号")
    asset_id = None
    if re.fullmatch(r"asset:[1-9][0-9]{0,19}", source):
        asset_id = int(source.split(":", 1)[1])
    elif source.startswith("draft:"):
        name = source[6:]
        if (not name or len(name) > 200 or "/" in name or "\\" in name or ".." in name
                or any(not char.isprintable() for char in name)):
            raise ComicError("草稿附件文件名无效；不能包含路径、换行或控制字符")
    else:
        raise ComicError("附件来源必须为 draft:文件名 或 asset:正整数编号；不接受任意网址")
    release_id = draft_release(api)
    assets = api.request("GET", f"/repos/{api.repo}/releases/{release_id}/assets?per_page=100")
    matches = [item for item in assets if (item.get("id") == asset_id if asset_id is not None
                                          else item.get("name") == name)]
    if len(matches) != 1:
        raise ComicError("未找到唯一的草稿附件；请先保存草稿，确认附件已上传完成")
    asset = matches[0]
    asset_id = asset.get("id")
    if (type(asset_id) is not int or asset_id <= 0 or asset.get("state") != "uploaded"
            or type(asset.get("size")) is not int or not 0 < asset["size"] <= MAX_BYTES):
        raise ComicError("草稿附件身份、状态或大小无效")
    store = Requests(api)
    # Automatic uploads already own a request; adopting them must not bypass it.
    canonical = re.fullmatch(r"mailbot-([0-9a-f-]{36})\.(png|jpg|webp)", asset.get("name", ""))
    if canonical and store.load(request_id(canonical[1]))[0] is not None:
        raise ComicError("这个附件已有发送请求；请查询原请求，禁止重复登记")
    rid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mail-bot:{api.repo.casefold()}:asset:{asset_id}"))
    print(f"漫画请求编号：{rid}；同一草稿附件只能登记一次。")
    if store.load(rid)[0] is not None:
        raise ComicError("这个附件已登记或处理；禁止通过重复运行再次发送")
    data = api.download_release_asset(asset_id, MAX_BYTES)
    extension, mime = image_type(data)
    expected = {".png": "png", ".jpg": "jpg", ".jpeg": "jpg", ".webp": "webp"}
    if expected.get(Path(asset.get("name", "")).suffix.lower()) != extension or len(data) != asset["size"]:
        raise ComicError("草稿附件扩展名、类型或大小不一致")
    StateStore(api).ensure_branch()
    image_sha = hashlib.sha256(data).hexdigest()
    record = {"version": 1, "id": rid, "status": "dispatching", "created_at": format_utc(utc_now()),
              "image_sha256": image_sha, "payload_sha256": digest_payload(subject, body, image_sha),
              "mime": mime, "size": len(data), "cleanup": "pending",
              "asset_id": asset_id, "release_id": release_id}
    store.save(record)  # A competing registration or an uncertain write cannot retry.
    renamed = api.request("PATCH", f"/repos/{api.repo}/releases/assets/{asset_id}",
                          {"name": f"mailbot-{rid}.{extension}"})
    if renamed.get("id") != asset_id or renamed.get("name") != f"mailbot-{rid}.{extension}":
        raise ComicError("附件登记响应未获确认；禁止重跑，请先核验请求")
    return rid


def event_inputs():
    try:
        with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as stream:
            text = stream.read(1024 * 1024 + 1)
        if len(text) > 1024 * 1024:
            raise ValueError
        inputs = json.loads(text)["inputs"]
        rid = request_id(os.environ.get("COMIC_REQUEST_ID") or inputs.get("request_id"))
        subject, body = content(inputs.get("custom_subject", ""), inputs.get("custom_body", ""))
        if inputs.get("action") != ACTION:
            raise ValueError
        return rid, subject, body
    except (OSError, KeyError, TypeError, ValueError):
        raise ComicError("漫画邮件触发参数无效；已阻止发送") from None


def output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(f"{name}={value}\n")


def receive(api, *, sender=None, root=None):
    if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        raise ComicError("漫画接收入口仅供 Actions 调用；本地请使用 upload 或 status")
    if not os.environ.get("COMIC_REQUEST_ID"):
        with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ComicError("触发参数超过大小限制")
        inputs = json.loads(raw)["inputs"]
        if inputs.get("action") != ACTION:
            raise ComicError("漫画邮件操作无效")
        if not inputs.get("request_id"):
            rid = adopt(api, inputs.get("image_path", ""), inputs.get("custom_subject", ""),
                        inputs.get("custom_body", ""))
            os.environ["COMIC_REQUEST_ID"] = rid
    rid, subject, body = event_inputs()
    output("request_id", rid)
    store = Requests(api)
    record, sha = store.load(rid)
    if record is None or record["status"] != "dispatching":
        raise ComicError("请求不存在或已经处理；为防止重复发送，拒绝重跑")
    if digest_payload(subject, body, record["image_sha256"]) != record["payload_sha256"]:
        raise ComicError("触发内容与上传请求不一致；已阻止发送")
    record.update(status="claimed", run_id=os.environ.get("GITHUB_RUN_ID", ""),
                  run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    if not str(record["run_id"]).isdigit() or not str(record["run_attempt"]).isdigit():
        raise ComicError("缺少有效 Actions 运行编号；已阻止发送")
    sha = store.save(record, sha)  # Exactly one CAS winner can call SMTP.
    output("claimed", "true")
    destination = None
    result = None
    smtp_started = False
    status, explanation = "failed", "图片准备失败，未调用 SMTP。"
    try:
        release = api.request("GET", f"/repos/{api.repo}/releases/{record['release_id']}")
        if release.get("draft") is not True or release.get("tag_name") != RELEASE_TAG:
            raise ComicError("临时图片存储已被发布或变更；已阻止发送")
        assets = api.request("GET", f"/repos/{api.repo}/releases/{record['release_id']}/assets?per_page=100")
        asset = next((item for item in assets if item.get("id") == record["asset_id"]), None)
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[record["mime"]]
        if (not asset or asset.get("name") != f"mailbot-{rid}.{ext}"
                or asset.get("size") != record["size"] or asset.get("state") != "uploaded"):
            raise ComicError("附件身份或大小与请求不一致；已阻止发送")
        data = api.download_release_asset(record["asset_id"], MAX_BYTES)
        if hashlib.sha256(data).hexdigest() != record["image_sha256"] or image_type(data)[1] != record["mime"]:
            raise ComicError("附件内容校验失败；已阻止发送")
        repository = Path(root or send_mail.REPOSITORY_ROOT).resolve()
        images = repository / "images"
        if images.is_symlink() or not images.is_dir():
            raise ComicError("附件临时目录无效；已阻止发送")
        temporary = images / ".mail-bot-tmp"
        if temporary.is_symlink():
            raise ComicError("附件临时目录不能是符号链接")
        temporary.mkdir(mode=0o700, exist_ok=True)
        destination = temporary / str(uuid.uuid4())
        destination.mkdir(mode=0o700)
        image = destination / f"attachment.{ext}"
        with image.open("xb") as stream:
            stream.write(data)
        os.environ.update(MESSAGE_TYPE="custom", IMAGE_PATH=str(image.relative_to(repository)))
        smtp_started = True
        result = (sender or send_mail.send)()
        status, explanation = "accepted", "QQ SMTP 已接受漫画邮件；不等于最终投递。"
        record["accepted_at"] = result["accepted_at"]
        send_mail.write_accepted_output(result["accepted_at"])
    except Exception as exc:
        if smtp_started:
            status, explanation = send_mail.failure_details(exc)
        else:
            status, explanation = "failed", "附件准备或权限检查失败，未调用 SMTP；检查上传记录和权限。"
    finally:
        if destination is not None:
            try:
                shutil.rmtree(destination)
            except OSError:
                print("运行器临时图片清理失败；未改变发送结果。", file=sys.stderr)
    record.update(status=status, finished_at=format_utc(utc_now()))
    try:
        store.save(record, sha)
    except Exception:
        explanation += " 持久结果保存未获确认，禁止重跑；先核验运行摘要和收件箱。"
        send_mail.append_send_summary(status, result if status == "accepted" else None, explanation)
        print(explanation, file=sys.stderr)
        return 1
    send_mail.append_send_summary(status, result if status == "accepted" else None, explanation)
    print(explanation)
    return 0 if status == "accepted" else 1


def cleanup(api):
    """Best effort after a successful claim; never overwrites SMTP acceptance."""
    try:
        rid, _, _ = event_inputs()
        store = Requests(api)
        record, sha = store.load(rid)
        if (record is None or record.get("run_id") != os.environ.get("GITHUB_RUN_ID")
                or record.get("run_attempt") != os.environ.get("GITHUB_RUN_ATTEMPT", "1")):
            return
        if record["status"] == "claimed":
            record.update(status="uncertain", finished_at=format_utc(utc_now()))
            sha = store.save(record, sha)
        if record["status"] not in {"accepted", "failed", "uncertain"}:
            return
        try:
            api.request("DELETE", f"/repos/{api.repo}/releases/assets/{record['asset_id']}")
        except GitHubAPIError as exc:
            if exc.status != 404:
                raise
        record["cleanup"] = "deleted"
        store.save(record, sha)
        print("临时草稿附件已删除；发送记录保留。")
    except Exception:
        print("临时附件清理未完成；发送结果保留，禁止仅因清理失败再次发送。", file=sys.stderr)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ComicError("参数无效；请使用 --help，不要传入凭据参数")


def main(argv=None):
    try:
        parser = Parser(description="上传本地漫画并仅提交一次发送；不在参数中接受令牌")
        parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
        commands = parser.add_subparsers(dest="command", required=True)
        uploader = commands.add_parser("upload")
        uploader.add_argument("--image", required=True)
        uploader.add_argument("--message-file", required=True)
        uploader.add_argument("--request-id", required=True)
        registrar = commands.add_parser("register", help="登记手机/浏览器已上传的草稿附件并触发发送")
        registrar.add_argument("--asset", required=True, help="draft:文件名 或 asset:编号")
        registrar.add_argument("--message-file", required=True)
        inspect = commands.add_parser("status")
        inspect.add_argument("--request-id", required=True)
        commands.add_parser("receive")
        commands.add_parser("cleanup")
        args = parser.parse_args(argv)
        if not args.repo:
            raise ComicError("必须明确指定自己的仓库 --repo owner/name")
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            raise ComicError("缺少安全绑定的 GitHub 身份；不要将令牌发送到聊天")
        api = GitHubAPI(args.repo, token)
        if args.command in {"upload", "register"}:
            with open(args.message_file, encoding="utf-8") as stream:
                text = stream.read(128 * 1024 + 1)
            if len(text) > 128 * 1024:
                raise ComicError("邮件描述文件超过大小限制")
            message = json.loads(text)
            if not isinstance(message, dict) or set(message) != {"subject", "body"}:
                raise ComicError("邮件描述文件仅允许 subject 和 body 两个字段")
            if args.command == "upload":
                upload(api, args.image, message["subject"], message["body"], args.request_id)
            else:
                rid = adopt(api, args.asset, message["subject"], message["body"])
                dispatch(api, rid, message["subject"], message["body"])
            print("上传与发送请求已提交；尚未确认 SMTP 接受或最终投递。禁止重复提交同一请求。")
        elif args.command == "status":
            record, _ = Requests(api).load(request_id(args.request_id))
            if record is None:
                print("请求不存在。")
            else:
                report = {key: record.get(key) for key in (
                    "id", "status", "accepted_at", "run_id", "run_attempt", "cleanup")}
                report["meaning"] = {
                    "uploading": "上传尚未完成，未自动重试",
                    "upload_failed": "上传被拒绝，未提交发送",
                    "upload_uncertain": "上传结果待核验，未提交发送，禁止自动重试",
                    "dispatching": "发送请求待执行或提交结果待核验，禁止重复提交",
                    "claimed": "正在处理或处理结果未保存，禁止重跑",
                    "accepted": "SMTP 已接受，不等于最终投递或已读",
                    "failed": "发送失败或未发送，核验原因后再安排新请求",
                    "uncertain": "发送结果不确定，先核验收件箱，禁止自动重发",
                }[record["status"]]
                print(json.dumps(report, ensure_ascii=False))
        elif args.command == "receive":
            return receive(api)
        else:
            cleanup(api)
    except GitHubAPIError as exc:
        code = exc.status if type(exc.status) is int else "连接或响应错误"
        print(f"漫画邮件操作未获确认（HTTP {code}）；未自动重试，请查询请求状态。", file=sys.stderr)
        return 1
    except ComicError as exc:
        print(f"漫画邮件操作停止：{exc}", file=sys.stderr)
        return 1
    except Exception:
        print("漫画邮件操作停止：配置、文件或内部错误；未显示底层信息，未自动重试。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
