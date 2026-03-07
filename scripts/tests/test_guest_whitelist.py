#!/usr/bin/env python3
"""Tests for the sidecar guest whitelist feature.

Covers:
- filter_by_allowed_senders() — operator + guest message filtering with privilege tags
- Guest CRUD in lark_im (get_guests, set_guests, add_guests, remove_guests)
- Guest field in session storage (get_session returns parsed guests)
- Integration: allowed senders → bot interactions filter chain with guests
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

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
            ("HANDOFF_SESSION_TOOL", self._old_tool),
        ]:
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self.tmp.cleanup()


# ---------------------------------------------------------------------------
# filter_by_allowed_senders()
# ---------------------------------------------------------------------------

class FilterByAllowedSendersTest(unittest.TestCase):
    """Tests for wait_for_reply.filter_by_allowed_senders()."""

    def test_operator_tagged_as_owner(self):
        """Operator messages get privilege='owner'."""
        replies = [{"sender_id": "op1", "text": "hello"}]
        result = wait_for_reply.filter_by_allowed_senders(replies, "op1", {})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["privilege"], "owner")

    def test_guest_tagged_as_guest(self):
        """Guest messages get privilege='guest'."""
        replies = [{"sender_id": "g1", "text": "hi"}]
        result = wait_for_reply.filter_by_allowed_senders(
            replies, "op1", {"g1": "guest"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["privilege"], "guest")

    def test_coowner_tagged_as_coowner(self):
        """Coowner messages get privilege='coowner'."""
        replies = [{"sender_id": "co1", "text": "hi"}]
        result = wait_for_reply.filter_by_allowed_senders(
            replies, "op1", {"co1": "coowner"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["privilege"], "coowner")

    def test_unknown_sender_excluded(self):
        """Messages from non-operator, non-member senders are excluded."""
        replies = [{"sender_id": "stranger", "text": "hey"}]
        result = wait_for_reply.filter_by_allowed_senders(
            replies, "op1", {"g1": "guest"})
        self.assertEqual(len(result), 0)

    def test_mixed_senders(self):
        """Operator, guest, coowner, and stranger are filtered and tagged correctly."""
        replies = [
            {"sender_id": "op1", "text": "owner msg"},
            {"sender_id": "g1", "text": "guest msg"},
            {"sender_id": "co1", "text": "coowner msg"},
            {"sender_id": "stranger", "text": "excluded"},
        ]
        result = wait_for_reply.filter_by_allowed_senders(
            replies, "op1", {"g1": "guest", "co1": "coowner"})
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["privilege"], "owner")
        self.assertEqual(result[1]["privilege"], "guest")
        self.assertEqual(result[2]["privilege"], "coowner")

    def test_empty_operator_and_members(self):
        """With no operator and no members, all replies pass through."""
        replies = [
            {"sender_id": "u1", "text": "a"},
            {"sender_id": "u2", "text": "b"},
        ]
        result = wait_for_reply.filter_by_allowed_senders(replies, "", {})
        self.assertEqual(len(result), 2)

    def test_none_operator_with_members(self):
        """None operator — only member messages pass."""
        replies = [
            {"sender_id": "g1", "text": "guest"},
            {"sender_id": "other", "text": "nope"},
        ]
        result = wait_for_reply.filter_by_allowed_senders(
            replies, None, {"g1": "guest"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["privilege"], "guest")

    def test_empty_replies(self):
        """Empty input returns empty output."""
        result = wait_for_reply.filter_by_allowed_senders(
            [], "op1", {"g1": "guest"})
        self.assertEqual(len(result), 0)

    def test_missing_sender_id_excluded(self):
        """Messages without sender_id are excluded."""
        replies = [{"text": "no sender"}]
        result = wait_for_reply.filter_by_allowed_senders(
            replies, "op1", {"g1": "guest"})
        self.assertEqual(len(result), 0)

    def test_original_reply_not_mutated(self):
        """filter_by_allowed_senders creates new dicts, not mutating originals."""
        original = {"sender_id": "op1", "text": "hello"}
        wait_for_reply.filter_by_allowed_senders([original], "op1", {})
        self.assertNotIn("privilege", original)

    def test_operator_is_also_in_member_roles(self):
        """If operator's open_id is also in member_roles, they still get 'owner'."""
        replies = [{"sender_id": "op1", "text": "hi"}]
        result = wait_for_reply.filter_by_allowed_senders(
            replies, "op1", {"op1": "guest", "g1": "guest"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["privilege"], "owner")

    def test_role_defaults_to_guest_for_backward_compat(self):
        """Members with no explicit role default to guest."""
        replies = [{"sender_id": "g1", "text": "hi"}]
        # Passing a dict where role is already "guest" (backward compat check)
        result = wait_for_reply.filter_by_allowed_senders(
            replies, "op1", {"g1": "guest"})
        self.assertEqual(result[0]["privilege"], "guest")


# ---------------------------------------------------------------------------
# Guest CRUD — lark_im functions
# ---------------------------------------------------------------------------

class GuestCrudTest(_DbTestCase):
    """Tests for guest whitelist CRUD functions in lark_im."""

    def _create_session(self, session_id="s1", chat_id="chat-1"):
        handoff_db.register_session(session_id, chat_id, "opus")

    def test_get_guests_empty_by_default(self):
        """New session has an empty guest list."""
        self._create_session()
        guests = handoff_db.get_guests("s1")
        self.assertEqual(guests, [])

    def test_get_guests_nonexistent_session(self):
        """Nonexistent session returns empty list."""
        guests = handoff_db.get_guests("nonexistent")
        self.assertEqual(guests, [])

    def test_set_guests_and_get(self):
        """set_guests stores and get_guests retrieves correctly."""
        self._create_session()
        guest_list = [
            {"open_id": "ou_alice", "name": "Alice"},
            {"open_id": "ou_bob", "name": "Bob"},
        ]
        handoff_db.set_guests("s1", guest_list)
        result = handoff_db.get_guests("s1")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["open_id"], "ou_alice")
        self.assertEqual(result[1]["open_id"], "ou_bob")

    def test_set_guests_overwrites(self):
        """set_guests replaces the entire list."""
        self._create_session()
        handoff_db.set_guests("s1", [{"open_id": "ou_a", "name": "A"}])
        handoff_db.set_guests("s1", [{"open_id": "ou_b", "name": "B"}])
        result = handoff_db.get_guests("s1")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["open_id"], "ou_b")

    def test_add_guests_new(self):
        """add_guests adds new guests to an empty list."""
        self._create_session()
        added, current = handoff_db.add_guests("s1", [
            {"open_id": "ou_alice", "name": "Alice"},
        ])
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["name"], "Alice")
        self.assertEqual(len(current), 1)

    def test_add_guests_skip_duplicates(self):
        """add_guests skips guests already in the list."""
        self._create_session()
        handoff_db.set_guests("s1", [{"open_id": "ou_alice", "name": "Alice"}])
        added, current = handoff_db.add_guests("s1", [
            {"open_id": "ou_alice", "name": "Alice"},
            {"open_id": "ou_bob", "name": "Bob"},
        ])
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["name"], "Bob")
        self.assertEqual(len(current), 2)

    def test_add_guests_all_duplicates(self):
        """add_guests with all duplicates returns empty added list."""
        self._create_session()
        handoff_db.set_guests("s1", [{"open_id": "ou_alice", "name": "Alice"}])
        added, current = handoff_db.add_guests("s1", [
            {"open_id": "ou_alice", "name": "Alice"},
        ])
        self.assertEqual(len(added), 0)
        self.assertEqual(len(current), 1)

    def test_remove_guests_existing(self):
        """remove_guests removes matching guests by open_id."""
        self._create_session()
        handoff_db.set_guests("s1", [
            {"open_id": "ou_alice", "name": "Alice"},
            {"open_id": "ou_bob", "name": "Bob"},
        ])
        removed, remaining = handoff_db.remove_guests("s1", {"ou_alice"})
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["name"], "Alice")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["name"], "Bob")

    def test_remove_guests_not_found(self):
        """remove_guests with non-matching IDs returns empty removed list."""
        self._create_session()
        handoff_db.set_guests("s1", [{"open_id": "ou_alice", "name": "Alice"}])
        removed, remaining = handoff_db.remove_guests("s1", {"ou_unknown"})
        self.assertEqual(len(removed), 0)
        self.assertEqual(len(remaining), 1)

    def test_remove_guests_all(self):
        """remove_guests can remove all guests."""
        self._create_session()
        handoff_db.set_guests("s1", [
            {"open_id": "ou_a", "name": "A"},
            {"open_id": "ou_b", "name": "B"},
        ])
        removed, remaining = handoff_db.remove_guests("s1", {"ou_a", "ou_b"})
        self.assertEqual(len(removed), 2)
        self.assertEqual(len(remaining), 0)
        # Verify DB is updated
        self.assertEqual(handoff_db.get_guests("s1"), [])

    def test_add_guests_multiple(self):
        """add_guests can add multiple guests at once."""
        self._create_session()
        added, current = handoff_db.add_guests("s1", [
            {"open_id": "ou_a", "name": "A"},
            {"open_id": "ou_b", "name": "B"},
            {"open_id": "ou_c", "name": "C"},
        ])
        self.assertEqual(len(added), 3)
        self.assertEqual(len(current), 3)

    def test_set_guests_unicode_names(self):
        """Guests with unicode names are stored correctly."""
        self._create_session()
        handoff_db.set_guests("s1", [
            {"open_id": "ou_jack", "name": "小明"},
        ])
        result = handoff_db.get_guests("s1")
        self.assertEqual(result[0]["name"], "小明")


