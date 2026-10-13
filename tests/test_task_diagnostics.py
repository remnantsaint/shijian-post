import contextlib
import io
import os
import unittest
from unittest.mock import patch

from scripts import task_runner, send_mail
from scripts.comic_mail import ComicError


class TaskDiagnosticsTests(unittest.TestCase):
    def report(self, error):
        output = io.StringIO()
        with patch.object(task_runner, 'api_from_environment', return_value=object()), \
                patch.object(task_runner, 'prepare', side_effect=error) as prepare, \
                patch.dict(os.environ, {'GITHUB_EVENT_NAME':'workflow_dispatch'}), \
                contextlib.redirect_stderr(output):
            self.assertEqual(task_runner.main(['prepare']), 1)
        prepare.assert_called_once()
        return output.getvalue()

    def test_comic_validation_is_visible(self):
        self.assertIn('草稿附件扩展名、类型或大小不一致', self.report(
            ComicError('草稿附件扩展名、类型或大小不一致')))

    def test_mail_validation_is_visible(self):
        self.assertIn('自定义邮件正文不能为空', self.report(
            send_mail.ConfigurationError('自定义邮件正文不能为空')))

    def test_unexpected_exception_never_exposes_message(self):
        for error in (RuntimeError('private-token'), KeyError('private-body'),
                      ValueError('private-url'), OSError('private-path')):
            with self.subTest(error=type(error).__name__):
                result = self.report(error)
                self.assertNotIn('private-', result)
                self.assertIn('禁止自动重试', result)
