#!/usr/bin/env python3
"""Tests for handoff_worker.py — HTTP polling, WebSocket, helper functions."""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

import handoff_worker


class IsDOQuotaErrorTest(unittest.TestCase):
    def test_none_input(self):
        self.assertFalse(handoff_worker.is_do_quota_error(None))

    def test_empty_string(self):
        self.assertFalse(handoff_worker.is_do_quota_error(""))

    def test_unrelated_error(self):
        self.assertFalse(handoff_worker.is_do_quota_error("connection refused"))

    def test_exceeded_duration(self):
        self.assertTrue(handoff_worker.is_do_quota_error(
            "Worker error: Durable Object exceeded allowed duration"))

    def test_free_tier(self):
        self.assertTrue(handoff_worker.is_do_quota_error(
            "durable objects free tier limit reached"))

    def test_cpu_time_limit(self):
        self.assertTrue(handoff_worker.is_do_quota_error(
            "The script exceeded its cpu time limit"))

    def test_case_insensitive(self):
        self.assertTrue(handoff_worker.is_do_quota_error(
            "EXCEEDED ALLOWED DURATION"))


class PollWorkerTest(unittest.TestCase):
    """Test poll_worker with mocked subprocess."""

    @patch("handoff_worker.load_api_key", return_value="test-key")
    def test_success(self, _mock_key):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            "replies": [{"text": "hello", "create_time": "123"}],
            "takeover": False,
        })

        with patch("subprocess.run", return_value=mock_result):
            result = handoff_worker.poll_worker("https://w.example", "chat1")

        self.assertIsNone(result["error"])
        self.assertEqual(len(result["replies"]), 1)
        self.assertFalse(result["takeover"])

    @patch("handoff_worker.load_api_key", return_value="test-key")
    def test_curl_failure(self, _mock_key):
        mock_result = MagicMock()
        mock_result.returncode = 7
        mock_result.stderr = "Connection refused"

        with patch("subprocess.run", return_value=mock_result):
            result = handoff_worker.poll_worker("https://w.example", "chat1")

        self.assertIn("curl failed", result["error"])
        self.assertEqual(result["replies"], [])

    @patch("handoff_worker.load_api_key", return_value="test-key")
    def test_non_json_response(self, _mock_key):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "<html>502 Bad Gateway</html>"

        with patch("subprocess.run", return_value=mock_result):
            result = handoff_worker.poll_worker("https://w.example", "chat1")

        self.assertIn("non-JSON", result["error"])

    @patch("handoff_worker.load_api_key", return_value="test-key")
    def test_worker_error_in_response(self, _mock_key):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"error": "DO quota exceeded"})

        with patch("subprocess.run", return_value=mock_result):
            result = handoff_worker.poll_worker("https://w.example", "chat1")

        self.assertIn("Worker error", result["error"])

    @patch("handoff_worker.load_api_key", return_value="test-key")
    def test_takeover_signal(self, _mock_key):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            "replies": [],
            "takeover": True,
        })

        with patch("subprocess.run", return_value=mock_result):
            result = handoff_worker.poll_worker("https://w.example", "chat1")

        self.assertTrue(result["takeover"])

    @patch("handoff_worker.load_api_key", return_value="test-key")
    def test_since_param_appended(self, _mock_key):
        captured_args = {}

        def capture_run(cmd, **kwargs):
            captured_args["cmd"] = cmd
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = json.dumps({"replies": []})
            return mock_result

        with patch("subprocess.run", side_effect=capture_run):
            handoff_worker.poll_worker("https://w.example", "chat1", since="999")

        url = captured_args["cmd"][-1]
        self.assertIn("since=999", url)

    @patch("handoff_worker.load_api_key", return_value="test-key")
    def test_custom_key(self, _mock_key):
        captured_args = {}

        def capture_run(cmd, **kwargs):
            captured_args["cmd"] = cmd
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = json.dumps({"replies": []})
            return mock_result

        with patch("subprocess.run", side_effect=capture_run):
            handoff_worker.poll_worker("https://w.example", "chat1", key="nonce:abc")

        url = captured_args["cmd"][-1]
        self.assertIn("/poll/nonce:abc", url)


class CheckDOQuotaStatusTest(unittest.TestCase):
    @patch("handoff_worker.load_api_key", return_value="test-key")
    def test_quota_exhausted(self, _mock_key):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            "do_quota_exhausted": True,
            "exhausted_at": "2025-01-01T00:00:00Z",
        })

        with patch("subprocess.run", return_value=mock_result):
            result = handoff_worker.check_do_quota_status("https://w.example", "chat1")

        self.assertEqual(result, "2025-01-01T00:00:00Z")

    @patch("handoff_worker.load_api_key", return_value="test-key")
    def test_quota_ok(self, _mock_key):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"do_quota_exhausted": False})

        with patch("subprocess.run", return_value=mock_result):
            result = handoff_worker.check_do_quota_status("https://w.example", "chat1")

        self.assertIsNone(result)

    @patch("handoff_worker.load_api_key", return_value="test-key")
    def test_network_error_returns_none(self, _mock_key):
        mock_result = MagicMock()
        mock_result.returncode = 7

        with patch("subprocess.run", return_value=mock_result):
            result = handoff_worker.check_do_quota_status("https://w.example", "chat1")

        self.assertIsNone(result)


class WebSocketProxyDetectionTest(unittest.TestCase):
    """Test _WebSocket._get_http_proxy static method."""

    def test_no_proxy_env(self):
        with patch.dict(os.environ, {}, clear=True):
            host, port = handoff_worker._WebSocket._get_http_proxy("example.com")
        self.assertIsNone(host)
        self.assertIsNone(port)

    def test_https_proxy(self):
        with patch.dict(os.environ, {"https_proxy": "http://proxy.local:8080"}, clear=True):
            host, port = handoff_worker._WebSocket._get_http_proxy("example.com")
        self.assertEqual(host, "proxy.local")
        self.assertEqual(port, 8080)

    def test_no_proxy_wildcard(self):
        with patch.dict(os.environ, {
            "https_proxy": "http://proxy.local:8080",
            "no_proxy": "*",
        }, clear=True):
            host, port = handoff_worker._WebSocket._get_http_proxy("example.com")
        self.assertIsNone(host)

    def test_no_proxy_exact_match(self):
        with patch.dict(os.environ, {
            "https_proxy": "http://proxy.local:8080",
            "no_proxy": "example.com,other.com",
        }, clear=True):
            host, port = handoff_worker._WebSocket._get_http_proxy("example.com")
        self.assertIsNone(host)

    def test_no_proxy_subdomain_match(self):
        with patch.dict(os.environ, {
            "https_proxy": "http://proxy.local:8080",
            "no_proxy": ".example.com",
        }, clear=True):
            host, port = handoff_worker._WebSocket._get_http_proxy("foo.example.com")
        self.assertIsNone(host)

    def test_no_proxy_no_match(self):
        with patch.dict(os.environ, {
            "https_proxy": "http://proxy.local:8080",
            "no_proxy": "other.com",
        }, clear=True):
            host, port = handoff_worker._WebSocket._get_http_proxy("example.com")
        self.assertEqual(host, "proxy.local")


if __name__ == "__main__":
    unittest.main()
