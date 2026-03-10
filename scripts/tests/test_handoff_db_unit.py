#!/usr/bin/env python3

import json
import os
import sqlite3
import tempfile
import unittest

import sys

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

import handoff_config
import handoff_db
import handoff_worker
import lark_im


class HandoffDbUnitTest(unittest.TestCase):
    def setUp(self):
        self._old_home = os.environ.get("HOME")
        self._old_project = os.environ.get("HANDOFF_PROJECT_DIR")
        self._old_tool = os.environ.get("HANDOFF_SESSION_TOOL")

        self.tmp = tempfile.TemporaryDirectory()
        self.project_dir = os.path.join(self.tmp.name, "project")
        os.makedirs(self.project_dir, exist_ok=True)

        os.environ["HOME"] = self.tmp.name
        self._old_handoff_home = handoff_config.HANDOFF_HOME
        handoff_config.HANDOFF_HOME = os.path.join(self.tmp.name, ".handoff")
        os.environ["HANDOFF_PROJECT_DIR"] = self.project_dir
        os.environ["HANDOFF_SESSION_TOOL"] = "Claude Code"

        self.db_path = handoff_db._db_path()
        handoff_db._db_initialized.discard(self.db_path)
        conn = handoff_db._get_db()
        conn.close()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home

        if self._old_project is None:
            os.environ.pop("HANDOFF_PROJECT_DIR", None)
        else:
            os.environ["HANDOFF_PROJECT_DIR"] = self._old_project

        if self._old_tool is None:
            os.environ.pop("HANDOFF_SESSION_TOOL", None)
        else:
            os.environ["HANDOFF_SESSION_TOOL"] = self._old_tool

        handoff_config.HANDOFF_HOME = self._old_handoff_home
        self.tmp.cleanup()

    def _table_info(self, table):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(f"PRAGMA table_info({table})").fetchall()
        finally:
            conn.close()

    def test_schema_constraints(self):
        sessions = {r[1]: r for r in self._table_info("sessions")}
        self.assertEqual(sessions["session_id"][3], 1)  # notnull
        self.assertEqual(sessions["session_id"][5], 1)  # pk
        self.assertEqual(sessions["chat_id"][3], 1)
        self.assertEqual(sessions["session_tool"][3], 1)
        self.assertEqual(sessions["session_model"][3], 1)
        self.assertEqual(sessions["activated_at"][3], 1)

        messages = {r[1]: r for r in self._table_info("messages")}
        self.assertEqual(messages["message_id"][3], 1)
        self.assertEqual(messages["chat_id"][3], 1)
        self.assertEqual(messages["direction"][3], 1)

    def test_unique_chat_claim(self):
        handoff_db.register_session("s1", "chat-1", "opus")
        s1 = handoff_db.get_session("s1")
        if s1 is None:
            self.fail("expected session s1")
        self.assertEqual(s1["chat_id"], "chat-1")

        with self.assertRaises(RuntimeError):
            handoff_db.register_session("s2", "chat-1", "opus")

        handoff_db.register_session("s1", "chat-1", "opus")
        s1_new = handoff_db.get_session("s1")
        if s1_new is None:
            self.fail("expected updated session s1")

    def test_session_tool_default_from_env(self):
        os.environ["HANDOFF_SESSION_TOOL"] = "OpenCode"
        handoff_db.register_session("sid-env", "chat-env", "opus")
        sess = handoff_db.get_session("sid-env")
        if sess is None:
            self.fail("expected session sid-env")
        self.assertEqual(sess["session_tool"], "OpenCode")

    def test_prune_stale_sessions(self):
        now_s = 1_700_000_000
        old_ms = (now_s - 40 * 24 * 60 * 60) * 1000
        new_ms = (now_s - 60) * 1000

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO sessions (session_id, chat_id, session_tool, session_model, last_checked, activated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "old",
                    "chat-old",
                    "Claude Code",
                    "opus",
                    old_ms,
                    now_s - 40 * 24 * 60 * 60,
                ),
            )
            conn.execute(
                "INSERT INTO sessions (session_id, chat_id, session_tool, session_model, last_checked, activated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("new", "chat-new", "Claude Code", "opus", new_ms, now_s - 60),
            )
            conn.commit()
        finally:
            conn.close()

        # Monkeypatch time for deterministic cutoff
        real_time = handoff_db.time.time
        handoff_db.time.time = lambda: now_s
        try:
            deleted = handoff_db.prune_stale_sessions()
        finally:
            handoff_db.time.time = real_time

        self.assertEqual(deleted, 1)
        self.assertIsNone(handoff_db.get_session("old"))
        self.assertIsNotNone(handoff_db.get_session("new"))

    def test_message_recording_directions(self):
        with self.assertRaises(ValueError):
            handoff_db.record_sent_message("m1", text="x", title="t", chat_id=None)

        handoff_db.record_sent_message("m1", text="sent", title="A", chat_id="chat-a")
        handoff_db.record_received_message(
            chat_id="chat-a",
            text="recv",
            source_message_id="ext-1",
            message_time=1700000000000,
        )

        parent = handoff_db.lookup_parent_message("m1")
        if parent is None:
            self.fail("expected parent message m1")
        self.assertEqual(parent["text"], "sent")

        parent_recv = handoff_db.lookup_parent_message("recv:ext-1")
        self.assertIsNone(parent_recv)

    def test_safe_local_filename_blocks_traversal(self):
        self.assertEqual(lark_im._safe_local_filename("../../etc/passwd"), "passwd")
        self.assertEqual(
            lark_im._safe_local_filename(r"..\\..\\secrets.txt"), "secrets.txt"
        )
        generated = lark_im._safe_local_filename("")
        self.assertTrue(generated.startswith("file-"))

    def test_takeover_chat_compare_and_swap(self):
        handoff_db.register_session("old", "chat-tk", "opus")

        ok, owner, replaced = handoff_db.takeover_chat(
            "new",
            "chat-tk",
            "sonnet",
            expected_owner_session_id="old",
        )
        self.assertTrue(ok)
        self.assertEqual(owner, "new")
        self.assertEqual(replaced, "old")

        sess = handoff_db.get_session("new")
        if sess is None:
            self.fail("expected new session owner")
        self.assertEqual(sess["chat_id"], "chat-tk")
        self.assertIsNone(handoff_db.get_session("old"))

    def test_takeover_chat_conflict_rejected(self):
        handoff_db.register_session("old", "chat-tk2", "opus")
        handoff_db.register_session("other", "chat-tk2b", "opus")

        # Current owner is "old", expected says "other" -> conflict.
        ok, owner, replaced = handoff_db.takeover_chat(
            "new",
            "chat-tk2",
            "sonnet",
            expected_owner_session_id="other",
        )
        self.assertFalse(ok)
        self.assertEqual(owner, "old")
        self.assertIsNone(replaced)
        self.assertIsNone(handoff_db.get_session("new"))

    def test_takeover_chat_without_expected_owner_is_strict(self):
        handoff_db.register_session("old", "chat-strict", "opus")

        ok, owner, replaced = handoff_db.takeover_chat(
            "new",
            "chat-strict",
            "sonnet",
            expected_owner_session_id=None,
        )

        self.assertFalse(ok)
        self.assertEqual(owner, "old")
        self.assertIsNone(replaced)
        self.assertIsNone(handoff_db.get_session("new"))

    # --- Tests for recent bug fixes ---

    def test_set_session_last_checked_int(self):
        """Integer millisecond timestamps are stored as-is."""
        handoff_db.register_session("ts-int", "chat-ts-int", "opus")
        handoff_db.set_session_last_checked("ts-int", 1771300000000)
        sess = handoff_db.get_session("ts-int")
        self.assertIsNotNone(sess)
        self.assertEqual(sess["last_checked"], 1771300000000)

    def test_set_session_last_checked_string_int(self):
        """String integer timestamps (as Lark sends them) are parsed correctly."""
        handoff_db.register_session("ts-str", "chat-ts-str", "opus")
        handoff_db.set_session_last_checked("ts-str", "1771300000000")
        sess = handoff_db.get_session("ts-str")
        self.assertIsNotNone(sess)
        self.assertEqual(sess["last_checked"], 1771300000000)

    def test_set_session_last_checked_float_string(self):
        """Float-string timestamps are safely truncated to int (defensive fix)."""
        handoff_db.register_session("ts-flt", "chat-ts-flt", "opus")
        handoff_db.set_session_last_checked("ts-flt", "1771300000000.5")
        sess = handoff_db.get_session("ts-flt")
        self.assertIsNotNone(sess)
        self.assertEqual(sess["last_checked"], 1771300000000)

    def test_set_session_last_checked_float(self):
        """Float values are coerced to int (defensive fix)."""
        handoff_db.register_session("ts-f2", "chat-ts-f2", "opus")
        handoff_db.set_session_last_checked("ts-f2", 1771300000000.9)
        sess = handoff_db.get_session("ts-f2")
        self.assertIsNotNone(sess)
        self.assertEqual(sess["last_checked"], 1771300000000)

    def test_set_session_last_checked_none(self):
        """None is accepted and stored as NULL without error."""
        handoff_db.register_session("ts-none", "chat-ts-none", "opus")
        handoff_db.set_session_last_checked("ts-none", None)
        sess = handoff_db.get_session("ts-none")
        self.assertIsNotNone(sess)
        self.assertIsNone(sess["last_checked"])

    def test_set_session_last_checked_invalid(self):
        """Non-numeric strings result in NULL without raising."""
        handoff_db.register_session("ts-bad", "chat-ts-bad", "opus")
        handoff_db.set_session_last_checked("ts-bad", "not-a-number")
        sess = handoff_db.get_session("ts-bad")
        self.assertIsNotNone(sess)
        self.assertIsNone(sess["last_checked"])

    def test_record_received_message_hash_stability(self):
        """record_received_message with no source_message_id uses stable sha256 hash.
        Same text + chat + timestamp should produce the same DB message_id on repeated calls.
        """
        # First call — inserts
        handoff_db.record_received_message(
            chat_id="chat-hash",
            text="hello world",
            source_message_id=None,
            message_time=1771300000000,
        )
        # Second call — same inputs, should hit INSERT OR REPLACE idempotently (no error)
        handoff_db.record_received_message(
            chat_id="chat-hash",
            text="hello world",
            source_message_id=None,
            message_time=1771300000000,
        )
        # Verify exactly one row exists (idempotent upsert)
        conn = handoff_db._get_db()
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id = 'chat-hash' AND direction = 'received'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(rows[0], 1)

    def test_recv_exact_closes_socket_on_empty_recv(self):
        """_recv_exact must call self.close() before raising ConnectionError.
        Verifies the socket-leak fix: empty recv → socket is closed before error.
        """
        import socket as socket_module

        closed = []

        class FakeSocket:
            def recv(self, n):
                return b""  # Simulate connection closed

            def close(self):
                closed.append(True)

        ws = handoff_worker._WebSocket.__new__(handoff_worker._WebSocket)
        ws._sock = FakeSocket()
        ws._buf = b""

        with self.assertRaises(ConnectionError):
            ws._recv_exact(4)

        self.assertEqual(len(closed), 1, "_recv_exact must close socket before raising")

    def test_get_unprocessed_messages_returns_unprocessed(self):
        """Messages received after the last sent message are returned."""
        handoff_db.record_sent_message("m-s1", text="hi", title="", chat_id="chat-up")
        handoff_db.record_received_message(
            chat_id="chat-up",
            text="user reply",
            source_message_id="ext-up1",
            message_time=int(handoff_db.time.time() * 1000) + 5000,
        )
        result = handoff_db.get_unprocessed_messages("chat-up")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "user reply")
        self.assertEqual(result[0]["message_id"], "ext-up1")
        self.assertEqual(result[0]["msg_type"], "text")

    def test_get_unprocessed_messages_empty_when_all_processed(self):
        """No unprocessed messages when last sent is newer than last received."""
        handoff_db.record_received_message(
            chat_id="chat-up2",
            text="old msg",
            source_message_id="ext-up2",
            message_time=1000,
        )
        handoff_db.record_sent_message(
            "m-s2", text="response", title="", chat_id="chat-up2"
        )
        result = handoff_db.get_unprocessed_messages("chat-up2")
        self.assertEqual(len(result), 0)

    def test_get_unprocessed_messages_empty_no_messages(self):
        """Empty result when there are no messages at all."""
        result = handoff_db.get_unprocessed_messages("chat-nonexistent")
        self.assertEqual(len(result), 0)

    def test_get_unprocessed_messages_multiple(self):
        """Multiple unprocessed messages are returned in order."""
        handoff_db.record_sent_message("m-s3", text="hi", title="", chat_id="chat-up3")
        base = int(handoff_db.time.time() * 1000) + 5000
        handoff_db.record_received_message(
            chat_id="chat-up3",
            text="msg A",
            source_message_id="ext-a",
            message_time=base,
        )
        handoff_db.record_received_message(
            chat_id="chat-up3",
            text="msg B",
            source_message_id="ext-b",
            message_time=base + 1000,
        )
        result = handoff_db.get_unprocessed_messages("chat-up3")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["text"], "msg A")
        self.assertEqual(result[1]["text"], "msg B")


