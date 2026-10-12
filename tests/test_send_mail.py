import contextlib
import io
import os
import smtplib
import ssl
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from unittest.mock import patch
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

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
            (smtplib.SMTPDataError(554, b"private-test-authorization"), "拒绝邮件正文"),
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

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_all_cleanup_failures_after_acceptance_are_success(self, smtp):
        smtp.return_value.send_message.return_value = {}
        smtp.return_value.quit.side_effect = smtplib.SMTPServerDisconnected("closed")
        smtp.return_value.close.side_effect = OSError("private-test-authorization")
        self.assertEqual(self.run_main()[0], 0)

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_cleanup_failure_preserves_authentication_error(self, smtp):
        smtp.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"private-test-authorization")
        smtp.return_value.quit.side_effect = smtplib.SMTPServerDisconnected("closed")
        smtp.return_value.close.side_effect = OSError("private-test-authorization")
        result, output = self.run_main()
        self.assertEqual(result, 1)
        self.assertIn("认证失败", output)
        smtp.return_value.send_message.assert_not_called()

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_send_stage_errors_are_distinct_and_do_not_leak(self, smtp):
        cases = [
            (smtplib.SMTPDataError(554, b"private-test-authorization"), "拒绝邮件正文"),
            (smtplib.SMTPServerDisconnected("private-test-authorization"), "发送结果可能不确定"),
            (TimeoutError("private-test-authorization"), "重试前请先检查收件箱"),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                smtp.return_value.send_message.side_effect = error
                result, output = self.run_main()
                self.assertEqual(result, 1)
                self.assertIn(expected, output)

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_success_summary_records_message_without_mailbox_or_credentials(self, smtp):
        smtp.return_value.send_message.return_value = {}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.md"
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(path)}):
                self.assertEqual(self.run_main()[0], 0)
            report = path.read_text()
            self.assertIn("SMTP 已接受", report)
            self.assertIn("中文 UTF-8 编码正常", report)
            self.assertIn("北京时间", report)
            for sensitive in ("private-test-authorization", "sender@qq.com", "recipient@example.com"):
                self.assertNotIn(sensitive, report)

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_failure_summary_redacts_server_error(self, smtp):
        smtp.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"private-test-authorization")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.md"
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(path)}):
                self.assertEqual(self.run_main()[0], 1)
            report = path.read_text()
            self.assertIn("认证失败", report)
            self.assertNotIn("private-test-authorization", report)

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_summary_write_failure_does_not_turn_accepted_mail_into_failure(self, smtp):
        smtp.return_value.send_message.return_value = {}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": directory}):
            result, output = self.run_main()
        self.assertEqual(result, 0)
        self.assertIn("摘要保存失败", output)

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_disconnection_summary_marks_uncertainty(self, smtp):
        smtp.return_value.send_message.side_effect = smtplib.SMTPServerDisconnected("private-test-authorization")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.md"
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(path)}):
                self.assertEqual(self.run_main()[0], 1)
            report = path.read_text()
            self.assertIn("结果不确定", report)
            self.assertNotIn("private-test-authorization", report)

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_date_is_refreshed_after_login_and_acceptance_time_is_actual(self, smtp):
        smtp.return_value.send_message.return_value = {}
        construction = datetime(2026, 12, 31, 15, 59, 59, tzinfo=timezone.utc)
        submission = construction + timedelta(seconds=1)
        acceptance = submission + timedelta(seconds=1, microseconds=123456)
        with patch("scripts.send_mail.utc_now", side_effect=[construction, submission, acceptance]):
            result = send_mail.send()
        message = smtp.return_value.send_message.call_args.args[0]
        date = parsedate_to_datetime(str(message['Date']))
        self.assertEqual(date, submission)
        self.assertEqual(date.utcoffset(), timedelta(hours=8))
        self.assertEqual(date.strftime('%Y-%m-%d %H:%M:%S'), '2027-01-01 00:00:00')
        self.assertEqual(send_mail.parse_utc(result['accepted_at']), acceptance)

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_smtp_time_output_preserves_success_and_does_not_expose_credentials(self, smtp):
        smtp.return_value.send_message.return_value = {}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'output'
            with patch.dict(os.environ, {'GITHUB_OUTPUT': str(path)}):
                self.assertEqual(self.run_main()[0], 0)
            output = path.read_text()
            self.assertTrue(output.startswith('accepted_at='))
            self.assertIsNotNone(send_mail.parse_utc(output.strip().split('=', 1)[1]))
            self.assertNotIn('private-test-authorization', output)

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_time_output_failure_does_not_change_mail_acceptance(self, smtp):
        smtp.return_value.send_message.return_value = {}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'GITHUB_OUTPUT': directory}):
            result, output = self.run_main()
        self.assertEqual(result, 0)
        self.assertIn('发送时间输出保存失败', output)


if __name__ == "__main__":
    unittest.main()
