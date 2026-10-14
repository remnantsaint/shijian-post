import contextlib
import io
import json
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
    @patch('scripts.send_mail.smtplib.SMTP_SSL')
    def test_deadline_crossed_during_login_never_submits_data(self,smtp):
        from scripts.time_utils import DeadlineExpired
        start=datetime(2026,10,20,12,0,tzinfo=timezone.utc)
        with patch.dict(os.environ,{'MAIL_DEADLINE':(start+timedelta(seconds=10)).isoformat()}), \
                patch('scripts.send_mail.utc_now',side_effect=[start,start+timedelta(seconds=20)]):
            with self.assertRaises(DeadlineExpired):send_mail.send()
        smtp.return_value.send_message.assert_not_called()
        smtp.return_value.quit.assert_called_once()
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

    def test_custom_message_preserves_utf8_body_and_default_subject(self):
        body = "我在这里\n  第二行保留空格。\n"
        with patch.dict(os.environ, {"MAIL_BODY": body}):
            message = send_mail.build_message("sender@qq.com", "recipient@example.com", "custom")
        parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
        self.assertEqual(parsed["Subject"], "来自邮件机器人的消息")
        self.assertEqual(parsed.get_content().split("\r\n\r\n写信时间", 1)[0],
                         body.replace("\n", "\r\n"))
        self.assertIn("写信时间（北京时间 UTC+8）：", parsed.get_content())
        self.assertEqual(parsed.get_content_charset(), "utf-8")

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_custom_message_success_uses_requested_subject_and_records_summary(self, smtp):
        smtp.return_value.send_message.return_value = {}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.md"
            with patch.dict(os.environ, {"MESSAGE_TYPE": "custom", "MAIL_BODY": "我在这里",
                                         "MAIL_SUBJECT": "中文消息：你好！", "GITHUB_STEP_SUMMARY": str(path)}):
                result, output = self.run_main()
            report = path.read_text(encoding="utf-8")
        self.assertEqual(result, 0)
        self.assertNotIn("我在这里", output)
        message = smtp.return_value.send_message.call_args.args[0]
        self.assertEqual(message["Subject"], "中文消息：你好！")
        self.assertEqual(message.get_content().split("\n\n写信时间", 1)[0], "我在这里")
        self.assertIn("自定义邮件", report)
        self.assertNotIn("我在这里", report)
        self.assertNotIn("中文消息：你好！", report)

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_custom_invalid_subject_rejects_before_smtp_without_echoing_payload(self, smtp):
        for subject in ("private-payload\r\nBcc: other@example.com", "private-payload\0",
                        "private-payload\u2028second line", "private-payload" + "文" * 200):
            environment = dict(os.environ, MESSAGE_TYPE="custom", MAIL_SUBJECT=subject, MAIL_BODY="private-body")
            with self.subTest(subject=subject), patch("scripts.send_mail.os.environ", environment):
                result, output = self.run_main()
            self.assertEqual(result, 1)
            self.assertIn("配置错误", output)
            self.assertNotIn("private-payload", output)
            self.assertNotIn("private-body", output)
        smtp.assert_not_called()

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_custom_invalid_body_rejects_before_smtp_without_echoing_payload(self, smtp):
        for body in (" \n\t", "private-body\0", "private-body" + "文" * 100000,
                     "private-body\ud800"):
            environment = dict(os.environ, MESSAGE_TYPE="custom", MAIL_SUBJECT="private-title", MAIL_BODY=body)
            with self.subTest(body_length=len(body)), patch("scripts.send_mail.os.environ", environment):
                result, output = self.run_main()
            self.assertEqual(result, 1)
            self.assertIn("配置错误", output)
            self.assertNotIn("private-body", output)
            self.assertNotIn("private-title", output)
        smtp.assert_not_called()

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_blank_workflow_body_uses_prepared_letter(self, smtp):
        smtp.return_value.send_message.return_value = {}
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            event.write_text(json.dumps({"inputs":{"custom_subject":"","custom_body":""}}),encoding="utf-8")
            with patch.dict(os.environ, {"MESSAGE_TYPE":"custom","GITHUB_ACTIONS":"true",
                                         "GITHUB_EVENT_PATH":str(event)}):
                result, output = self.run_main()
        self.assertEqual(result,0)
        from scripts.dragon_letters import prepared_letter
        title, body = prepared_letter()
        sent = smtp.return_value.send_message.call_args.args[0]
        self.assertEqual(sent["Subject"],title)
        self.assertIn(body,sent.get_content())

    def test_custom_body_accepts_size_limit_and_empty_subject_uses_default(self):
        with patch.dict(os.environ, {"MAIL_BODY": "文" * 100000, "MAIL_SUBJECT": "   "}):
            message = send_mail.build_message("sender@qq.com", "recipient@example.com", "custom")
        self.assertEqual(message["Subject"], "来自邮件机器人的消息")
        self.assertEqual(len(message.get_content().replace("\r\n", "\n").split(
            "\n\n写信时间", 1)[0]), 100000)

    def test_custom_fields_take_precedence_and_do_not_override_reserved_random_letter(self):
        with patch.dict(os.environ, {
                "MAIL_SUBJECT": "已预留的随机主题", "MAIL_BODY": "已预留的随机正文",
                "CUSTOM_MAIL_SUBJECT": "指定主题", "CUSTOM_MAIL_BODY": "我在这里"}):
            custom = send_mail.build_message("sender@qq.com", "recipient@example.com", "custom")
            random_message = send_mail.build_message("sender@qq.com", "recipient@example.com", "random")
        self.assertEqual(custom["Subject"], "指定主题")
        self.assertEqual(custom.get_content().split("\n\n写信时间", 1)[0], "我在这里")
        self.assertEqual(random_message["Subject"], "已预留的随机主题")
        self.assertEqual(random_message.get_content().split("\n\n写信时间", 1)[0], "已预留的随机正文")

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_actions_custom_inputs_read_from_event_and_masked_without_summary_content(self, smtp):
        smtp.return_value.send_message.return_value = {}
        body = "私密正文 100%\n::warning::第二行"
        subject = "私密主题"
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            summary = Path(directory) / "summary.md"
            event.write_text(json.dumps({"inputs": {"custom_subject": subject,
                                                    "custom_body": body}}), encoding="utf-8")
            with patch.dict(os.environ, {"MESSAGE_TYPE": "custom", "GITHUB_ACTIONS": "true",
                                         "GITHUB_EVENT_PATH": str(event), "GITHUB_STEP_SUMMARY": str(summary),
                                         "CUSTOM_MAIL_BODY": "must not use step env"}):
                result, output = self.run_main()
            report = summary.read_text(encoding="utf-8")
        self.assertEqual(result, 0)
        self.assertEqual(smtp.return_value.send_message.call_args.args[0].get_content().split(
            "\n\n写信时间", 1)[0], body)
        self.assertIn("::add-mask::" + subject, output)
        self.assertIn("::add-mask::私密正文 100%25%0A::warning::第二行", output)
        self.assertNotIn("\n::warning::", output)
        self.assertNotIn(body, report)
        self.assertNotIn(subject, report)

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_actions_invalid_event_fails_closed_without_environment_fallback(self, smtp):
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            for payload in ("not json private-marker", '{}', '{"inputs":null}',
                            '{"inputs":{"custom_body":["private-marker"]}}'):
                event.write_text(payload, encoding="utf-8")
                with patch.dict(os.environ, {"MESSAGE_TYPE": "custom", "GITHUB_ACTIONS": "true",
                                             "GITHUB_EVENT_PATH": str(event), "MAIL_BODY": "fallback"}):
                    result, output = self.run_main()
                self.assertEqual(result, 1)
                self.assertNotIn("private-marker", output)
        smtp.assert_not_called()

    def test_success_summary_never_includes_private_content_for_any_message_type(self):
        for kind in ("test", "random", "custom"):
            with tempfile.TemporaryDirectory() as directory:
                summary = Path(directory) / "summary.md"
                with patch.dict(os.environ, {"MESSAGE_TYPE": kind, "GITHUB_STEP_SUMMARY": str(summary)}):
                    send_mail.append_send_summary("accepted", {
                        "subject": "private-title", "body": "private-body sender@qq.com",
                        "has_image": True, "accepted_at": "2026-10-13T00:00:00Z"})
                report = summary.read_text(encoding="utf-8")
            self.assertIn("SMTP 已接受", report)
            self.assertIn("北京时间", report)
            for sensitive in ("private-title", "private-body", "sender@qq.com"):
                self.assertNotIn(sensitive, report)

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_explicit_empty_custom_body_does_not_fall_back_to_reserved_random_content(self, smtp):
        with patch.dict(os.environ, {"MESSAGE_TYPE": "custom", "MAIL_BODY": "已预留的随机正文",
                                     "CUSTOM_MAIL_BODY": "", "CUSTOM_MAIL_SUBJECT": ""}):
            result, output = self.run_main()
        self.assertEqual(result, 1)
        self.assertIn("正文不能为空", output)
        smtp.assert_not_called()

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_custom_failure_summary_does_not_echo_requested_content(self, smtp):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.md"
            with patch.dict(os.environ, {"MESSAGE_TYPE": "custom", "MAIL_BODY": "private-body",
                                         "MAIL_SUBJECT": "private-title\nInjected", "GITHUB_STEP_SUMMARY": str(path)}):
                self.assertEqual(self.run_main()[0], 1)
            report = path.read_text(encoding="utf-8")
        self.assertIn("配置错误", report)
        self.assertNotIn("private-title", report)
        self.assertNotIn("private-body", report)
        smtp.assert_not_called()

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
            self.assertNotIn("中文 UTF-8 编码正常", report)
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
        self.assertTrue(message.get_content().endswith(
            '写信时间（北京时间 UTC+8）：2026-12-31 23:59:59\n'))

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
