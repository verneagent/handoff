#!/usr/bin/env python3
"""Tests for on_notification.py hook script.

Covers session lookup, notification type routing, card building/sending,
and error handling paths.
"""

import io
import json
import os
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

import on_notification  # type: ignore


class OnNotificationTest(unittest.TestCase):
    def setUp(self):
        self._orig_get_session = on_notification.handoff_db.get_session
        self._orig_load_creds = on_notification.handoff_config.load_credentials
        self._orig_get_token = on_notification.lark_im.get_tenant_token
        self._orig_build_card = on_notification.lark_im.build_markdown_card
        self._orig_send_msg = on_notification.lark_im.send_message
        self._orig_record = on_notification.handoff_db.record_sent_message
        self._orig_worktree = on_notification.handoff_config.get_worktree_name

    def tearDown(self):
        on_notification.handoff_db.get_session = self._orig_get_session
        on_notification.handoff_config.load_credentials = self._orig_load_creds
        on_notification.lark_im.get_tenant_token = self._orig_get_token
        on_notification.lark_im.build_markdown_card = self._orig_build_card
        on_notification.lark_im.send_message = self._orig_send_msg
        on_notification.handoff_db.record_sent_message = self._orig_record
        on_notification.handoff_config.get_worktree_name = self._orig_worktree

    def _run_main(self, hook_input):
        old_stdin, old_stdout, old_stderr = sys.stdin, sys.stdout, sys.stderr
        sys.stdin = io.StringIO(json.dumps(hook_input))
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        try:
            on_notification.main()
            return sys.stdout.getvalue(), sys.stderr.getvalue()
        finally:
            sys.stdin = old_stdin
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    # -- Early returns --

    def test_no_session_returns_early(self):
        """No active session → return without sending anything."""
        sent = []
        on_notification.handoff_db.get_session = lambda sid: None
        on_notification.lark_im.send_message = lambda *a, **kw: sent.append(1)

        self._run_main({"session_id": "s1", "notification_type": "elicitation_dialog", "message": "hi"})
        self.assertEqual(len(sent), 0)

    def test_empty_session_id_returns_early(self):
        sent = []
        on_notification.handoff_db.get_session = lambda sid: {"chat_id": "c1"}
        on_notification.lark_im.send_message = lambda *a, **kw: sent.append(1)

        self._run_main({"session_id": "", "notification_type": "elicitation_dialog", "message": "hi"})
        self.assertEqual(len(sent), 0)

    def test_permission_prompt_returns_early(self):
        """permission_prompt is handled by permission_bridge, not here."""
        sent = []
        on_notification.handoff_db.get_session = lambda sid: {"chat_id": "c1"}
        on_notification.lark_im.send_message = lambda *a, **kw: sent.append(1)

        self._run_main({
            "session_id": "s1",
            "notification_type": "permission_prompt",
            "message": "approve?",
        })
        self.assertEqual(len(sent), 0)

    # -- idle_prompt --

    def test_idle_prompt_prints_recovery(self):
        """idle_prompt outputs recovery instructions to stdout."""
        on_notification.handoff_db.get_session = lambda sid: {"chat_id": "c1"}

        stdout, _ = self._run_main({
            "session_id": "s1",
            "notification_type": "idle_prompt",
            "message": "idle",
        })
        self.assertIn("Handoff loop recovery", stdout)
        self.assertIn("chat_id: c1", stdout)
        self.assertIn("wait_for_reply.py", stdout)

    # -- Normal notification sending --

    def test_normal_notification_sends_card(self):
        """Normal notification type sends a markdown card to Lark."""
        sent = []
        on_notification.handoff_db.get_session = lambda sid: {"chat_id": "c1"}
        on_notification.handoff_config.load_credentials = lambda **kw: {
            "app_id": "a", "app_secret": "b"
        }
        on_notification.lark_im.get_tenant_token = lambda a, b: "tok"
        on_notification.handoff_config.get_worktree_name = lambda: "my-branch"
        on_notification.lark_im.build_markdown_card = (
            lambda msg, title=None, color=None: {"title": title, "body": msg, "color": color}
        )
        on_notification.lark_im.send_message = lambda tok, cid, card: (
            sent.append({"token": tok, "chat_id": cid, "card": card}) or "msg-1"
        )
        on_notification.handoff_db.record_sent_message = lambda *a, **kw: None

        self._run_main({
            "session_id": "s1",
            "notification_type": "elicitation_dialog",
            "message": "Something happened",
        })
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["token"], "tok")
        self.assertEqual(sent[0]["chat_id"], "c1")
        self.assertIn("my-branch", sent[0]["card"]["title"])
        self.assertEqual(sent[0]["card"]["color"], "purple")  # elicitation_dialog

    def test_elicitation_dialog_uses_purple(self):
        on_notification.handoff_db.get_session = lambda sid: {"chat_id": "c1"}
        on_notification.handoff_config.load_credentials = lambda **kw: {
            "app_id": "a", "app_secret": "b"
        }
        on_notification.lark_im.get_tenant_token = lambda a, b: "tok"
        on_notification.handoff_config.get_worktree_name = lambda: "wt"
        cards = []
        on_notification.lark_im.build_markdown_card = (
            lambda msg, title=None, color=None: cards.append(color) or {}
        )
        on_notification.lark_im.send_message = lambda *a, **kw: "m1"
        on_notification.handoff_db.record_sent_message = lambda *a, **kw: None

        self._run_main({
            "session_id": "s1",
            "notification_type": "elicitation_dialog",
            "message": "Need input",
        })
        self.assertEqual(cards[0], "purple")

    # -- Error handling --

    def test_no_credentials_returns_silently(self):
        on_notification.handoff_db.get_session = lambda sid: {"chat_id": "c1"}
        on_notification.handoff_config.load_credentials = lambda **kw: None
        sent = []
        on_notification.lark_im.send_message = lambda *a, **kw: sent.append(1)

        self._run_main({
            "session_id": "s1",
            "notification_type": "elicitation_dialog",
            "message": "hi",
        })
        self.assertEqual(len(sent), 0)

    def test_token_error_returns_silently(self):
        on_notification.handoff_db.get_session = lambda sid: {"chat_id": "c1"}
        on_notification.handoff_config.load_credentials = lambda **kw: {
            "app_id": "a", "app_secret": "b"
        }

        def bad_token(a, b):
            raise RuntimeError("auth fail")

        on_notification.lark_im.get_tenant_token = bad_token
        sent = []
        on_notification.lark_im.send_message = lambda *a, **kw: sent.append(1)

        _, stderr = self._run_main({
            "session_id": "s1",
            "notification_type": "elicitation_dialog",
            "message": "hi",
        })
        self.assertEqual(len(sent), 0)
        self.assertIn("auth fail", stderr)

    def test_send_failure_logs_to_stderr(self):
        on_notification.handoff_db.get_session = lambda sid: {"chat_id": "c1"}
        on_notification.handoff_config.load_credentials = lambda **kw: {
            "app_id": "a", "app_secret": "b"
        }
        on_notification.lark_im.get_tenant_token = lambda a, b: "tok"
        on_notification.handoff_config.get_worktree_name = lambda: "wt"
        on_notification.lark_im.build_markdown_card = lambda *a, **kw: {}

        def fail_send(*a, **kw):
            raise RuntimeError("network")

        on_notification.lark_im.send_message = fail_send

        _, stderr = self._run_main({
            "session_id": "s1",
            "notification_type": "elicitation_dialog",
            "message": "hi",
        })
        self.assertIn("network", stderr)

    def test_record_failure_does_not_crash(self):
        on_notification.handoff_db.get_session = lambda sid: {"chat_id": "c1"}
        on_notification.handoff_config.load_credentials = lambda **kw: {
            "app_id": "a", "app_secret": "b"
        }
        on_notification.lark_im.get_tenant_token = lambda a, b: "tok"
        on_notification.handoff_config.get_worktree_name = lambda: "wt"
        on_notification.lark_im.build_markdown_card = lambda *a, **kw: {}
        on_notification.lark_im.send_message = lambda *a, **kw: "m1"

        def fail_record(*a, **kw):
            raise RuntimeError("db error")

        on_notification.handoff_db.record_sent_message = fail_record

        _, stderr = self._run_main({
            "session_id": "s1",
            "notification_type": "elicitation_dialog",
            "message": "hi",
        })
        self.assertIn("db error", stderr)

    def test_invalid_json_stdin(self):
        """Malformed JSON defaults to empty dict, no session → early return."""
        on_notification.handoff_db.get_session = lambda sid: None

        old_stdin, old_stdout, old_stderr = sys.stdin, sys.stdout, sys.stderr
        sys.stdin = io.StringIO("not json!!!")
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        try:
            on_notification.main()
            stderr = sys.stderr.getvalue()
        finally:
            sys.stdin = old_stdin
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        self.assertIn("invalid", stderr)

    def test_default_message(self):
        """Missing message field uses default text."""
        on_notification.handoff_db.get_session = lambda sid: {"chat_id": "c1"}
        on_notification.handoff_config.load_credentials = lambda **kw: {
            "app_id": "a", "app_secret": "b"
        }
        on_notification.lark_im.get_tenant_token = lambda a, b: "tok"
        on_notification.handoff_config.get_worktree_name = lambda: "wt"
        bodies = []
        on_notification.lark_im.build_markdown_card = (
            lambda msg, title=None, color=None: bodies.append(msg) or {}
        )
        on_notification.lark_im.send_message = lambda *a, **kw: "m1"
        on_notification.handoff_db.record_sent_message = lambda *a, **kw: None

        self._run_main({"session_id": "s1", "notification_type": "elicitation_dialog"})
        self.assertIn("attention", bodies[0])


if __name__ == "__main__":
    unittest.main()
