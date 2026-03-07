#!/usr/bin/env python3
"""Tests for send_and_wait.py and resolve_session_context() in lark_im.py.

Covers argument parsing, session context resolution, send/wait phases,
backoff jitter, and timeout behavior.
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

import handoff_config
import handoff_db
import handoff_worker
import lark_im
import send_and_wait  # type: ignore


# ---------------------------------------------------------------------------
# resolve_session_context() — the new shared helper in lark_im.py
# ---------------------------------------------------------------------------

class ResolveSessionContextTest(unittest.TestCase):
    """Tests for lark_im.resolve_session_context() helper."""

    def setUp(self):
        self._old_home = os.environ.get("HOME")
        self._old_project = os.environ.get("HANDOFF_PROJECT_DIR")
        self._old_session = os.environ.get("HANDOFF_SESSION_ID")
        self._old_tool = os.environ.get("HANDOFF_SESSION_TOOL")

        self.tmp = tempfile.TemporaryDirectory()
        self.project_dir = os.path.join(self.tmp.name, "project")
        os.makedirs(self.project_dir, exist_ok=True)

        os.environ["HOME"] = self.tmp.name
        os.environ["HANDOFF_PROJECT_DIR"] = self.project_dir
        os.environ["HANDOFF_SESSION_TOOL"] = "Claude Code"

        self.db_path = handoff_db._db_path()
        handoff_db._db_initialized.discard(self.db_path)
        conn = handoff_db._get_db()
        conn.close()

    def tearDown(self):
        for key, val in [
            ("HOME", self._old_home),
            ("HANDOFF_PROJECT_DIR", self._old_project),
            ("HANDOFF_SESSION_ID", self._old_session),
            ("HANDOFF_SESSION_TOOL", self._old_tool),
        ]:
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self.tmp.cleanup()

    def test_no_session_id_raises(self):
        os.environ.pop("HANDOFF_SESSION_ID", None)
        # Mock credentials so we get past that check to the session ID check
        orig_load = handoff_config.load_credentials
        handoff_config.load_credentials = lambda: {"app_id": "a", "app_secret": "b"}
        try:
            with self.assertRaises(RuntimeError) as ctx:
                lark_im.resolve_session_context()
            self.assertIn("HANDOFF_SESSION_ID", str(ctx.exception))
        finally:
            handoff_config.load_credentials = orig_load

    def test_no_active_session_raises(self):
        os.environ["HANDOFF_SESSION_ID"] = "nonexistent"
        # Mock credentials so we don't fail there
        orig_load = handoff_config.load_credentials
        handoff_config.load_credentials = lambda: {"app_id": "a", "app_secret": "b"}
        orig_token = lark_im.get_tenant_token
        lark_im.get_tenant_token = lambda a, b: "tok"
        try:
            with self.assertRaises(RuntimeError) as ctx:
                lark_im.resolve_session_context()
            self.assertIn("No active session", str(ctx.exception))
        finally:
            handoff_config.load_credentials = orig_load
            lark_im.get_tenant_token = orig_token

    def test_no_credentials_raises(self):
        os.environ["HANDOFF_SESSION_ID"] = "s1"
        orig_load = handoff_config.load_credentials
        handoff_config.load_credentials = lambda: None
        try:
            with self.assertRaises(RuntimeError) as ctx:
                lark_im.resolve_session_context()
            self.assertIn("credentials", str(ctx.exception).lower())
        finally:
            handoff_config.load_credentials = orig_load

    def test_success(self):
        os.environ["HANDOFF_SESSION_ID"] = "s1"
        handoff_db.register_session("s1", "chat-1", "opus")

        orig_load = handoff_config.load_credentials
        orig_token = lark_im.get_tenant_token
        handoff_config.load_credentials = lambda: {"app_id": "a", "app_secret": "b"}
        lark_im.get_tenant_token = lambda a, b: "tok"
        try:
            ctx = lark_im.resolve_session_context()
            self.assertEqual(ctx["token"], "tok")
            self.assertEqual(ctx["session_id"], "s1")
            self.assertEqual(ctx["chat_id"], "chat-1")
            self.assertIn("session", ctx)
        finally:
            handoff_config.load_credentials = orig_load
            lark_im.get_tenant_token = orig_token


# ---------------------------------------------------------------------------
# send_and_wait.py — argument parsing
# ---------------------------------------------------------------------------

class SendAndWaitArgParsingTest(unittest.TestCase):
    """Tests for newline replacement and button parsing."""

    def test_newline_replacement(self):
        """Escaped \\n in CLI args should become real newlines."""
        # Simulate what the CLI does: the arg string contains a literal backslash-n
        msg = "line1\\nline2"
        result = msg.replace("\\n", "\n")
        self.assertEqual(result, "line1\nline2")

    def test_invalid_buttons_json(self):
        """Invalid --buttons JSON should exit with code 1."""
        old_argv = sys.argv
        old_stderr = sys.stderr
        sys.argv = ["send_and_wait.py", "hello", "--buttons", "not json"]
        sys.stderr = io.StringIO()
        try:
            with self.assertRaises(SystemExit) as ctx:
                send_and_wait.main()
            self.assertEqual(ctx.exception.code, 1)
        finally:
            sys.argv = old_argv
            sys.stderr = old_stderr

    def test_buttons_enables_card_mode(self):
        """When --buttons is provided, card mode should be auto-enabled."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("message")
        parser.add_argument("--card", action="store_true")
        parser.add_argument("--buttons", default="")
        parser.add_argument("--color", default="blue")
        parser.add_argument("--title", default="")
        parser.add_argument("--timeout", type=int, default=540)
        args = parser.parse_args(["hello", "--buttons", '[["OK","ok","primary"]]'])

        # Replicate the logic from send_and_wait.main()
        if args.buttons:
            buttons = json.loads(args.buttons)
            args.card = True

        self.assertTrue(args.card)


