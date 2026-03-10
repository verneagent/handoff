#!/usr/bin/env python3
"""Tests for sidecar-mode filters, operator filtering, and session sidecar_mode/bot_open_id fields.

Covers:
- filter_by_operator() — operator-level message filtering
- filter_bot_interactions() — expanded sidecar-mode filter (3 conditions)
- is_bot_sent_message() — parent message lookup in local DB
- sidecar_mode / bot_open_id session fields — stored and read correctly
"""

import os
import sqlite3
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

import handoff_config
import handoff_db
import wait_for_reply


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _DbTestCase(unittest.TestCase):
    """Base class that sets up a temporary handoff DB."""

    def setUp(self):
        self._old_home = os.environ.get("HOME")
        self._old_project = os.environ.get("HANDOFF_PROJECT_DIR")
        self._old_tool = os.environ.get("HANDOFF_SESSION_TOOL")
        self._old_handoff_home = handoff_config.HANDOFF_HOME

        self.tmp = tempfile.TemporaryDirectory()
        self.project_dir = os.path.join(self.tmp.name, "project")
        os.makedirs(self.project_dir, exist_ok=True)

        os.environ["HOME"] = self.tmp.name
        os.environ["HANDOFF_PROJECT_DIR"] = self.project_dir
        os.environ["HANDOFF_SESSION_TOOL"] = "Claude Code"
        handoff_config.HANDOFF_HOME = os.path.join(self.tmp.name, ".handoff")

        self.db_path = handoff_db._db_path()
        handoff_db._db_initialized.discard(self.db_path)
        conn = handoff_db._get_db()
        conn.close()

    def tearDown(self):
        for key, val in [
            ("HOME", self._old_home),
            ("HANDOFF_PROJECT_DIR", self._old_project),
            ("HANDOFF_SESSION_TOOL", self._old_tool),
        ]:
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        handoff_config.HANDOFF_HOME = self._old_handoff_home
        self.tmp.cleanup()


# ---------------------------------------------------------------------------
# filter_by_operator()
# ---------------------------------------------------------------------------

class FilterByOperatorTest(unittest.TestCase):
    """Tests for wait_for_reply.filter_by_operator()."""

    def test_no_operator_returns_all(self):
        """When operator_open_id is empty, all replies pass through."""
        replies = [
            {"sender_id": "u1", "text": "hello"},
            {"sender_id": "u2", "text": "world"},
        ]
        result = wait_for_reply.filter_by_operator(replies, "")
        self.assertEqual(len(result), 2)

    def test_none_operator_returns_all(self):
        """When operator_open_id is None, all replies pass through."""
        replies = [{"sender_id": "u1", "text": "hello"}]
        result = wait_for_reply.filter_by_operator(replies, None)
        self.assertEqual(len(result), 1)

    def test_filters_to_matching_operator(self):
        """Only messages from the operator's open_id are returned."""
        replies = [
            {"sender_id": "op1", "text": "from operator"},
            {"sender_id": "other", "text": "from someone else"},
            {"sender_id": "op1", "text": "another from operator"},
        ]
        result = wait_for_reply.filter_by_operator(replies, "op1")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["text"], "from operator")
        self.assertEqual(result[1]["text"], "another from operator")

    def test_no_match_returns_empty(self):
        """When no messages match the operator, empty list is returned."""
        replies = [
            {"sender_id": "u1", "text": "hello"},
            {"sender_id": "u2", "text": "world"},
        ]
        result = wait_for_reply.filter_by_operator(replies, "op_missing")
        self.assertEqual(len(result), 0)

    def test_missing_sender_id_excluded(self):
        """Messages without sender_id don't match any operator."""
        replies = [
            {"text": "no sender"},
            {"sender_id": "op1", "text": "has sender"},
        ]
        result = wait_for_reply.filter_by_operator(replies, "op1")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "has sender")


# ---------------------------------------------------------------------------
# filter_bot_interactions()
# ---------------------------------------------------------------------------