class CardV2FallbackTest(unittest.TestCase):
    """Tests for Card V2 → V1 → text fallback chain."""

    def _make_v2_card(self, title="Test", body="Hello world"):
        """Build a minimal V2 card for testing."""
        return {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "green",
            },
            "body": {
                "elements": [
                    {"tag": "markdown", "content": body},
                ]
            },
        }

    def _make_v2_form_card(self, title="Pick", body="Choose one"):
        """Build a V2 card with a form containing markdown + select."""
        return {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "body": {
                "elements": [
                    {"tag": "markdown", "content": body},
                    {
                        "tag": "form",
                        "name": "f",
                        "elements": [
                            {"tag": "markdown", "content": "**Options:**"},
                            {"tag": "select_static", "name": "choice"},
                            {
                                "tag": "button",
                                "text": {"content": "Submit"},
                                "action_type": "form_submit",
                            },
                        ],
                    },
                ]
            },
        }

    def _make_v1_card(self, title="Test", body="Hello"):
        return lark_im.build_card(title, body=body, color="blue")

    # --- _is_v2_card ---

    def test_is_v2_card_true(self):
        self.assertTrue(lark_im._is_v2_card(self._make_v2_card()))

    def test_is_v2_card_false_v1(self):
        self.assertFalse(lark_im._is_v2_card(self._make_v1_card()))

    def test_is_v2_card_false_none(self):
        self.assertFalse(lark_im._is_v2_card(None))

    def test_is_v2_card_false_string(self):
        self.assertFalse(lark_im._is_v2_card("not a card"))

    # --- _extract_card_text ---

    def test_extract_v2_card_text(self):
        card = self._make_v2_card("My Title", "Body text here")
        title, body = lark_im._extract_card_text(card)
        self.assertEqual(title, "My Title")
        self.assertIn("Body text here", body)

    def test_extract_v2_form_card_text(self):
        card = self._make_v2_form_card("Form Title", "Pick something")
        title, body = lark_im._extract_card_text(card)
        self.assertEqual(title, "Form Title")
        self.assertIn("Pick something", body)
        self.assertIn("**Options:**", body)

    def test_extract_v1_card_text(self):
        card = self._make_v1_card("V1 Title", "V1 Body")
        title, body = lark_im._extract_card_text(card)
        self.assertEqual(title, "V1 Title")
        self.assertIn("V1 Body", body)

    def test_extract_empty_card(self):
        card = {"header": {}, "elements": []}
        title, body = lark_im._extract_card_text(card)
        self.assertEqual(title, "")
        self.assertEqual(body, "")

    # --- _card_to_v1_fallback ---

    def test_v1_fallback_has_degradation_note(self):
        card = self._make_v2_card("Test", "Content")
        fallback = lark_im._card_to_v1_fallback(card)
        # Should be V1 (no schema key)
        self.assertNotIn("schema", fallback)
        # Should have elements (V1 structure)
        self.assertIn("elements", fallback)
        # Should contain degradation note in the body
        body_text = fallback["elements"][0]["text"]["content"]
        self.assertIn("Lark Card V2 down", body_text)
        self.assertIn("Content", body_text)

    def test_v1_fallback_preserves_title(self):
        card = self._make_v2_card("Important", "Data")
        fallback = lark_im._card_to_v1_fallback(card)
        self.assertEqual(
            fallback["header"]["title"]["content"], "Important"
        )

    def test_v1_fallback_preserves_color(self):
        card = self._make_v2_card()
        card["header"]["template"] = "red"
        fallback = lark_im._card_to_v1_fallback(card)
        self.assertEqual(fallback["header"]["template"], "red")

    # --- _card_to_text_fallback ---

    def test_text_fallback_with_title(self):
        card = self._make_v2_card("Alert", "Something happened")
        text = lark_im._card_to_text_fallback(card)
        self.assertTrue(text.startswith("[Alert]"))
        self.assertIn("Something happened", text)
        self.assertIn("Lark Card V2 down", text)

    def test_text_fallback_no_title(self):
        card = self._make_v2_card("", "Just body")
        text = lark_im._card_to_text_fallback(card)
        self.assertFalse(text.startswith("["))
        self.assertIn("Just body", text)

    # --- send_message fallback chain ---

    def test_send_message_success_no_fallback(self):
        """Card succeeds on first try — no fallback needed."""
        calls = []

        def mock_post(url, token, payload):
            calls.append(payload.get("msg_type"))
            return {"code": 0, "data": {"message_id": "ok-1"}}

        orig = lark_im._im_post
        lark_im._im_post = mock_post
        try:
            mid = lark_im.send_message("tok", "chat-1", self._make_v2_card())
            self.assertEqual(mid, "ok-1")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0], "interactive")
        finally:
            lark_im._im_post = orig

    def test_send_message_v2_fails_v1_succeeds(self):
        """V2 card fails with 230099, V1 fallback succeeds."""
        calls = []

        def mock_post(url, token, payload):
            calls.append(payload.get("msg_type"))
            if len(calls) == 1:
                return {"code": 230099, "msg": "Failed to create card content"}
            return {"code": 0, "data": {"message_id": "v1-ok"}}

        orig = lark_im._im_post
        lark_im._im_post = mock_post
        try:
            mid = lark_im.send_message("tok", "chat-1", self._make_v2_card())
            self.assertEqual(mid, "v1-ok")
            self.assertEqual(len(calls), 2)
            # Both attempts are interactive (card)
            self.assertEqual(calls[0], "interactive")
            self.assertEqual(calls[1], "interactive")
        finally:
            lark_im._im_post = orig

    def test_send_message_v2_and_v1_fail_text_succeeds(self):
        """Both V2 and V1 fail, text fallback succeeds."""
        calls = []

        def mock_post(url, token, payload):
            calls.append(payload.get("msg_type"))
            if payload.get("msg_type") == "interactive":
                return {"code": 230099, "msg": "Failed to create card content"}
            return {"code": 0, "data": {"message_id": "text-ok"}}

        orig = lark_im._im_post
        lark_im._im_post = mock_post
        try:
            mid = lark_im.send_message("tok", "chat-1", self._make_v2_card())
            self.assertEqual(mid, "text-ok")
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[2], "text")
        finally:
            lark_im._im_post = orig

    def test_send_message_all_fail_raises(self):
        """All three attempts fail — raises RuntimeError."""
        def mock_post(url, token, payload):
            return {"code": 230099, "msg": "Failed"}

        orig = lark_im._im_post
        lark_im._im_post = mock_post
        try:
            with self.assertRaises(RuntimeError):
                lark_im.send_message("tok", "chat-1", self._make_v2_card())
        finally:
            lark_im._im_post = orig

    def test_send_message_non_230099_error_raises_immediately(self):
        """Non-230099 error does not trigger fallback chain."""
        calls = []

        def mock_post(url, token, payload):
            calls.append(1)
            return {"code": 99999, "msg": "Other error"}

        orig = lark_im._im_post
        lark_im._im_post = mock_post
        try:
            with self.assertRaises(RuntimeError):
                lark_im.send_message("tok", "chat-1", self._make_v2_card())
            # Only one call — no fallback attempted
            self.assertEqual(len(calls), 1)
        finally:
            lark_im._im_post = orig

    def test_send_message_text_fallback_contains_unavailable_note(self):
        """Text fallback message includes degradation note."""
        sent_payloads = []

        def mock_post(url, token, payload):
            sent_payloads.append(payload)
            if payload.get("msg_type") == "interactive":
                return {"code": 230099, "msg": "Failed"}
            return {"code": 0, "data": {"message_id": "t-1"}}

        orig = lark_im._im_post
        lark_im._im_post = mock_post
        try:
            lark_im.send_message(
                "tok", "chat-1", self._make_v2_card("Hi", "Hello")
            )
            text_payload = sent_payloads[-1]
            import json
            content = json.loads(text_payload["content"])
            self.assertIn("Lark Card V2 down", content["text"])
            self.assertIn("Hello", content["text"])
        finally:
            lark_im._im_post = orig

    # --- reply_message fallback chain ---

    def test_reply_message_v2_fails_v1_succeeds(self):
        """reply_message also has the fallback chain."""
        calls = []

        def mock_post(url, token, payload):
            calls.append(payload.get("msg_type"))
            if len(calls) == 1:
                return {"code": 230099, "msg": "Failed"}
            return {"code": 0, "data": {"message_id": "reply-v1"}}

        orig = lark_im._im_post
        lark_im._im_post = mock_post
        try:
            mid = lark_im.reply_message("tok", "msg-1", self._make_v2_card())
            self.assertEqual(mid, "reply-v1")
            self.assertEqual(len(calls), 2)
        finally:
            lark_im._im_post = orig

    def test_reply_message_all_fail_raises(self):
        """reply_message raises RuntimeError when all fallbacks fail."""
        def mock_post(url, token, payload):
            return {"code": 230099, "msg": "Failed"}

        orig = lark_im._im_post
        lark_im._im_post = mock_post
        try:
            with self.assertRaises(RuntimeError):
                lark_im.reply_message("tok", "msg-1", self._make_v2_card())
        finally:
            lark_im._im_post = orig