# ---------------------------------------------------------------------------
# Guest field in session storage
# ---------------------------------------------------------------------------

class SessionGuestFieldTest(_DbTestCase):
    """Tests for guests field in session storage."""

    def test_get_session_returns_guests(self):
        """get_session includes parsed guests list."""
        handoff_db.register_session("s1", "chat-1", "opus")
        handoff_db.set_guests("s1", [
            {"open_id": "ou_alice", "name": "Alice"},
        ])
        sess = handoff_db.get_session("s1")
        self.assertIsNotNone(sess)
        self.assertEqual(len(sess["guests"]), 1)
        self.assertEqual(sess["guests"][0]["open_id"], "ou_alice")

    def test_get_session_empty_guests_default(self):
        """New session returns empty guests list in get_session."""
        handoff_db.register_session("s1", "chat-1", "opus")
        sess = handoff_db.get_session("s1")
        self.assertIsNotNone(sess)
        self.assertEqual(sess["guests"], [])

    def test_get_session_invalid_json_guests(self):
        """Corrupted guests JSON returns empty list."""
        handoff_db.register_session("s1", "chat-1", "opus")
        # Manually corrupt the guests column
        conn = handoff_db._get_db()
        try:
            conn.execute(
                "UPDATE sessions SET guests = ? WHERE session_id = ?",
                ("not valid json", "s1"),
            )
            conn.commit()
        finally:
            conn.close()
        sess = handoff_db.get_session("s1")
        self.assertEqual(sess["guests"], [])

    def test_guests_persist_across_get_guests_calls(self):
        """Guests set via set_guests are returned by get_guests."""
        handoff_db.register_session("s1", "chat-1", "opus")
        handoff_db.set_guests("s1", [{"open_id": "ou_x", "name": "X"}])

        # get_guests returns the same data
        g1 = handoff_db.get_guests("s1")
        g2 = handoff_db.get_guests("s1")
        self.assertEqual(g1, g2)

    def test_guests_independent_per_session(self):
        """Different sessions have independent guest lists."""
        handoff_db.register_session("s1", "chat-1", "opus")
        handoff_db.register_session("s2", "chat-2", "opus")
        handoff_db.set_guests("s1", [{"open_id": "ou_a", "name": "A"}])
        handoff_db.set_guests("s2", [{"open_id": "ou_b", "name": "B"}])

        g1 = handoff_db.get_guests("s1")
        g2 = handoff_db.get_guests("s2")
        self.assertEqual(g1[0]["name"], "A")
        self.assertEqual(g2[0]["name"], "B")