class FilterBotInteractionsTest(unittest.TestCase):
    """Tests for wait_for_reply.filter_bot_interactions()."""

    def setUp(self):
        # Mock is_bot_sent_message to avoid needing a real DB
        self._orig_is_bot = handoff_db.is_bot_sent_message
        self._bot_sent_ids = set()
        handoff_db.is_bot_sent_message = lambda mid: mid in self._bot_sent_ids

    def tearDown(self):
        handoff_db.is_bot_sent_message = self._orig_is_bot

    # --- Condition 1: @-mention ---

    def test_at_mention_passes(self):
        """Message that @-mentions the bot passes the filter."""
        replies = [
            {
                "text": "@Bot hello there",
                "mentions": [{"id": "bot1", "key": "@Bot"}],
            }
        ]
        result = wait_for_reply.filter_bot_interactions(replies, "bot1")
        self.assertEqual(len(result), 1)

    def test_at_mention_strips_marker(self):
        """@-mention markers are stripped from the text."""
        replies = [
            {
                "text": "@Bot what is this?",
                "mentions": [{"id": "bot1", "key": "@Bot"}],
            }
        ]
        result = wait_for_reply.filter_bot_interactions(replies, "bot1")
        self.assertEqual(result[0]["text"], "what is this?")

    def test_at_mention_strips_multiple_markers(self):
        """Multiple @-mention markers are all stripped."""
        replies = [
            {
                "text": "@Bot @Bot do something",
                "mentions": [
                    {"id": "bot1", "key": "@Bot"},
                    {"id": "bot1", "key": "@Bot"},
                ],
            }
        ]
        result = wait_for_reply.filter_bot_interactions(replies, "bot1")
        self.assertEqual(result[0]["text"], "do something")

    def test_at_mention_wrong_bot_excluded(self):
        """@-mention of a different bot does not pass."""
        replies = [
            {
                "text": "@OtherBot hello",
                "mentions": [{"id": "other_bot", "key": "@OtherBot"}],
            }
        ]
        result = wait_for_reply.filter_bot_interactions(replies, "bot1")
        self.assertEqual(len(result), 0)

    def test_no_mentions_no_parent_excluded(self):
        """Message with no mentions and no parent_id is excluded."""
        replies = [{"text": "random message"}]
        result = wait_for_reply.filter_bot_interactions(replies, "bot1")
        self.assertEqual(len(result), 0)

    def test_empty_bot_open_id_no_mention_match(self):
        """With empty bot_open_id, @-mention condition cannot match."""
        replies = [
            {
                "text": "@Bot hello",
                "mentions": [{"id": "bot1", "key": "@Bot"}],
            }
        ]
        result = wait_for_reply.filter_bot_interactions(replies, "")
        self.assertEqual(len(result), 0)

    # --- Condition 2: reply to bot-sent message ---

    def test_reply_to_bot_message_passes(self):
        """Message replying to a bot-sent message passes."""
        self._bot_sent_ids.add("msg-from-bot")
        replies = [
            {"text": "thanks", "parent_id": "msg-from-bot"},
        ]
        result = wait_for_reply.filter_bot_interactions(replies, "bot1")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "thanks")

    def test_reply_to_non_bot_message_excluded(self):
        """Message replying to a non-bot message is excluded."""
        replies = [
            {"text": "reply", "parent_id": "msg-from-human"},
        ]
        result = wait_for_reply.filter_bot_interactions(replies, "bot1")
        self.assertEqual(len(result), 0)

    def test_reply_to_bot_no_mention_stripping(self):
        """Reply-to-bot messages don't have @-mention stripping applied."""
        self._bot_sent_ids.add("bot-msg-1")
        replies = [
            {"text": "@Bot something", "parent_id": "bot-msg-1"},
        ]
        result = wait_for_reply.filter_bot_interactions(replies, "bot1")
        self.assertEqual(len(result), 1)
        # Text unchanged because no @-mention match (no mentions field)
        self.assertEqual(result[0]["text"], "@Bot something")

    def test_reply_to_bot_with_mention_strips(self):
        """Reply-to-bot + @-mention strips the marker."""
        self._bot_sent_ids.add("bot-msg-1")
        replies = [
            {
                "text": "@Bot something",
                "parent_id": "bot-msg-1",
                "mentions": [{"id": "bot1", "key": "@Bot"}],
            }
        ]
        result = wait_for_reply.filter_bot_interactions(replies, "bot1")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "something")

    # --- Condition 3: reaction/sticker ---

    def test_reaction_passes(self):
        """Reaction messages always pass the filter."""
        replies = [
            {"msg_type": "reaction", "text": "THUMBSUP"},
        ]
        result = wait_for_reply.filter_bot_interactions(replies, "bot1")
        self.assertEqual(len(result), 1)

    def test_sticker_passes(self):
        """Sticker messages always pass the filter."""
        replies = [
            {"msg_type": "sticker", "text": ""},
        ]
        result = wait_for_reply.filter_bot_interactions(replies, "bot1")
        self.assertEqual(len(result), 1)

    def test_reaction_passes_even_without_bot_id(self):
        """Reactions pass even when bot_open_id is empty."""
        replies = [
            {"msg_type": "reaction", "text": "THUMBSUP"},
        ]
        result = wait_for_reply.filter_bot_interactions(replies, "")
        self.assertEqual(len(result), 1)

    # --- Mixed scenarios ---

    def test_mixed_messages_filtered(self):
        """Multiple messages: only bot-directed ones pass."""
        self._bot_sent_ids.add("bot-msg")
        replies = [
            {"text": "random message"},  # excluded
            {"msg_type": "reaction", "text": "LIKE"},  # passes (condition 3)
            {  # passes (condition 1)
                "text": "@Bot help",
                "mentions": [{"id": "bot1", "key": "@Bot"}],
            },
            {"text": "reply to bot", "parent_id": "bot-msg"},  # passes (condition 2)
            {"text": "reply to human", "parent_id": "human-msg"},  # excluded
        ]
        result = wait_for_reply.filter_bot_interactions(replies, "bot1")
        self.assertEqual(len(result), 3)

    def test_empty_replies_returns_empty(self):
        """Empty input returns empty output."""
        result = wait_for_reply.filter_bot_interactions([], "bot1")
        self.assertEqual(len(result), 0)

    def test_original_reply_not_mutated(self):
        """filter_bot_interactions should not mutate the original reply dict."""
        original = {
            "text": "@Bot hello world",
            "mentions": [{"id": "bot1", "key": "@Bot"}],
        }
        original_text = original["text"]
        wait_for_reply.filter_bot_interactions([original], "bot1")
        # Original dict should be unchanged
        self.assertEqual(original["text"], original_text)


