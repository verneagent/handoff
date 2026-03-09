#!/usr/bin/env python3
"""Tests for Durable Objects quota exhaustion detection and warning.

Covers:
- is_do_quota_error() pattern matching in handoff_worker.py
- _send_quota_warning() card delivery in wait_for_reply.py
- Quota warning sent once per session in wait_for_reply.py poll loop
- Quota warning sent once per session in send_and_wait.py poll loop
- Worker-side isDOQuotaError() detection and KV flag in index.js (logic only)
- /status endpoint (documented, not tested here — needs wrangler)
"""

import io
import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

import handoff_config
import handoff_db
import handoff_worker
import lark_im
import send_and_wait  # type: ignore
import wait_for_reply  # type: ignore


# ---------------------------------------------------------------------------
# handoff_worker.is_do_quota_error()
# ---------------------------------------------------------------------------


class IsDOQuotaErrorTest(unittest.TestCase):
    """Tests for the is_do_quota_error() helper."""

    def test_exact_cf_error(self):
        self.assertTrue(handoff_worker.is_do_quota_error(
            "Worker error: Exceeded allowed duration in Durable Objects free tier."
        ))

    def test_case_insensitive(self):
        self.assertTrue(handoff_worker.is_do_quota_error(
            "EXCEEDED ALLOWED DURATION IN DURABLE OBJECTS FREE TIER"
        ))

    def test_cpu_time_limit(self):
        self.assertTrue(handoff_worker.is_do_quota_error(
            "Worker error: Exceeded its CPU time limit"
        ))

    def test_partial_match(self):
        self.assertTrue(handoff_worker.is_do_quota_error(
            "Something exceeded allowed duration something"
        ))

    def test_unrelated_error(self):
        self.assertFalse(handoff_worker.is_do_quota_error(
            "Worker error: internal server error"
        ))

    def test_empty_string(self):
        self.assertFalse(handoff_worker.is_do_quota_error(""))

    def test_none(self):
        self.assertFalse(handoff_worker.is_do_quota_error(None))

    def test_curl_failure(self):
        self.assertFalse(handoff_worker.is_do_quota_error(
            "curl failed (exit 28)"
        ))


# ---------------------------------------------------------------------------
# wait_for_reply._send_quota_warning()
# ---------------------------------------------------------------------------


class SendQuotaWarningTest(unittest.TestCase):
    """Tests for _send_quota_warning() in wait_for_reply.py."""

    def setUp(self):
        self._orig_load_creds = handoff_config.load_credentials
        self._orig_get_token = lark_im.get_tenant_token
        self._orig_send_msg = lark_im.send_message

    def tearDown(self):
        handoff_config.load_credentials = self._orig_load_creds
        lark_im.get_tenant_token = self._orig_get_token
        lark_im.send_message = self._orig_send_msg

    def test_sends_card_on_success(self):
        sent = []
        handoff_config.load_credentials = lambda: {"app_id": "a", "app_secret": "b"}
        lark_im.get_tenant_token = lambda a, b: "tok"
        lark_im.send_message = lambda token, chat_id, card: sent.append(
            {"token": token, "chat_id": chat_id, "card": card}
        )

        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        try:
            result = wait_for_reply._send_quota_warning("chat-1")
        finally:
            sys.stderr = old_stderr

        self.assertTrue(result)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["chat_id"], "chat-1")
        self.assertEqual(sent[0]["token"], "tok")
        # Card should have orange header about quota
        card = sent[0]["card"]
        self.assertEqual(card["header"]["template"], "orange")
        self.assertIn("quota", card["header"]["title"]["content"].lower())

    def test_returns_false_on_no_credentials(self):
        handoff_config.load_credentials = lambda: None

        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        try:
            result = wait_for_reply._send_quota_warning("chat-1")
        finally:
            sys.stderr = old_stderr

        self.assertFalse(result)

    def test_returns_false_on_send_error(self):
        handoff_config.load_credentials = lambda: {"app_id": "a", "app_secret": "b"}
        lark_im.get_tenant_token = lambda a, b: "tok"
        lark_im.send_message = lambda *a: (_ for _ in ()).throw(
            Exception("send failed")
        )

        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        try:
            result = wait_for_reply._send_quota_warning("chat-1")
        finally:
            sys.stderr = old_stderr

        self.assertFalse(result)


# ---------------------------------------------------------------------------
# wait_for_reply.py poll loop — quota warning sent once
# ---------------------------------------------------------------------------


