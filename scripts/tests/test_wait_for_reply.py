#!/usr/bin/env python3
"""Tests for wait_for_reply.py — reply filters and result handling."""

import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

import wait_for_reply


class FilterSelfBotTest(unittest.TestCase):
    def test_filters_own_bot(self):
        replies = [
            {"text": "bot echo", "sender_type": "app", "sender_id": "ou_bot"},
            {"text": "user msg", "sender_type": "user", "sender_id": "ou_1"},
        ]
        result = wait_for_reply.filter_self_bot(replies, "ou_bot")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "user msg")

    def test_passes_other_bots(self):
        replies = [
            {"text": "other bot", "sender_type": "app", "sender_id": "ou_other_bot"},
            {"text": "user msg", "sender_type": "user", "sender_id": "ou_1"},
        ]
        result = wait_for_reply.filter_self_bot(replies, "ou_bot")
        self.assertEqual(len(result), 2)

    def test_no_bot_id_passes_all(self):
        replies = [
            {"text": "bot msg", "sender_type": "app", "sender_id": "ou_bot"},
        ]
        result = wait_for_reply.filter_self_bot(replies, "")
        self.assertEqual(len(result), 1)


class FilterByOperatorTest(unittest.TestCase):
    def test_no_operator(self):
        """When no operator_open_id, all replies pass through."""
        replies = [{"text": "a", "sender_id": "ou_1"}, {"text": "b", "sender_id": "ou_2"}]
        result = wait_for_reply.filter_by_operator(replies, "")
        self.assertEqual(len(result), 2)

    def test_filters_non_operator(self):
        replies = [
            {"text": "a", "sender_id": "ou_1"},
            {"text": "b", "sender_id": "ou_2"},
        ]
        result = wait_for_reply.filter_by_operator(replies, "ou_1")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "a")


class FilterByAllowedSendersTest(unittest.TestCase):
    def test_operator_gets_owner_privilege(self):
        replies = [{"text": "a", "sender_id": "ou_op"}]
        result = wait_for_reply.filter_by_allowed_senders(
            replies, "ou_op", {"ou_guest": "guest"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["privilege"], "owner")

    def test_guest_gets_guest_privilege(self):
        replies = [{"text": "a", "sender_id": "ou_guest"}]
        result = wait_for_reply.filter_by_allowed_senders(
            replies, "ou_op", {"ou_guest": "guest"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["privilege"], "guest")

    def test_coowner_gets_coowner_privilege(self):
        replies = [{"text": "a", "sender_id": "ou_co"}]
        result = wait_for_reply.filter_by_allowed_senders(
            replies, "ou_op", {"ou_co": "coowner"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["privilege"], "coowner")

    def test_unknown_sender_filtered_out(self):
        replies = [{"text": "a", "sender_id": "ou_unknown"}]
        result = wait_for_reply.filter_by_allowed_senders(
            replies, "ou_op", {"ou_guest": "guest"})
        self.assertEqual(len(result), 0)

    def test_no_allowed_passes_all(self):
        replies = [{"text": "a", "sender_id": "ou_1"}]
        result = wait_for_reply.filter_by_allowed_senders(replies, "", {})
        self.assertEqual(len(result), 1)


class FilterBotInteractionsTest(unittest.TestCase):
    def test_reaction_always_passes(self):
        replies = [{"text": "thumbsup", "msg_type": "reaction", "sender_id": "ou_1"}]
        result = wait_for_reply.filter_bot_interactions(replies, "ou_bot")
        self.assertEqual(len(result), 1)

    def test_sticker_always_passes(self):
        replies = [{"text": "sticker", "msg_type": "sticker", "sender_id": "ou_1"}]
        result = wait_for_reply.filter_bot_interactions(replies, "ou_bot")
        self.assertEqual(len(result), 1)

    def test_mention_passes_and_strips(self):
        replies = [{
            "text": "@bot hello world",
            "msg_type": "text",
            "sender_id": "ou_1",
            "mentions": [{"id": "ou_bot", "key": "@bot"}],
        }]
        result = wait_for_reply.filter_bot_interactions(replies, "ou_bot")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "hello world")

    @patch("wait_for_reply.handoff_db.is_bot_sent_message", return_value=True)
    def test_reply_to_bot_passes(self, _mock):
        replies = [{
            "text": "reply text",
            "msg_type": "text",
            "sender_id": "ou_1",
            "parent_id": "msg_123",
        }]
        result = wait_for_reply.filter_bot_interactions(replies, "ou_bot")
        self.assertEqual(len(result), 1)

    @patch("wait_for_reply.handoff_db.is_bot_sent_message", return_value=False)
    def test_unrelated_message_filtered(self, _mock):
        replies = [{
            "text": "random message",
            "msg_type": "text",
            "sender_id": "ou_1",
        }]
        result = wait_for_reply.filter_bot_interactions(replies, "ou_bot")
        self.assertEqual(len(result), 0)


class OtherBotFilterTest(unittest.TestCase):
    """Other bots (sender_type=app) are treated like regular users — same filter rules apply."""

    def test_other_bot_filtered_by_operator(self):
        """Other bots are filtered out by operator filter (not in whitelist)."""
        replies = [
            {"text": "jenkins msg", "sender_type": "app", "sender_id": "ou_jenkins"},
            {"text": "from op", "sender_type": "user", "sender_id": "ou_op"},
        ]
        result = wait_for_reply.filter_by_operator(replies, "ou_op")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "from op")

    def test_other_bot_passes_as_guest(self):
        """Other bots pass through when added as a guest."""
        replies = [
            {"text": "jenkins msg", "sender_type": "app", "sender_id": "ou_jenkins"},
            {"text": "stranger", "sender_type": "user", "sender_id": "ou_stranger"},
        ]
        result = wait_for_reply.filter_by_allowed_senders(
            replies, "ou_op", {"ou_jenkins": "guest"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "jenkins msg")
        self.assertEqual(result[0]["privilege"], "guest")

    @patch("wait_for_reply.handoff_db.is_bot_sent_message", return_value=False)
    def test_other_bot_filtered_by_bot_interactions(self, _mock):
        """Other bots are subject to bot-interaction filter in sidecar mode."""
        replies = [
            {"text": "jenkins msg", "sender_type": "app", "sender_id": "ou_jenkins", "msg_type": "text"},
        ]
        result = wait_for_reply.filter_bot_interactions(replies, "ou_bot")
        self.assertEqual(len(result), 0)

    def test_other_bot_passes_bot_interactions_with_mention(self):
        """Other bots pass bot-interaction filter when @-mentioning the bot."""
        replies = [{
            "text": "@bot jenkins msg",
            "sender_type": "app",
            "sender_id": "ou_jenkins",
            "msg_type": "text",
            "mentions": [{"id": "ou_bot", "key": "@bot"}],
        }]
        result = wait_for_reply.filter_bot_interactions(replies, "ou_bot")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "jenkins msg")


class RelayFilterTest(unittest.TestCase):
    """Relay messages (sender_type=relay) must pass through all filters."""

    def test_relay_passes_operator_filter(self):
        replies = [
            {"text": "relayed info", "sender_type": "relay", "sender_id": "", "msg_type": "relay"},
            {"text": "other", "sender_type": "user", "sender_id": "ou_other", "msg_type": "text"},
        ]
        result = wait_for_reply.filter_by_operator(replies, "ou_op")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "relayed info")

    def test_relay_passes_allowed_senders_filter(self):
        replies = [
            {"text": "relayed", "sender_type": "relay", "sender_id": "", "msg_type": "relay"},
            {"text": "stranger", "sender_type": "user", "sender_id": "ou_stranger", "msg_type": "text"},
        ]
        result = wait_for_reply.filter_by_allowed_senders(
            replies, "ou_op", {"ou_guest": "guest"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "relayed")

    def test_relay_passes_bot_interactions_filter(self):
        replies = [
            {"text": "relayed", "sender_type": "relay", "sender_id": "", "msg_type": "relay"},
            {"text": "random", "sender_type": "user", "sender_id": "ou_1", "msg_type": "text"},
        ]
        result = wait_for_reply.filter_bot_interactions(replies, "ou_bot")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "relayed")

    def test_relay_coexists_with_operator_messages(self):
        replies = [
            {"text": "from relay", "sender_type": "relay", "sender_id": "", "msg_type": "relay"},
            {"text": "from op", "sender_type": "user", "sender_id": "ou_op", "msg_type": "text"},
        ]
        result = wait_for_reply.filter_by_operator(replies, "ou_op")
        self.assertEqual(len(result), 2)


