import base64
import contextlib
import io
import os
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

from scripts import send_mail


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aF1sAAAAASUVORK5CYII="
)


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "images").mkdir()
        self.env = patch.dict(os.environ, {
            "QQ_EMAIL": "sender@qq.com", "MAIL_TO": "recipient@example.com",
            "QQ_SMTP_AUTH_CODE": "private-test-authorization", "MESSAGE_TYPE": "test",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        root = patch("scripts.send_mail.REPOSITORY_ROOT", self.root)
        root.start()
        self.addCleanup(root.stop)

    def message(self):
        return send_mail.build_message("sender@qq.com", "recipient@example.com", "test")

    def test_chinese_filename_and_image_bytes_survive_mime_round_trip(self):
        (self.root / "images" / "风景.png").write_bytes(PNG)
        message = self.message()
        send_mail.add_image_attachment(message, "images/风景.png")
        parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
        attachments = list(parsed.iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_filename(), "风景.png")
        self.assertEqual(attachments[0].get_content_type(), "image/png")
        self.assertEqual(attachments[0].get_content_disposition(), "attachment")
        self.assertEqual(attachments[0].get_payload(decode=True), PNG)
        self.assertIn("中文 UTF-8 编码正常", parsed.get_body(preferencelist=("plain",)).get_content())

    def test_empty_path_preserves_plain_text(self):
        message = self.message()
        send_mail.add_image_attachment(message, "")
        self.assertFalse(message.is_multipart())
        self.assertEqual(message.get_content_type(), "text/plain")

    def test_formats_are_detected_from_file_headers(self):
        # Minimal format signatures exercise MIME selection independently of suffixes.
        formats = [(PNG, "png"), (b"\xff\xd8\xff\xe0test", "jpeg"),
                   (b"GIF89atest", "gif"), (b"RIFF\x00\x00\x00\x00WEBPtest", "webp")]
        for data, subtype in formats:
            with self.subTest(subtype=subtype):
                (self.root / "images" / "picture.bin").write_bytes(data)
                message = self.message()
                send_mail.add_image_attachment(message, "images/picture.bin")
                attachment = next(message.iter_attachments())
                self.assertEqual(attachment.get_content_type(), f"image/{subtype}")
                self.assertEqual(attachment.get_payload(decode=True), data)

    def test_paths_outside_images_and_invalid_characters_are_rejected(self):
        cases = [str(self.root / "images" / "photo.png"), "../photo.png",
                 "images/../photo.png", "scripts/send_mail.py", "images/bad\r\n.png",
                 "images/bad\0.png"]
        for path in cases:
            with self.subTest(path=path), self.assertRaises(send_mail.ConfigurationError):
                send_mail.add_image_attachment(self.message(), path)

    def test_symlinks_and_directories_do_not_escape_attachment_boundary(self):
        outside = self.root / "outside.png"
        outside.write_bytes(PNG)
        (self.root / "images" / "outside.png").symlink_to(outside)
        (self.root / "images" / "folder").mkdir()
        (self.root / "images" / "loop.png").symlink_to("loop.png")
        for path in ("images/outside.png", "images/folder", "images/loop.png"):
            with self.subTest(path=path), self.assertRaises(send_mail.ConfigurationError):
                send_mail.add_image_attachment(self.message(), path)
        alternate_root = self.root / "alternate"
        alternate_root.mkdir()
        (alternate_root / "images").symlink_to(self.root / "images", target_is_directory=True)
        (self.root / "images" / "photo.png").write_bytes(PNG)
        with self.assertRaises(send_mail.ConfigurationError):
            send_mail.add_image_attachment(self.message(), "images/photo.png", alternate_root)

    def test_size_limit_accepts_boundary_and_rejects_larger_files(self):
        file = self.root / "images" / "photo.png"
        file.write_bytes(PNG + b"\0" * (send_mail.MAX_IMAGE_BYTES - len(PNG)))
        message = self.message()
        send_mail.add_image_attachment(message, "images/photo.png")
        self.assertEqual(len(next(message.iter_attachments()).get_payload(decode=True)), send_mail.MAX_IMAGE_BYTES)
        with file.open("ab") as data:
            data.write(b"\0")
        with self.assertRaisesRegex(send_mail.ConfigurationError, "5 MiB"):
            send_mail.add_image_attachment(self.message(), "images/photo.png")

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_invalid_images_fail_before_connecting_and_do_not_log_paths(self, smtp):
        (self.root / "images" / "private-test-authorization.png").write_text("not an image")
        for path in ("images/missing.png", "images/private-test-authorization.png"):
            with self.subTest(path=path), patch.dict(os.environ, {"IMAGE_PATH": path}):
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    self.assertEqual(send_mail.main(), 1)
                self.assertIn("配置错误", output.getvalue())
                self.assertNotIn(path, output.getvalue())
                self.assertNotIn("private-test-authorization", output.getvalue())
        smtp.assert_not_called()

    @patch("scripts.send_mail.smtplib.SMTP_SSL")
    def test_smtp_receives_text_and_attachment_together(self, smtp):
        (self.root / "images" / "photo.png").write_bytes(PNG)
        smtp.return_value.send_message.return_value = {}
        with patch.dict(os.environ, {"IMAGE_PATH": "images/photo.png"}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(send_mail.main(), 0)
        message = smtp.return_value.send_message.call_args.args[0]
        self.assertEqual(next(message.iter_attachments()).get_payload(decode=True), PNG)
        self.assertIn("中文 UTF-8 编码正常", message.get_body(preferencelist=("plain",)).get_content())


if __name__ == "__main__":
    unittest.main()
