"""Small verified HTTPS GitHub API client with safe errors and no redirects."""

import json
import re
import urllib.error
import urllib.request


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
