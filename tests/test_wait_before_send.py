import contextlib
import io
import os
import unittest
from unittest.mock import patch

from scripts import wait_before_send


class DelayTests(unittest.TestCase):
    def run_main(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = wait_before_send.main()
        return result, output.getvalue()

    @patch.dict(os.environ, {}, clear=True)
    @patch("scripts.wait_before_send.time.sleep")
    def test_default_sends_immediately_without_sleep(self, sleep):
        result, output = self.run_main()
        self.assertEqual(result, 0)
        self.assertIn("立即发送", output)
        sleep.assert_not_called()

    @patch.dict(os.environ, {"SEND_DELAY": "3 分钟内随机发送"}, clear=True)
    @patch("scripts.wait_before_send.time.sleep")
    def test_random_delay_bounds_and_visible_plan(self, sleep):
        for seconds in (1, 180):
            with self.subTest(seconds=seconds), patch("scripts.wait_before_send.random.randint", return_value=seconds) as random_delay:
                result, output = self.run_main()
                self.assertEqual(result, 0)
                random_delay.assert_called_once_with(1, 180)
                sleep.assert_called_with(seconds)
                self.assertIn(f"等待 {seconds} 秒", output)
                self.assertIn("北京时间", output)

    @patch.dict(os.environ, {"SEND_DELAY": "invalid-sensitive-input"}, clear=True)
    @patch("scripts.wait_before_send.time.sleep")
    def test_invalid_option_fails_without_waiting_or_echoing_value(self, sleep):
        result, output = self.run_main()
        self.assertEqual(result, 1)
        self.assertIn("配置错误", output)
        self.assertNotIn("invalid-sensitive-input", output)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
