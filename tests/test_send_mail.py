import contextlib
import io
import os
import smtplib
import ssl
import unittest
from email import policy
from email.parser import BytesParser
from unittest.mock import patch

from scripts import send_mail


class MailTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "QQ_EMAIL": "sender@qq.com", "MAIL_TO": "recipient@example.com",
            "QQ_SMTP_AUTH_CODE": "private-test-authorization", "MESSAGE_TYPE": "test",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_main(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = send_mail.main()
        text = output.getvalue()
        for sensitive in ("private-test-authorization", "sender@qq.com", "recipient@example.com"):
            self.assertNotIn(sensitive, text)
        return result, text

    def test_utf8_round_trip(self):
        for kind in ("test", "random"):
            message = send_mail.build_message("sender@qq.com", "recipient@example.com", kind)
            parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
            self.assertEqual(parsed["Subject"], message["Subject"])
            self.assertEqual(parsed.get_content(), message.get_content().replace("\n", "\r\n"))
            self.assertEqual(parsed.get_content_charset(), "utf-8")

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_success_uses_verified_ssl_and_envelope(self, smtp):
        smtp.return_value.send_message.return_value = {}
        result, output = self.run_main()
        self.assertEqual(result, 0)
        self.assertIn("已接受", output)
        args, kwargs = smtp.call_args
        self.assertEqual(args, ("smtp.qq.com", 465))
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(kwargs["context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(kwargs["context"].check_hostname)
        smtp.return_value.login.assert_called_once_with("sender@qq.com", "private-test-authorization")
        self.assertEqual(smtp.return_value.send_message.call_args.kwargs,
                         {"from_addr": "sender@qq.com", "to_addrs": ["recipient@example.com"]})
        smtp.return_value.quit.assert_called_once()

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_failure_messages_do_not_echo_server_response(self, smtp):
        cases = [
            (smtplib.SMTPAuthenticationError(535, b"private-test-authorization"), "认证失败"),
            (smtplib.SMTPRecipientsRefused({"recipient@example.com": (550, b"private-test-authorization")}), "拒绝收件"),
            (smtplib.SMTPDataError(554, b"private-test-authorization"), "服务器拒绝"),
            (TimeoutError("private-test-authorization"), "超时"),
            (ssl.SSLError("private-test-authorization"), "TLS"),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                smtp.return_value.login.side_effect = error
                result, output = self.run_main()
                self.assertEqual(result, 1)
                self.assertIn(expected, output)

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_configuration_errors_do_not_connect(self, smtp):
        for name, value in [("QQ_SMTP_AUTH_CODE", ""), ("MAIL_TO", "bad\r\nBcc: other@example.com"),
                            ("MESSAGE_TYPE", "invalid")]:
            with self.subTest(name=name), patch.dict(os.environ, {name: value}):
                result, output = self.run_main()
                self.assertEqual(result, 1)
                self.assertIn("配置错误", output)
        smtp.assert_not_called()

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_quit_failure_after_acceptance_is_success(self, smtp):
        smtp.return_value.send_message.return_value = {}
        smtp.return_value.quit.side_effect = smtplib.SMTPServerDisconnected("closed")
        self.assertEqual(self.run_main()[0], 0)
        smtp.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