# ---------------------------------------------------------------------------
# is_bot_sent_message() — requires real DB
# ---------------------------------------------------------------------------

class IsBotSentMessageTest(_DbTestCase):
    """Tests for handoff_db.is_bot_sent_message()."""

    def test_sent_message_found_by_message_id(self):
        """Bot-sent message found by its primary message_id."""
        handoff_db.record_sent_message(
            "internal-1", text="hello", title="", chat_id="chat-1"
        )
        self.assertTrue(handoff_db.is_bot_sent_message("internal-1"))

    def test_sent_message_found_by_source_message_id(self):
        """Bot-sent message found by its source_message_id column.

        record_sent_message stores message_id as both PK and source_message_id,
        so querying by source_message_id also matches.
        """
        handoff_db.record_sent_message(
            "lark-msg-id", text="hello", title="", chat_id="chat-1"
        )
        # is_bot_sent_message checks both message_id and source_message_id
        self.assertTrue(handoff_db.is_bot_sent_message("lark-msg-id"))

    def test_received_message_not_matched(self):
        """Received (non-bot) messages are not matched."""
        handoff_db.record_received_message(
            chat_id="chat-1", text="user msg",
            source_message_id="user-msg-1", message_time=1700000000000
        )
        self.assertFalse(handoff_db.is_bot_sent_message("user-msg-1"))

    def test_nonexistent_message_returns_false(self):
        """Unknown message_id returns False."""
        self.assertFalse(handoff_db.is_bot_sent_message("nonexistent"))

    def test_empty_message_id_returns_false(self):
        """Empty string returns False without DB query."""
        self.assertFalse(handoff_db.is_bot_sent_message(""))

    def test_none_message_id_returns_false(self):
        """None returns False without DB query."""
        self.assertFalse(handoff_db.is_bot_sent_message(None))


