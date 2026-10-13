"""Shared state-branch creation, independent of any scheduling policy."""
from urllib.parse import quote
from .github_api import GitHubAPIError

STATE_BRANCH = "mail-bot-state"

class StateBranch:
    def __init__(self, api):
        self.api = api
        self.prefix = f"/repos/{api.repo}"

    def ensure_branch(self):
        try:
            self.api.request("GET", f"{self.prefix}/git/ref/heads/{STATE_BRANCH}")
            return
        except GitHubAPIError as exc:
            if exc.status != 404:
                raise
        repository = self.api.request("GET", self.prefix)
        default_branch = quote(repository["default_branch"], safe="")
        reference = self.api.request("GET", f"{self.prefix}/git/ref/heads/{default_branch}")
        try:
            self.api.request("POST", f"{self.prefix}/git/refs", {
                "ref": f"refs/heads/{STATE_BRANCH}",
                "sha": reference["object"]["sha"],
            })
        except GitHubAPIError as exc:
            # A concurrent creator is harmless; verify the branch really exists.
            if exc.status != 422:
                raise
            self.api.request("GET", f"{self.prefix}/git/ref/heads/{STATE_BRANCH}")