# ---------------------------------------------------------------------------
# Integration: allowed senders + bot interactions filter chain with guests
# ---------------------------------------------------------------------------

class GuestFilterChainIntegrationTest(unittest.TestCase):
    """Integration tests for the filter chain with guest whitelist."""

    def setUp(self):
        self._orig_is_bot = handoff_db.is_bot_sent_message
        self._bot_sent_ids = set()
        handoff_db.is_bot_sent_message = lambda mid: mid in self._bot_sent_ids

    def tearDown(self):
        handoff_db.is_bot_sent_message = self._orig_is_bot

    def test_guest_message_passes_both_filters(self):
        """Guest @-mentioning the bot passes allowed_senders then bot_interactions."""
        replies = [
            {
                "sender_id": "g1", "text": "@Bot help me",
                "mentions": [{"id": "bot1", "key": "@Bot"}],
            },
        ]
        # Step 1: allowed senders
        after_senders = wait_for_reply.filter_by_allowed_senders(
            replies, "op1", {"g1": "guest"})
        self.assertEqual(len(after_senders), 1)
        self.assertEqual(after_senders[0]["privilege"], "guest")

        # Step 2: bot interactions
        after_bot = wait_for_reply.filter_bot_interactions(after_senders, "bot1")
        self.assertEqual(len(after_bot), 1)
        self.assertEqual(after_bot[0]["text"], "help me")
        self.assertEqual(after_bot[0]["privilege"], "guest")

    def test_guest_without_bot_mention_excluded_by_bot_filter(self):
        """Guest message without bot interaction passes sender filter but not bot filter."""
        replies = [
            {"sender_id": "g1", "text": "random chat"},
        ]
        after_senders = wait_for_reply.filter_by_allowed_senders(
            replies, "op1", {"g1": "guest"})
        self.assertEqual(len(after_senders), 1)

        after_bot = wait_for_reply.filter_bot_interactions(after_senders, "bot1")
        self.assertEqual(len(after_bot), 0)

    def test_operator_and_guest_mixed_through_chain(self):
        """Mixed operator, guest, and coowner messages through full filter chain."""
        self._bot_sent_ids.add("bot-msg")
        replies = [
            # Operator @-mentions bot -> passes both
            {
                "sender_id": "op1", "text": "@Bot do this",
                "mentions": [{"id": "bot1", "key": "@Bot"}],
            },
            # Guest @-mentions bot -> passes both
            {
                "sender_id": "g1", "text": "@Bot question",
                "mentions": [{"id": "bot1", "key": "@Bot"}],
            },
            # Stranger @-mentions bot -> fails sender filter
            {
                "sender_id": "stranger", "text": "@Bot hack",
                "mentions": [{"id": "bot1", "key": "@Bot"}],
            },
            # Coowner replies to bot -> passes both
            {"sender_id": "co1", "text": "thanks", "parent_id": "bot-msg"},
            # Guest random chat -> fails bot filter
            {"sender_id": "g1", "text": "random"},
        ]

        after_senders = wait_for_reply.filter_by_allowed_senders(
            replies, "op1", {"g1": "guest", "co1": "coowner"})
        self.assertEqual(len(after_senders), 4)  # excludes stranger

        after_bot = wait_for_reply.filter_bot_interactions(after_senders, "bot1")
        self.assertEqual(len(after_bot), 3)  # excludes random chat

        # Verify privileges preserved
        privileges = [r["privilege"] for r in after_bot]
        self.assertEqual(privileges, ["owner", "guest", "coowner"])

    def test_reaction_from_guest_passes_full_chain(self):
        """Guest reaction passes both filters."""
        replies = [
            {"sender_id": "g1", "msg_type": "reaction", "text": "THUMBSUP"},
        ]
        after_senders = wait_for_reply.filter_by_allowed_senders(
            replies, "op1", {"g1": "guest"})
        self.assertEqual(len(after_senders), 1)

        after_bot = wait_for_reply.filter_bot_interactions(after_senders, "bot1")
        self.assertEqual(len(after_bot), 1)

    def test_no_members_falls_back_to_operator_only(self):
        """With empty member_roles, filter_by_allowed_senders acts like filter_by_operator."""
        replies = [
            {"sender_id": "op1", "text": "owner"},
            {"sender_id": "g1", "text": "guest"},
        ]
        result = wait_for_reply.filter_by_allowed_senders(
            replies, "op1", {})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["privilege"], "owner")

    def test_coowner_passes_without_bot_filter_in_regular_mode(self):
        """In regular mode (no sidecar), coowner messages pass sender filter directly."""
        replies = [
            {"sender_id": "co1", "text": "do this"},
            {"sender_id": "op1", "text": "ok"},
        ]
        result = wait_for_reply.filter_by_allowed_senders(
            replies, "op1", {"co1": "coowner"})
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["privilege"], "coowner")
        self.assertEqual(result[1]["privilege"], "owner")


if __name__ == "__main__":
    unittest.main()