# ---------------------------------------------------------------------------
# Session sidecar_mode / bot_open_id fields
# ---------------------------------------------------------------------------

class SessionBotFieldsTest(_DbTestCase):
    """Tests for sidecar_mode and bot_open_id in session storage."""

    def test_register_session_stores_bot_open_id(self):
        """bot_open_id is stored and retrieved correctly."""
        handoff_db.register_session(
            "s1", "chat-1", "opus", bot_open_id="ou_bot123"
        )
        sess = handoff_db.get_session("s1")
        self.assertIsNotNone(sess)
        self.assertEqual(sess["bot_open_id"], "ou_bot123")

    def test_register_session_stores_sidecar_mode(self):
        """sidecar_mode is stored and retrieved correctly."""
        handoff_db.register_session(
            "s1", "chat-1", "opus", sidecar_mode=True
        )
        sess = handoff_db.get_session("s1")
        self.assertIsNotNone(sess)
        self.assertTrue(sess["sidecar_mode"])

    def test_register_session_defaults_sidecar_mode_false(self):
        """sidecar_mode defaults to False when not specified."""
        handoff_db.register_session("s1", "chat-1", "opus")
        sess = handoff_db.get_session("s1")
        self.assertIsNotNone(sess)
        self.assertFalse(sess["sidecar_mode"])

    def test_register_session_defaults_bot_open_id_empty(self):
        """bot_open_id defaults to empty string when not specified."""
        handoff_db.register_session("s1", "chat-1", "opus")
        sess = handoff_db.get_session("s1")
        self.assertIsNotNone(sess)
        self.assertEqual(sess["bot_open_id"], "")

    def test_register_session_stores_operator_open_id(self):
        """operator_open_id is stored and retrieved correctly."""
        handoff_db.register_session(
            "s1", "chat-1", "opus", operator_open_id="ou_op456"
        )
        sess = handoff_db.get_session("s1")
        self.assertIsNotNone(sess)
        self.assertEqual(sess["operator_open_id"], "ou_op456")

    def test_activate_handoff_passes_bot_fields(self):
        """activate_handoff forwards sidecar_mode and bot_open_id to register_session."""
        handoff_db.activate_handoff(
            "s2", "chat-2", session_model="opus",
            operator_open_id="ou_op", bot_open_id="ou_bot", sidecar_mode=True,
        )
        sess = handoff_db.get_session("s2")
        self.assertIsNotNone(sess)
        self.assertEqual(sess["operator_open_id"], "ou_op")
        self.assertEqual(sess["bot_open_id"], "ou_bot")
        self.assertTrue(sess["sidecar_mode"])

    def test_takeover_preserves_bot_fields(self):
        """takeover_chat stores bot_open_id for the new session."""
        handoff_db.register_session("old", "chat-tk", "opus")
        ok, owner, replaced = handoff_db.takeover_chat(
            "new", "chat-tk", "sonnet",
            expected_owner_session_id="old",
            bot_open_id="ou_newbot",
        )
        self.assertTrue(ok)
        sess = handoff_db.get_session("new")
        self.assertIsNotNone(sess)
        self.assertEqual(sess["bot_open_id"], "ou_newbot")

    def test_get_active_sessions_includes_bot_fields(self):
        """get_active_sessions returns sidecar_mode and bot_open_id."""
        handoff_db.register_session(
            "s1", "chat-1", "opus",
            bot_open_id="ou_bot", sidecar_mode=True,
        )
        sessions = handoff_db.get_active_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["bot_open_id"], "ou_bot")
        self.assertTrue(sessions[0]["sidecar_mode"])