class ImPostHTTPErrorTest(unittest.TestCase):
    """Tests that _im_post handles HTTP error responses correctly."""

    def test_im_post_reads_json_from_http_error(self):
        """HTTP 400 with JSON body should return the JSON, not raise."""
        import urllib.error
        import io

        orig_urlopen = lark_im.urllib.request.urlopen

        def mock_urlopen(req):
            body = json.dumps({"code": 230099, "msg": "Failed"}).encode()
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request",
                {"Content-Type": "application/json"},
                io.BytesIO(body),
            )

        lark_im.urllib.request.urlopen = mock_urlopen
        try:
            data = lark_im._im_post("https://example.com", "tok", {})
            self.assertEqual(data["code"], 230099)
        finally:
            lark_im.urllib.request.urlopen = orig_urlopen

    def test_im_post_reraises_if_no_json_body(self):
        """HTTP error with non-JSON body should re-raise the HTTPError."""
        import urllib.error
        import io

        orig_urlopen = lark_im.urllib.request.urlopen

        def mock_urlopen(req):
            raise urllib.error.HTTPError(
                req.full_url, 500, "Internal Server Error",
                {}, io.BytesIO(b"not json"),
            )

        lark_im.urllib.request.urlopen = mock_urlopen
        try:
            with self.assertRaises(urllib.error.HTTPError):
                lark_im._im_post("https://example.com", "tok", {})
        finally:
            lark_im.urllib.request.urlopen = orig_urlopen

    def test_send_message_fallback_on_http_400(self):
        """Full integration: HTTP 400 with 230099 triggers V1 fallback."""
        import urllib.error
        import io

        calls = []
        orig_urlopen = lark_im.urllib.request.urlopen

        def mock_urlopen(req):
            calls.append(1)
            if len(calls) == 1:
                body = json.dumps({"code": 230099, "msg": "Failed"}).encode()
                raise urllib.error.HTTPError(
                    req.full_url, 400, "Bad Request",
                    {"Content-Type": "application/json"},
                    io.BytesIO(body),
                )
            # Second call succeeds
            class FakeResp:
                def read(self):
                    return json.dumps(
                        {"code": 0, "data": {"message_id": "ok-v1"}}
                    ).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    pass
            return FakeResp()

        lark_im.urllib.request.urlopen = mock_urlopen
        try:
            card = {
                "schema": "2.0",
                "header": {
                    "title": {"tag": "plain_text", "content": "T"},
                    "template": "blue",
                },
                "body": {"elements": [{"tag": "markdown", "content": "Hi"}]},
            }
            mid = lark_im.send_message("tok", "chat-1", card)
            self.assertEqual(mid, "ok-v1")
            self.assertEqual(len(calls), 2)
        finally:
            lark_im.urllib.request.urlopen = mock_urlopen


if __name__ == "__main__":
    unittest.main()
