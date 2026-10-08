import unittest
from unittest.mock import patch

import requests

import ufirst_watcher


class TelegramTests(unittest.TestCase):
    def test_telegram_skips_when_missing_credentials(self):
        with patch.object(ufirst_watcher, "TELEGRAM_TOKEN", ""), patch.object(ufirst_watcher, "TELEGRAM_CHAT_ID", ""), patch("ufirst_watcher.requests.post") as mock_post:
            sent = ufirst_watcher.telegram("hello")
        self.assertFalse(sent)
        mock_post.assert_not_called()

    def test_telegram_returns_false_when_request_fails(self):
        with patch.object(ufirst_watcher, "TELEGRAM_TOKEN", "token"), patch.object(ufirst_watcher, "TELEGRAM_CHAT_ID", "chat"), patch("ufirst_watcher.requests.post", side_effect=requests.RequestException("boom")):
            sent = ufirst_watcher.telegram("hello")
        self.assertFalse(sent)


if __name__ == "__main__":
    unittest.main()
