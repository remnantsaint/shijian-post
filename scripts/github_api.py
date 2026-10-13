"""Small verified HTTPS GitHub API client with safe errors and no redirects."""

import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit


class GitHubAPIError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class GitHubAPI:
    def __init__(self, repo, token):
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
                or any(part in (".", "..") for part in repo.split("/"))):
            raise GitHubAPIError("GitHub 仓库名称配置错误")
        if not token:
            raise GitHubAPIError("缺少 GitHub API 认证配置")
        self.repo = repo
        self._token = token
        self._opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, data=None):
        if not path.startswith("/") or path.startswith("//") or "\r" in path or "\n" in path:
            raise GitHubAPIError("GitHub API 请求路径配置错误")
        try:
            payload = None if data is None else json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            raise GitHubAPIError("GitHub API 请求参数编码失败") from None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mail-bot",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            request = urllib.request.Request("https://api.github.com" + path, data=payload, headers=headers, method=method)
            with self._opener.open(request, timeout=15) as response:
                content = response.read(4 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise GitHubAPIError(f"GitHub API 请求失败（HTTP {exc.code}）", status=exc.code) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise GitHubAPIError("GitHub API 连接失败，请检查网络和权限") from None
        if len(content) > 4 * 1024 * 1024:
            raise GitHubAPIError("GitHub API 响应超过读取限制")
        if not content:
            return None
        try:
            return json.loads(content)
        except (ValueError, UnicodeError):
            raise GitHubAPIError("GitHub API 返回的数据格式错误") from None

    def upload_release_asset(self, release_id, name, mime, data):
        if (type(release_id) is not int or release_id < 1
                or not re.fullmatch(r"mailbot-[0-9a-f-]{36}\.(png|jpg|webp)", name)
                or mime not in {"image/png", "image/jpeg", "image/webp"}
                or not isinstance(data, bytes) or not 0 < len(data) <= 5 * 1024 * 1024):
            raise GitHubAPIError("图片上传参数无效")
        url = (f"https://uploads.github.com/repos/{self.repo}/releases/{release_id}/assets?"
               + urlencode({"name": name}))
        request = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": f"Bearer {self._token}", "Content-Type": mime,
            "Accept": "application/vnd.github+json", "User-Agent": "mail-bot",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with self._opener.open(request, timeout=60) as response:
                content = response.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise ValueError
            return json.loads(content)
        except urllib.error.HTTPError as exc:
            raise GitHubAPIError(f"图片上传未获确认（HTTP {exc.code}）", status=exc.code) from None
        except (urllib.error.URLError, OSError, ValueError, UnicodeError):
            raise GitHubAPIError("图片上传连接失败或响应无效；不能自动重试") from None

    def download_release_asset(self, asset_id, max_bytes=5 * 1024 * 1024):
        if type(asset_id) is not int or asset_id < 1 or not 0 < max_bytes <= 5 * 1024 * 1024:
            raise GitHubAPIError("图片下载参数无效")
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}/releases/assets/{asset_id}",
            headers={"Authorization": f"Bearer {self._token}",
                     "Accept": "application/octet-stream", "User-Agent": "mail-bot"})
        try:
            try:
                response = self._opener.open(request, timeout=60)
            except urllib.error.HTTPError as exc:
                if exc.code not in (301, 302, 303, 307, 308):
                    raise
                location = exc.headers.get("Location", "")
                exc.close()
                target = urlsplit(location)
                if (target.scheme != "https" or target.hostname != "release-assets.githubusercontent.com"
                        or target.username or target.password or target.port not in (None, 443)
                        or target.fragment):
                    raise GitHubAPIError("图片下载重定向目标不受信任；已阻止发送")
                # Signed GitHub asset URLs never receive the repository token.
                response = self._opener.open(urllib.request.Request(
                    location, headers={"User-Agent": "mail-bot"}), timeout=60)
            with response:
                content = response.read(max_bytes + 1)
            if not content or len(content) > max_bytes:
                raise GitHubAPIError("图片附件为空或超过大小限制")
            return content
        except urllib.error.HTTPError as exc:
            raise GitHubAPIError(f"图片下载失败（HTTP {exc.code}）", status=exc.code) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise GitHubAPIError("图片下载连接失败或响应无效") from None
