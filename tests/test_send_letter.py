import base64
import contextlib
import io
import os
import smtplib
import ssl
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from unittest.mock import patch

from scripts import send_letter, send_mail


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aF1sAAAAASUVORK5CYII="
)


class LetterTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "QQ_EMAIL": "sender@qq.com",
            "MAIL_TO": "recipient@example.com",
            "QQ_SMTP_AUTH_CODE": "private-test-authorization",
            "MAIL_SUBJECT": "这是惊喜标题 🐉",
            "MAIL_BODY": "这是第一行惊喜正文。\n这是第二行中文内容。",
            "MAIL_FROM_NAME": "小龙娘 🐉",
            "GITHUB_ACTIONS": "true",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "images").mkdir()
        self.summary = self.root / "summary.md"
        self.output = self.root / "output"
        self.paths = patch.dict(os.environ, {
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "GITHUB_OUTPUT": str(self.output),
        })
        self.paths.start()
        self.addCleanup(self.paths.stop)
        root = patch("scripts.send_mail.REPOSITORY_ROOT", self.root)
        root.start()
        self.addCleanup(root.stop)

    def run_main(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = send_letter.main()
        log = output.getvalue()
        report = self.summary.read_text(encoding="utf-8") if self.summary.is_file() else ""
        for hidden in ("private-test-authorization", "sender@qq.com", "recipient@example.com",
                       "这是惊喜标题", "这是第一行惊喜正文", "这是第二行中文内容"):
            self.assertNotIn(hidden, log)
            self.assertNotIn(hidden, report)
        return result, log, report

    def add_images(self, count):
        paths = []
        for index in range(count):
            path = f"images/私密配图{index}.png"
            (self.root / path).write_bytes(PNG)
            paths.append(path)
        return paths

    @patch("scripts.send_letter.smtplib.SMTP_SSL")
    def test_utf8_content_from_name_and_envelope_survive_mime_round_trip(self, smtp):
        smtp.return_value.send_message.return_value = {}
        result = send_letter.send()
        self.assertIsInstance(result, str)
        self.assertIsNotNone(send_letter.parse_utc(result))
        message = smtp.return_value.send_message.call_args.args[0]
        parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
        self.assertEqual(parsed["Subject"], "这是惊喜标题 🐉")
        self.assertIn("小龙娘 🐉", str(parsed["From"]))
        self.assertIn("这是第一行惊喜正文。\r\n这是第二行中文内容。", parsed.get_content())
        self.assertEqual(parsed.get_content_charset(), "utf-8")
        self.assertEqual(smtp.return_value.send_message.call_args.kwargs,
                         {"from_addr": "sender@qq.com", "to_addrs": ["recipient@example.com"]})
        args, kwargs = smtp.call_args
        self.assertEqual(args, ("smtp.qq.com", 465))
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(kwargs["context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(kwargs["context"].check_hostname)

    @patch("scripts.send_letter.smtplib.SMTP_SSL")
    def test_actual_acceptance_time_and_summary_hide_surprise_content(self, smtp):
        smtp.return_value.send_message.return_value = {}
        construction = datetime(2026, 12, 31, 15, 59, 59, tzinfo=timezone.utc)
        submission = construction + timedelta(seconds=1)
        acceptance = submission + timedelta(seconds=1, microseconds=123456)
        with patch("scripts.send_letter.utc_now", side_effect=[construction, submission, acceptance]):
            result, _, report = self.run_main()
        self.assertEqual(result, 0)
        message = smtp.return_value.send_message.call_args.args[0]
        date = parsedate_to_datetime(str(message["Date"]))
        self.assertEqual(date, submission)
        self.assertEqual(date.utcoffset(), timedelta(hours=8))
        self.assertIn("SMTP 接受时间", report)
        self.assertIn("2027-01-01 00:00:01", report)
        self.assertIn("无（0 张）", report)
        self.assertIn("接受不等于最终投递", report)
        accepted_at = self.output.read_text().strip().split("=", 1)[1]
        self.assertEqual(send_letter.parse_utc(accepted_at), acceptance)

    @patch("scripts.send_letter.smtplib.SMTP_SSL")
    def test_four_chinese_attachments_and_private_metadata(self, smtp):
        paths = self.add_images(4)
        smtp.return_value.send_message.return_value = {}
        metadata = {}
        with patch.dict(os.environ, {"IMAGE_PATHS": ";".join(paths)}):
            accepted_at = send_letter.send(metadata=metadata)
        self.assertEqual(metadata, {"accepted_at": accepted_at, "attachment_count": 4})
        message = smtp.return_value.send_message.call_args.args[0]
        parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
        attachments = list(parsed.iter_attachments())
        self.assertEqual(len(attachments), 4)
        self.assertEqual([item.get_filename() for item in attachments],
                         [Path(path).name for path in paths])
        self.assertTrue(all(item.get_payload(decode=True) == PNG for item in attachments))
        with patch.dict(os.environ, {"IMAGE_PATHS": ";".join(paths)}):
            result, _, report = self.run_main()
        self.assertEqual(result, 0)
        self.assertIn("有（4 张）", report)
        for path in paths:
            self.assertNotIn(path, report)
            self.assertNotIn(Path(path).name, report)

    @patch("scripts.send_letter.smtplib.SMTP_SSL")
    def test_duplicate_attachment_inputs_do_not_inflate_count(self, smtp):
        paths = self.add_images(2)
        smtp.return_value.send_message.return_value = {}
        with patch.dict(os.environ, {
            "IMAGE_PATH": paths[0], "IMAGE_PATHS": f" ; {paths[0]} ; {paths[1]} ; ;",
        }):
            result, _, report = self.run_main()
        self.assertEqual(result, 0)
        self.assertIn("有（2 张）", report)
        self.assertEqual(len(list(smtp.return_value.send_message.call_args.args[0].iter_attachments())), 2)

    @patch("scripts.send_letter.smtplib.SMTP_SSL")
    def test_more_than_four_images_fail_before_connecting(self, smtp):
        paths = self.add_images(5)
        with patch.dict(os.environ, {"IMAGE_PATHS": ";".join(paths)}):
            result, _, report = self.run_main()
        self.assertEqual(result, 1)
        self.assertIn("最多支持 4 张", report)
        self.assertIn("未完成检查", report)
        self.assertFalse(self.output.exists())
        smtp.assert_not_called()

    @patch("scripts.send_letter.smtplib.SMTP_SSL")
    def test_attachment_path_and_content_failures_never_expose_values(self, smtp):
        outside = self.root / "outside.png"
        outside.write_bytes(PNG)
        (self.root / "images" / "linked.png").symlink_to(outside)
        (self.root / "images" / "private-test-authorization.png").write_text("not an image")
        invalid = ["../outside.png", "images/../outside.png", str(outside),
                   "images/linked.png", "images/missing.png", "images/bad\r\n.png",
                   "images/private-test-authorization.png"]
        for path in invalid:
            with self.subTest(path=path), patch.dict(os.environ, {"IMAGE_PATHS": path}):
                result, log, report = self.run_main()
                self.assertEqual(result, 1)
                self.assertIn("配置错误", log)
                self.assertNotIn(path, log + report)
        smtp.assert_not_called()

    @patch("scripts.send_letter.smtplib.SMTP_SSL")
    def test_oversized_attachment_fails_before_connecting(self, smtp):
        image = self.root / "images" / "large.png"
        image.write_bytes(PNG + b"\0" * (send_mail.MAX_IMAGE_BYTES + 1 - len(PNG)))
        with patch.dict(os.environ, {"IMAGE_PATH": "images/large.png"}):
            result, _, report = self.run_main()
        self.assertEqual(result, 1)
        self.assertIn("5 MiB", report)
        smtp.assert_not_called()

    @patch("scripts.send_letter.smtplib.SMTP_SSL")
    def test_actions_missing_reserved_content_cannot_generate_a_fallback(self, smtp):
        with patch("scripts.dragon_letters.make_letter") as make_letter:
            for missing in ("MAIL_BODY", "MAIL_SUBJECT"):
                with self.subTest(missing=missing), patch.dict(os.environ, {missing: ""}):
                    result, log, report = self.run_main()
                    self.assertEqual(result, 1)
                    self.assertIn("配置错误", log)
                    self.assertIn("发送失败或未发送", report)
            make_letter.assert_not_called()
        smtp.assert_not_called()

    @patch("scripts.send_letter.smtplib.SMTP_SSL")
    def test_explicit_local_preview_preserves_existing_fallback(self, smtp):
        smtp.return_value.send_message.return_value = {}
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false", "MAIL_BODY": "", "MAIL_SUBJECT": ""}), \
                patch("scripts.dragon_letters.make_letter", return_value=("本地预览标题", "本地预览正文")) as make_letter:
            send_letter.send()
        make_letter.assert_called_once_with()
        message = smtp.return_value.send_message.call_args.args[0]
        self.assertEqual(message["Subject"], "本地预览标题")
        self.assertIn("本地预览正文", message.get_content())

    @patch("scripts.send_letter.smtplib.SMTP_SSL")
    def test_header_injection_controls_and_body_limits_fail_before_smtp(self, smtp):
        invalid = [("MAIL_SUBJECT", "private-test-authorization\r\nBcc: other@example.com"),
                   ("MAIL_FROM_NAME", "private-test-authorization\nother"),
                   ("MAIL_FROM_NAME", "bad\x01name"),
                   ("MAIL_SUBJECT", "x" * 121),
                   ("MAIL_BODY", "x" * (send_letter.MAX_BODY_CHARS + 1))]
        for field, value in invalid:
            with self.subTest(field=field), patch.dict(os.environ, {field: value}):
                result, log, _ = self.run_main()
                self.assertEqual(result, 1)
                self.assertIn("配置错误", log)
        smtp.assert_not_called()

    @patch("scripts.send_letter.smtplib.SMTP_SSL")
    def test_smtp_failures_record_evidence_without_server_responses(self, smtp):
        cases = [
            (smtplib.SMTPAuthenticationError(535, b"private-test-authorization"), "认证失败", "发送失败或未发送"),
            (smtplib.SMTPSenderRefused(550, b"private-test-authorization", "sender@qq.com"), "拒绝发件", "发送失败或未发送"),
            (smtplib.SMTPRecipientsRefused({"recipient@example.com": (550, b"private-test-authorization")}), "拒绝收件", "发送失败或未发送"),
            (smtplib.SMTPDataError(554, b"private-test-authorization"), "拒绝邮件正文", "发送失败或未发送"),
            (ssl.SSLError("private-test-authorization"), "TLS", "发送失败或未发送"),
            (smtplib.SMTPServerDisconnected("private-test-authorization"), "连接中断", "发送结果不确定"),
            (TimeoutError("private-test-authorization"), "超时", "发送结果不确定"),
        ]
        for error, explanation, status in cases:
            with self.subTest(error=type(error).__name__):
                smtp.return_value.send_message.side_effect = error
                result, log, report = self.run_main()
                self.assertEqual(result, 1)
                self.assertIn(explanation, log)
                self.assertIn(status, report)
                self.assertIn("结果检查时间", report)
                self.assertIn("无（0 张）", report)
        self.assertFalse(self.output.exists())

    @patch("scripts.send_letter.smtplib.SMTP_SSL")
    def test_refusal_return_is_failure_and_does_not_write_acceptance(self, smtp):
        smtp.return_value.send_message.return_value = {
            "recipient@example.com": (550, b"private-test-authorization")
        }
        result, _, report = self.run_main()
        self.assertEqual(result, 1)
        self.assertIn("拒绝收件", report)
        self.assertFalse(self.output.exists())

    @patch("scripts.send_letter.smtplib.SMTP_SSL")
    def test_all_cleanup_errors_preserve_success_and_write_acceptance(self, smtp):
        smtp.return_value.send_message.return_value = {}
        smtp.return_value.quit.side_effect = smtplib.SMTPServerDisconnected("private-test-authorization")
        smtp.return_value.close.side_effect = OSError("private-test-authorization")
        result, _, report = self.run_main()
        self.assertEqual(result, 0)
        self.assertIn("SMTP 已接受", report)
        self.assertTrue(self.output.read_text().startswith("accepted_at="))
        smtp.return_value.send_message.assert_called_once()
        smtp.return_value.close.assert_called_once()

    @patch("scripts.send_letter.smtplib.SMTP_SSL")
    def test_cleanup_failure_does_not_hide_authentication_error(self, smtp):
        smtp.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"private-test-authorization")
        smtp.return_value.quit.side_effect = smtplib.SMTPServerDisconnected("private-test-authorization")
        smtp.return_value.close.side_effect = OSError("private-test-authorization")
        result, _, report = self.run_main()
        self.assertEqual(result, 1)
        self.assertIn("认证失败", report)
        smtp.return_value.send_message.assert_not_called()

    @patch("scripts.send_letter.smtplib.SMTP_SSL")
    def test_summary_write_failure_cannot_turn_acceptance_into_failure(self, smtp):
        smtp.return_value.send_message.return_value = {}
        with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(self.root)}):
            result, log, _ = self.run_main()
        self.assertEqual(result, 0)
        self.assertIn("摘要保存失败", log)
        self.assertTrue(self.output.read_text().startswith("accepted_at="))
        smtp.return_value.send_message.assert_called_once()

    @patch("scripts.send_letter.smtplib.SMTP_SSL")
    def test_summary_formatting_error_cannot_turn_acceptance_into_failure(self, smtp):
        smtp.return_value.send_message.return_value = {}
        actual_parse = send_letter.parse_utc
        with patch("scripts.send_letter.parse_utc", side_effect=[actual_parse("2026-01-01T00:00:00Z"), ValueError("private-test-authorization")]):
            result, log, _ = self.run_main()
        self.assertEqual(result, 0)
        self.assertIn("摘要保存失败", log)
        self.assertTrue(self.output.read_text().startswith("accepted_at="))

    @patch("scripts.send_letter.smtplib.SMTP_SSL")
    def test_acceptance_output_failure_preserves_mail_result_and_summary(self, smtp):
        smtp.return_value.send_message.return_value = {}
        with patch.dict(os.environ, {"GITHUB_OUTPUT": str(self.root)}):
            result, log, report = self.run_main()
        self.assertEqual(result, 0)
        self.assertIn("接受时间输出保存失败", log)
        self.assertIn("SMTP 已接受", report)
        smtp.return_value.send_message.assert_called_once()

    @patch("scripts.send_letter.smtplib.SMTP_SSL")
    def test_unexpected_reporting_errors_never_leak_values_or_change_success(self, smtp):
        smtp.return_value.send_message.return_value = {}
        with patch("scripts.send_letter.open", side_effect=ValueError("private-test-authorization")):
            result, log, _ = self.run_main()
        self.assertEqual(result, 0)
        self.assertIn("接受时间输出保存失败", log)
        self.assertIn("摘要保存失败", log)
        smtp.return_value.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