# ---------------------------------------------------------------------------
# Integration: filter chain (operator -> bot interactions)
# ---------------------------------------------------------------------------

class FilterChainIntegrationTest(unittest.TestCase):
    """Integration tests for the two-tier filter chain."""

    def setUp(self):
        self._orig_is_bot = handoff_db.is_bot_sent_message
        self._bot_sent_ids = set()
        handoff_db.is_bot_sent_message = lambda mid: mid in self._bot_sent_ids

    def tearDown(self):
        handoff_db.is_bot_sent_message = self._orig_is_bot

    def test_operator_then_bot_filter(self):
        """Messages are filtered by operator first, then by bot interaction."""
        self._bot_sent_ids.add("bot-msg")
        replies = [
            # From operator, @-mentions bot -> passes both
            {
                "sender_id": "op1", "text": "@Bot help",
                "mentions": [{"id": "bot1", "key": "@Bot"}],
            },
            # From other user, @-mentions bot -> fails operator filter
            {
                "sender_id": "other", "text": "@Bot help",
                "mentions": [{"id": "bot1", "key": "@Bot"}],
            },
            # From operator, no bot interaction -> fails bot filter
            {"sender_id": "op1", "text": "random chat"},
            # From operator, reply to bot -> passes both
            {"sender_id": "op1", "text": "thanks", "parent_id": "bot-msg"},
        ]
        # Step 1: operator filter
        after_op = wait_for_reply.filter_by_operator(replies, "op1")
        self.assertEqual(len(after_op), 3)  # excludes "other" user

        # Step 2: bot interaction filter
        after_bot = wait_for_reply.filter_bot_interactions(after_op, "bot1")
        self.assertEqual(len(after_bot), 2)  # @-mention and reply-to-bot
        texts = [r["text"] for r in after_bot]
        self.assertIn("help", texts)  # "@Bot help" -> stripped to "help"
        self.assertIn("thanks", texts)

    def test_non_sidecar_mode_skips_bot_filter(self):
        """When sidecar_mode is False, only operator filter is applied."""
        replies = [
            {"sender_id": "op1", "text": "hello"},
            {"sender_id": "other", "text": "hi"},
        ]
        after_op = wait_for_reply.filter_by_operator(replies, "op1")
        self.assertEqual(len(after_op), 1)
        # In non-sidecar mode, no bot filter is applied
        self.assertEqual(after_op[0]["text"], "hello")

    def test_non_sidecar_with_guests_no_bot_filter(self):
        """In regular mode with guests, sender filter applies but bot filter does not."""
        replies = [
            {"sender_id": "op1", "text": "owner msg"},
            {"sender_id": "g1", "text": "guest msg"},
            {"sender_id": "co1", "text": "coowner msg"},
            {"sender_id": "stranger", "text": "excluded"},
        ]
        member_roles = {"g1": "guest", "co1": "coowner"}

        # Sender filter applies (member_roles non-empty)
        after_senders = wait_for_reply.filter_by_allowed_senders(
            replies, "op1", member_roles)
        self.assertEqual(len(after_senders), 3)  # excludes stranger
        self.assertEqual(after_senders[0]["privilege"], "owner")
        self.assertEqual(after_senders[1]["privilege"], "guest")
        self.assertEqual(after_senders[2]["privilege"], "coowner")

        # In regular mode (sidecar_mode=False), bot filter is NOT applied,
        # so all 3 messages pass through without needing @-mentions.


if __name__ == "__main__":
    unittest.main()
