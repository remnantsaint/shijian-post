import urllib.error
import unittest
from unittest.mock import MagicMock

from scripts.github_api import GitHubAPI, GitHubAPIError, NoRedirect


class APITests(unittest.TestCase):
    def setUp(self):
        self.api = GitHubAPI("owner/repository", "private-api-token")
        self.api._opener = MagicMock()

    def test_json_request_uses_only_github_https_and_auth_header(self):
        self.api._opener.open.return_value.__enter__.return_value.read.return_value = b'{"state":"active"}'
        self.assertEqual(self.api.request("GET", "/repos/owner/repository/actions/workflows/main.yml"), {"state": "active"})
        request = self.api._opener.open.call_args.args[0]
        self.assertEqual(request.host, "api.github.com")
        self.assertEqual(request.type, "https")
        self.assertEqual(request.get_header("Authorization"), "Bearer private-api-token")
        self.assertEqual(self.api._opener.open.call_args.kwargs["timeout"], 15)

    def test_http_errors_are_classified_without_echoing_server_response(self):
        for code in (302, 401, 404, 409):
            with self.subTest(code=code):
                self.api._opener.open.side_effect = urllib.error.HTTPError(
                    "https://api.github.com", code, "private-api-token", {}, None)
                with self.assertRaises(GitHubAPIError) as caught:
                    self.api.request("GET", "/repos/owner/repository")
                self.assertEqual(caught.exception.status, code)
                self.assertNotIn("private-api-token", str(caught.exception))

    def test_redirect_handler_does_not_forward_token_to_another_host(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://other-host.example"))

    def test_invalid_response_is_not_echoed(self):
        self.api._opener.open.return_value.__enter__.return_value.read.return_value = b"private-api-token"
        with self.assertRaises(GitHubAPIError) as caught:
            self.api.request("GET", "/repos/owner/repository")
        self.assertNotIn("private-api-token", str(caught.exception))

    def test_external_paths_are_rejected_without_network_calls(self):
        for path in ("https://other-host.example", "//other-host.example/secret", "/repos/bad\r\nheader"):
            with self.subTest(path=path), self.assertRaises(GitHubAPIError):
                self.api.request("GET", path)
        self.api._opener.open.assert_not_called()