class HandleResultTest(unittest.TestCase):
    @patch("wait_for_reply.handoff_db.set_session_last_checked")
    @patch("wait_for_reply.handoff_db.record_received_message")
    def test_outputs_json_to_stdout(self, mock_record, mock_set):
        import io
        replies = [{"text": "hi", "create_time": "12345", "message_id": "m1"}]
        old_stdout = sys.stdout
        sys.stdout = buf = io.StringIO()
        try:
            wait_for_reply.handle_result(replies, "https://w", "chat1", "sess1")
        finally:
            sys.stdout = old_stdout

        output = json.loads(buf.getvalue())
        self.assertEqual(output["count"], 1)
        self.assertEqual(output["replies"][0]["text"], "hi")
        mock_record.assert_called_once()
        mock_set.assert_called_once_with("sess1", "12345")

    @patch("wait_for_reply.handoff_db.set_session_last_checked")
    @patch("wait_for_reply.handoff_db.record_received_message")
    def test_no_session_id_skips_last_checked(self, mock_record, mock_set):
        import io
        replies = [{"text": "hi", "create_time": "12345"}]
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            wait_for_reply.handle_result(replies, "https://w", "chat1", None)
        finally:
            sys.stdout = old_stdout

        mock_set.assert_not_called()


class SendQuotaWarningTest(unittest.TestCase):
    @patch("wait_for_reply.lark_im.send_message")
    @patch("wait_for_reply.lark_im.get_tenant_token", return_value="token")
    @patch("wait_for_reply.handoff_config.load_credentials",
           return_value={"app_id": "a", "app_secret": "s"})
    def test_sends_card(self, _creds, _token, mock_send):
        result = wait_for_reply._send_quota_warning("chat1")
        self.assertTrue(result)
        mock_send.assert_called_once()
        card = mock_send.call_args[0][2]
        self.assertEqual(card["header"]["template"], "orange")

    @patch("wait_for_reply.handoff_config.load_credentials", return_value=None)
    def test_no_creds_returns_false(self, _creds):
        result = wait_for_reply._send_quota_warning("chat1")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