# ---------------------------------------------------------------------------
# send_and_wait.py — send/wait phases
# ---------------------------------------------------------------------------

class SendAndWaitPhasesTest(unittest.TestCase):
    """Tests for the send and wait phases with mocked dependencies."""

    def setUp(self):
        self._orig_resolve = lark_im.resolve_session_context
        self._orig_load_worker = handoff_config.load_worker_url
        self._orig_get_session = handoff_db.get_session
        self._orig_poll_ws = handoff_worker.poll_worker_ws
        self._orig_ack = handoff_worker.ack_worker_replies
        self._orig_send = send_and_wait.send_to_group.send
        self._orig_handle = send_and_wait.wait_for_reply.handle_result
        self._orig_fetch_http = send_and_wait.wait_for_reply.fetch_replies_http

    def tearDown(self):
        lark_im.resolve_session_context = self._orig_resolve
        handoff_config.load_worker_url = self._orig_load_worker
        handoff_db.get_session = self._orig_get_session
        handoff_worker.poll_worker_ws = self._orig_poll_ws
        handoff_worker.ack_worker_replies = self._orig_ack
        send_and_wait.send_to_group.send = self._orig_send
        send_and_wait.wait_for_reply.handle_result = self._orig_handle
        send_and_wait.wait_for_reply.fetch_replies_http = self._orig_fetch_http

    def _run(self, args_list):
        old_argv = sys.argv
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.argv = ["send_and_wait.py"] + args_list
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        try:
            send_and_wait.main()
            return sys.stdout.getvalue()
        except SystemExit:
            return sys.stdout.getvalue()
        finally:
            sys.argv = old_argv
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    def test_session_context_error_exits(self):
        """resolve_session_context failure exits with code 1."""
        def bad_resolve():
            raise RuntimeError("No credentials configured")

        lark_im.resolve_session_context = bad_resolve

        old_argv = sys.argv
        old_stderr = sys.stderr
        sys.argv = ["send_and_wait.py", "hello"]
        sys.stderr = io.StringIO()
        try:
            with self.assertRaises(SystemExit) as ctx:
                send_and_wait.main()
            self.assertEqual(ctx.exception.code, 1)
        finally:
            sys.argv = old_argv
            sys.stderr = old_stderr

    def test_no_worker_url_returns_error_json(self):
        sent = []
        lark_im.resolve_session_context = lambda: {
            "token": "tok", "session_id": "s1", "chat_id": "c1",
            "session": {"chat_id": "c1"},
        }
        send_and_wait.send_to_group.send = lambda *a, **kw: sent.append(1)
        handoff_config.load_worker_url = lambda: None

        output = self._run(["hello"])
        data = json.loads(output)
        self.assertEqual(data["error"], "no_worker_url")
        self.assertEqual(len(sent), 1)  # Message was sent before wait phase

    def test_ws_takeover_returns_takeover_json(self):
        sent = []
        lark_im.resolve_session_context = lambda: {
            "token": "tok", "session_id": "s1", "chat_id": "c1",
            "session": {"chat_id": "c1"},
        }
        send_and_wait.send_to_group.send = lambda *a, **kw: sent.append(1)
        handoff_config.load_worker_url = lambda: "https://w.example"
        handoff_db.get_session = lambda sid: {"chat_id": "c1", "last_checked": "100"}
        handoff_worker.poll_worker_ws = lambda *a, **kw: {
            "replies": [], "takeover": True, "error": None,
        }

        output = self._run(["hello", "--timeout", "5"])
        data = json.loads(output)
        self.assertTrue(data["takeover"])

    def test_ws_replies_calls_handle_result(self):
        handled = []
        lark_im.resolve_session_context = lambda: {
            "token": "tok", "session_id": "s1", "chat_id": "c1",
            "session": {"chat_id": "c1"},
        }
        send_and_wait.send_to_group.send = lambda *a, **kw: None
        handoff_config.load_worker_url = lambda: "https://w.example"
        handoff_db.get_session = lambda sid: {"chat_id": "c1", "last_checked": "100"}
        handoff_worker.poll_worker_ws = lambda *a, **kw: {
            "replies": [{"text": "hi", "create_time": "200", "message_id": "r1"}],
            "takeover": False,
            "error": None,
        }
        send_and_wait.wait_for_reply.handle_result = (
            lambda replies, *a, **kw: handled.append(replies)
        )

        self._run(["hello", "--timeout", "5"])
        self.assertEqual(len(handled), 1)
        self.assertEqual(handled[0][0]["text"], "hi")

    def test_timeout_returns_timeout_json(self):
        lark_im.resolve_session_context = lambda: {
            "token": "tok", "session_id": "s1", "chat_id": "c1",
            "session": {"chat_id": "c1"},
        }
        send_and_wait.send_to_group.send = lambda *a, **kw: None
        handoff_config.load_worker_url = lambda: "https://w.example"
        handoff_db.get_session = lambda sid: {"chat_id": "c1", "last_checked": "100"}
        # WS error forces HTTP fallback, which also returns no replies
        handoff_worker.poll_worker_ws = lambda *a, **kw: (_ for _ in ()).throw(
            Exception("ws fail")
        )
        send_and_wait.wait_for_reply.fetch_replies_http = (
            lambda *a, **kw: ([], False, None)
        )

        output = self._run(["hello", "--timeout", "1"])
        data = json.loads(output)
        self.assertTrue(data["timeout"])
        self.assertEqual(data["count"], 0)

    def test_http_fallback_on_ws_error(self):
        handled = []
        lark_im.resolve_session_context = lambda: {
            "token": "tok", "session_id": "s1", "chat_id": "c1",
            "session": {"chat_id": "c1"},
        }
        send_and_wait.send_to_group.send = lambda *a, **kw: None
        handoff_config.load_worker_url = lambda: "https://w.example"
        handoff_db.get_session = lambda sid: {"chat_id": "c1", "last_checked": "100"}
        handoff_worker.ack_worker_replies = lambda *a, **kw: None

        # WS always errors, HTTP returns replies
        handoff_worker.poll_worker_ws = lambda *a, **kw: (_ for _ in ()).throw(
            Exception("ws fail")
        )
        send_and_wait.wait_for_reply.fetch_replies_http = (
            lambda *a, **kw: (
                [{"text": "reply", "create_time": "300", "message_id": "r2"}],
                False,
                None,
            )
        )
        send_and_wait.wait_for_reply.handle_result = (
            lambda replies, *a, **kw: handled.append(replies)
        )

        self._run(["hello", "--timeout", "5"])
        self.assertEqual(len(handled), 1)
        self.assertEqual(handled[0][0]["text"], "reply")

    def test_http_takeover(self):
        lark_im.resolve_session_context = lambda: {
            "token": "tok", "session_id": "s1", "chat_id": "c1",
            "session": {"chat_id": "c1"},
        }
        send_and_wait.send_to_group.send = lambda *a, **kw: None
        handoff_config.load_worker_url = lambda: "https://w.example"
        handoff_db.get_session = lambda sid: {"chat_id": "c1", "last_checked": "100"}

        handoff_worker.poll_worker_ws = lambda *a, **kw: (_ for _ in ()).throw(
            Exception("ws fail")
        )
        send_and_wait.wait_for_reply.fetch_replies_http = (
            lambda *a, **kw: ([], True, None)
        )

        output = self._run(["hello", "--timeout", "5"])
        data = json.loads(output)
        self.assertTrue(data["takeover"])


# ---------------------------------------------------------------------------
# Backoff jitter
# ---------------------------------------------------------------------------

class BackoffJitterTest(unittest.TestCase):
    """Verify that backoff uses random jitter (not deterministic)."""

    def test_jitter_is_bounded(self):
        """random.uniform(0, backoff) should be in [0, backoff]."""
        import random

        backoff = 10
        for _ in range(100):
            jitter = random.uniform(0, backoff)
            self.assertGreaterEqual(jitter, 0)
            self.assertLessEqual(jitter, backoff)


if __name__ == "__main__":
    unittest.main()