class WaitForReplyQuotaWarningTest(unittest.TestCase):
    """Verify wait_for_reply sends the quota warning card exactly once."""

    def setUp(self):
        self._orig_resolve = lark_im.resolve_session_context
        self._orig_load_worker = handoff_config.load_worker_url
        self._orig_poll_ws = handoff_worker.poll_worker_ws
        self._orig_send_warning = wait_for_reply._send_quota_warning
        self._orig_fetch_http = wait_for_reply.fetch_replies_http
        self._orig_get_unprocessed = handoff_db.get_unprocessed_messages
        self._orig_set_last = handoff_db.set_session_last_checked
        self._orig_time_sleep = wait_for_reply.time.sleep

    def tearDown(self):
        lark_im.resolve_session_context = self._orig_resolve
        handoff_config.load_worker_url = self._orig_load_worker
        handoff_worker.poll_worker_ws = self._orig_poll_ws
        wait_for_reply._send_quota_warning = self._orig_send_warning
        wait_for_reply.fetch_replies_http = self._orig_fetch_http
        handoff_db.get_unprocessed_messages = self._orig_get_unprocessed
        handoff_db.set_session_last_checked = self._orig_set_last
        wait_for_reply.time.sleep = self._orig_time_sleep

    def _run(self, args_list):
        old_argv = sys.argv
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.argv = ["wait_for_reply.py"] + args_list
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        try:
            wait_for_reply.main()
            return sys.stdout.getvalue(), sys.stderr.getvalue()
        except SystemExit:
            return sys.stdout.getvalue(), sys.stderr.getvalue()
        finally:
            sys.argv = old_argv
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    def test_quota_warning_sent_once_then_timeout(self):
        """On repeated DO quota errors, warning card is sent only once."""
        lark_im.resolve_session_context = lambda: {
            "token": "tok", "session_id": "s1", "chat_id": "c1",
            "session": {"chat_id": "c1", "last_checked": "100"},
        }
        handoff_config.load_worker_url = lambda: "https://w.example"
        handoff_db.get_unprocessed_messages = lambda chat_id: []

        # WS always fails
        handoff_worker.poll_worker_ws = lambda *a, **kw: (_ for _ in ()).throw(
            Exception("ws fail")
        )
        # HTTP always returns the DO quota error
        wait_for_reply.fetch_replies_http = lambda *a, **kw: (
            [], False, "Worker error: Exceeded allowed duration in Durable Objects free tier."
        )
        wait_for_reply.time.sleep = lambda s: None  # Skip sleeps

        warning_calls = []
        wait_for_reply._send_quota_warning = lambda chat_id: (
            warning_calls.append(chat_id) or True
        )

        stdout, stderr = self._run(["--timeout", "1"])

        # Warning should be sent exactly once
        self.assertEqual(len(warning_calls), 1)
        self.assertEqual(warning_calls[0], "c1")

        # Should eventually time out
        data = json.loads(stdout)
        self.assertTrue(data.get("timeout"))

    def test_no_quota_warning_on_regular_error(self):
        """Regular (non-quota) errors should not trigger the warning card."""
        lark_im.resolve_session_context = lambda: {
            "token": "tok", "session_id": "s1", "chat_id": "c1",
            "session": {"chat_id": "c1", "last_checked": "100"},
        }
        handoff_config.load_worker_url = lambda: "https://w.example"
        handoff_db.get_unprocessed_messages = lambda chat_id: []

        handoff_worker.poll_worker_ws = lambda *a, **kw: (_ for _ in ()).throw(
            Exception("ws fail")
        )
        wait_for_reply.fetch_replies_http = lambda *a, **kw: (
            [], False, "Worker error: internal server error"
        )
        wait_for_reply.time.sleep = lambda s: None

        warning_calls = []
        wait_for_reply._send_quota_warning = lambda chat_id: (
            warning_calls.append(chat_id) or True
        )

        self._run(["--timeout", "1"])
        self.assertEqual(len(warning_calls), 0)


# ---------------------------------------------------------------------------
# send_and_wait.py poll loop — quota warning sent once
# ---------------------------------------------------------------------------


class SendAndWaitQuotaWarningTest(unittest.TestCase):
    """Verify send_and_wait sends the quota warning card exactly once."""

    def setUp(self):
        self._orig_resolve = lark_im.resolve_session_context
        self._orig_load_worker = handoff_config.load_worker_url
        self._orig_get_session = handoff_db.get_session
        self._orig_poll_ws = handoff_worker.poll_worker_ws
        self._orig_ack = handoff_worker.ack_worker_replies
        self._orig_send = send_and_wait.send_to_group.send
        self._orig_handle = send_and_wait.wait_for_reply.handle_result
        self._orig_fetch_http = send_and_wait.wait_for_reply.fetch_replies_http
        self._orig_send_warning = send_and_wait.wait_for_reply._send_quota_warning
        self._orig_time_sleep = send_and_wait.time.sleep

    def tearDown(self):
        lark_im.resolve_session_context = self._orig_resolve
        handoff_config.load_worker_url = self._orig_load_worker
        handoff_db.get_session = self._orig_get_session
        handoff_worker.poll_worker_ws = self._orig_poll_ws
        handoff_worker.ack_worker_replies = self._orig_ack
        send_and_wait.send_to_group.send = self._orig_send
        send_and_wait.wait_for_reply.handle_result = self._orig_handle
        send_and_wait.wait_for_reply.fetch_replies_http = self._orig_fetch_http
        send_and_wait.wait_for_reply._send_quota_warning = self._orig_send_warning
        send_and_wait.time.sleep = self._orig_time_sleep

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

    def test_quota_warning_sent_once(self):
        """On repeated DO quota errors in send_and_wait, warning sent once."""
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
        send_and_wait.wait_for_reply.fetch_replies_http = lambda *a, **kw: (
            [], False,
            "Worker error: Exceeded allowed duration in Durable Objects free tier.",
        )
        send_and_wait.time.sleep = lambda s: None

        warning_calls = []
        send_and_wait.wait_for_reply._send_quota_warning = lambda chat_id: (
            warning_calls.append(chat_id) or True
        )

        output = self._run(["hello", "--timeout", "1"])
        self.assertEqual(len(warning_calls), 1)
        self.assertEqual(warning_calls[0], "c1")

        data = json.loads(output)
        self.assertTrue(data.get("timeout"))

    def test_no_warning_on_non_quota_error(self):
        """Non-quota HTTP errors do not trigger the warning."""
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
        send_and_wait.wait_for_reply.fetch_replies_http = lambda *a, **kw: (
            [], False, "Worker error: something else"
        )
        send_and_wait.time.sleep = lambda s: None

        warning_calls = []
        send_and_wait.wait_for_reply._send_quota_warning = lambda chat_id: (
            warning_calls.append(chat_id) or True
        )

        self._run(["hello", "--timeout", "1"])
        self.assertEqual(len(warning_calls), 0)


if __name__ == "__main__":
    unittest.main()
