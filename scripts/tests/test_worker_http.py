#!/usr/bin/env python3

import json
import os
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

import worker_http  # type: ignore


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class WorkerHttpTest(unittest.TestCase):
    def test_build_worker_headers(self):
        h = worker_http.build_worker_headers("k123")
        self.assertEqual(h["User-Agent"], "curl/8.0")
        self.assertEqual(h["Accept"], "*/*")
        self.assertEqual(h["Authorization"], "Bearer k123")

    def test_poll_worker_success(self):
        real_open = worker_http._opener.open

        def fake_open(req, timeout=0):
            body = json.dumps({"replies": [{"text": "yes"}], "takeover": True}).encode()
            return _Resp(body)

        worker_http._opener.open = fake_open
        try:
            result = worker_http.poll_worker_urllib(
                "https://worker.example", "chat-1", since="0", timeout=3, api_key="k"
            )
        finally:
            worker_http._opener.open = real_open

        self.assertIsNone(result["error"])
        self.assertTrue(result["takeover"])
        self.assertEqual(len(result["replies"]), 1)

    def test_poll_worker_http_error(self):
        real_open = worker_http._opener.open
        real_http_error = worker_http.urllib.error.HTTPError

        class DummyHTTPError(Exception):
            def __init__(self, code):
                self.code = code

        def fake_open(req, timeout=0):
            raise DummyHTTPError(403)

        worker_http._opener.open = fake_open
        worker_http.urllib.error.HTTPError = DummyHTTPError
        try:
            result = worker_http.poll_worker_urllib(
                "https://worker.example", "chat-2", api_key="k"
            )
        finally:
            worker_http._opener.open = real_open
            worker_http.urllib.error.HTTPError = real_http_error

        self.assertEqual(result["error"], "HTTP 403")

    def test_ack_worker_adds_auth_header(self):
        real_open = worker_http._opener.open
        captured = {"auth": ""}

        def fake_open(req, timeout=0):
            for k, v in req.header_items():
                if k.lower() == "authorization":
                    captured["auth"] = v
            return _Resp(b"{}")

        worker_http._opener.open = fake_open
        try:
            ok = worker_http.ack_worker_urllib(
                "https://worker.example",
                "chat-3",
                "999",
                api_key="k999",
            )
        finally:
            worker_http._opener.open = real_open

        self.assertTrue(ok)
        self.assertEqual(captured["auth"], "Bearer k999")


if __name__ == "__main__":
    unittest.main()
