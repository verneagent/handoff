#!/usr/bin/env python3
"""Tests for the group_config module.

Covers:
- Config card building and parsing
- load_config / save_config with mocked Lark API
- SQLite cache behavior (TTL, freshness)
- Convenience helpers (guests, autoapprove, filter, rules)
- PATCH failure fallback (delete + re-create)
- Pin API functions in lark_im
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

import handoff_config
import handoff_db
import group_config


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
        self._old_handoff_home = handoff_config.HANDOFF_HOME
        handoff_config.HANDOFF_HOME = os.path.join(self.tmp.name, ".handoff")
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
        handoff_config.HANDOFF_HOME = self._old_handoff_home
        self.tmp.cleanup()


# ---------------------------------------------------------------------------
# Card building and parsing
# ---------------------------------------------------------------------------

class CardBuildParseTest(unittest.TestCase):
    """Tests for _build_config_card and _parse_config_from_card."""

    def test_round_trip(self):
        """Config survives build → parse round trip."""
        config = {
            "guests": [{"open_id": "ou_alice", "name": "Alice", "role": "coowner"}],
            "autoapprove": True,
            "filter": "concise",
            "rules": {"lang": "Reply in Chinese."},
        }
        card = group_config._build_config_card(config)

        # Simulate what get_message returns: body.content = JSON-encoded card
        msg_item = {
            "msg_type": "interactive",
            "body": {"content": json.dumps(card)},
        }
        parsed = group_config._parse_config_from_card(msg_item)
        self.assertEqual(parsed, config)

    def test_empty_config(self):
        """Empty/default config round-trips."""
        config = {"guests": [], "autoapprove": False, "filter": "concise", "rules": {}}
        card = group_config._build_config_card(config)
        msg_item = {"body": {"content": json.dumps(card)}}
        parsed = group_config._parse_config_from_card(msg_item)
        self.assertEqual(parsed, config)

    def test_parse_bad_content(self):
        """Bad content returns None."""
        self.assertIsNone(group_config._parse_config_from_card({}))
        self.assertIsNone(group_config._parse_config_from_card({"body": {}}))
        self.assertIsNone(
            group_config._parse_config_from_card({"body": {"content": "not json"}})
        )

    def test_card_has_update_multi(self):
        """Card config includes update_multi: true."""
        card = group_config._build_config_card({"guests": []})
        self.assertTrue(card["config"]["update_multi"])


# ---------------------------------------------------------------------------
# SQLite cache
# ---------------------------------------------------------------------------

class CacheTest(_DbTestCase):
    """Tests for group_config_cache table operations."""

    def test_get_empty(self):
        """No cache returns None."""
        self.assertIsNone(handoff_db.get_cached_group_config("chat_xxx"))

    def test_set_and_get(self):
        """Set then get returns correct data."""
        config = {"guests": [], "autoapprove": True, "filter": "verbose", "rules": {}}
        handoff_db.set_cached_group_config("chat_1", config, "msg_pin_1")
        result = handoff_db.get_cached_group_config("chat_1")
        self.assertIsNotNone(result)
        cfg, pin_id, last_synced = result
        self.assertEqual(cfg, config)
        self.assertEqual(pin_id, "msg_pin_1")
        self.assertGreater(last_synced, 0)

    def test_update_overwrites(self):
        """Second set overwrites first."""
        handoff_db.set_cached_group_config("chat_2", {"filter": "verbose"}, "msg_1")
        handoff_db.set_cached_group_config("chat_2", {"filter": "concise"}, "msg_2")
        cfg, pin_id, _ = handoff_db.get_cached_group_config("chat_2")
        self.assertEqual(cfg["filter"], "concise")
        self.assertEqual(pin_id, "msg_2")

    def test_delete(self):
        """Delete removes the cache entry."""
        handoff_db.set_cached_group_config("chat_3", {"filter": "verbose"}, "msg_1")
        handoff_db.delete_cached_group_config("chat_3")
        self.assertIsNone(handoff_db.get_cached_group_config("chat_3"))


# ---------------------------------------------------------------------------
# load_config / save_config with mocked Lark API
# ---------------------------------------------------------------------------

class LoadConfigTest(_DbTestCase):
    """Tests for group_config.load_config."""

    def test_returns_cached_when_fresh(self):
        """Returns cache without hitting Lark when cache is fresh."""
        config = {"guests": [], "autoapprove": False, "filter": "concise", "rules": {}}
        handoff_db.set_cached_group_config("chat_c1", config, "pin_1")

        # Should not call Lark at all
        with mock.patch.object(group_config, "_find_config_pin") as mock_find:
            result = group_config.load_config("fake_token", "chat_c1")
            mock_find.assert_not_called()
        self.assertEqual(result, config)

    def test_fetches_from_lark_when_stale(self):
        """Fetches from Lark when cache is expired."""
        config = {"guests": [], "autoapprove": False, "filter": "verbose", "rules": {}}
        # Set cache with old timestamp
        handoff_db.set_cached_group_config("chat_c2", config, "pin_1")
        # Manually make it stale
        conn = handoff_db._get_db()
        conn.execute(
            "UPDATE group_config_cache SET last_synced = ? WHERE chat_id = ?",
            (int(time.time()) - 600, "chat_c2"),
        )
        conn.commit()
        conn.close()

        new_config = {"guests": [{"open_id": "ou_x", "name": "X"}], "autoapprove": True, "filter": "concise", "rules": {}}
        with mock.patch.object(group_config, "_find_config_pin", return_value=("pin_2", new_config)):
            result = group_config.load_config("fake_token", "chat_c2")
        self.assertEqual(result, new_config)

    def test_force_bypasses_cache(self):
        """force=True always fetches from Lark."""
        config = {"guests": [], "autoapprove": False, "filter": "concise", "rules": {}}
        handoff_db.set_cached_group_config("chat_c3", config, "pin_1")

        new_config = {"guests": [], "autoapprove": True, "filter": "verbose", "rules": {"test": "test rule"}}
        with mock.patch.object(group_config, "_find_config_pin", return_value=("pin_2", new_config)):
            result = group_config.load_config("fake_token", "chat_c3", force=True)
        self.assertEqual(result, new_config)

    def test_no_pin_returns_default(self):
        """Returns default config when no pinned card found."""
        with mock.patch.object(group_config, "_find_config_pin", return_value=(None, None)):
            result = group_config.load_config("fake_token", "chat_c4", force=True)
        self.assertEqual(result["guests"], [])
        self.assertFalse(result["autoapprove"])
        self.assertEqual(result["filter"], "concise")
        self.assertEqual(result["rules"], {})


class SaveConfigTest(_DbTestCase):
    """Tests for group_config.save_config."""

    def test_creates_new_card_when_none_exists(self):
        """Creates a new pinned card when no existing one found."""
        config = {"guests": [], "autoapprove": True, "filter": "verbose", "rules": {}}
        with mock.patch.object(group_config, "_find_config_pin", return_value=(None, None)), \
             mock.patch.object(group_config, "_create_config_card", return_value="new_msg_1") as mock_create:
            msg_id = group_config.save_config("token", "chat_s1", config)
        mock_create.assert_called_once_with("token", "chat_s1", config)
        self.assertEqual(msg_id, "new_msg_1")
        # Check cache was updated
        cached = handoff_db.get_cached_group_config("chat_s1")
        self.assertIsNotNone(cached)
        self.assertEqual(cached[0], config)
        self.assertEqual(cached[1], "new_msg_1")

    def test_updates_existing_card(self):
        """Updates existing card when pin_message_id is cached."""
        handoff_db.set_cached_group_config("chat_s2", {"filter": "verbose"}, "existing_pin")
        config = {"guests": [], "autoapprove": True, "filter": "concise", "rules": {}}
        with mock.patch.object(group_config, "_update_config_card", return_value="existing_pin") as mock_update:
            msg_id = group_config.save_config("token", "chat_s2", config)
        mock_update.assert_called_once_with("token", "chat_s2", "existing_pin", config)
        self.assertEqual(msg_id, "existing_pin")


# ---------------------------------------------------------------------------
# PATCH failure fallback
# ---------------------------------------------------------------------------

class PatchFallbackTest(_DbTestCase):
    """Tests for _update_config_card fallback on PATCH failure."""

    @mock.patch("group_config.lark_im")
    def test_patch_failure_recreates(self, mock_lark):
        """When PATCH fails, deletes old card and creates new one."""
        mock_lark.update_card_message.side_effect = RuntimeError("14-day expired")
        mock_lark.delete_pin.return_value = None
        mock_lark.delete_message.return_value = None
        mock_lark.send_message.return_value = "new_msg_id"
        mock_lark.create_pin.return_value = {"message_id": "new_msg_id"}

        config = {"guests": [], "autoapprove": False, "filter": "concise", "rules": {}}
        result = group_config._update_config_card("token", "chat_f1", "old_pin", config)

        self.assertEqual(result, "new_msg_id")
        mock_lark.delete_pin.assert_called_once_with("token", "old_pin")
        mock_lark.delete_message.assert_called_once_with("token", "old_pin")
        mock_lark.send_message.assert_called_once()
        mock_lark.create_pin.assert_called_once()


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

class ConvenienceHelpersTest(_DbTestCase):
    """Tests for get/set convenience methods."""

    def _enter_mock_load_save(self):
        """Set up mocks for load_config and save_config. Call in setUp or test body."""
        import copy
        self._stored_config = copy.deepcopy(group_config.DEFAULT_CONFIG)

        def fake_load(token, chat_id, **kwargs):
            return copy.deepcopy(self._stored_config)

        def fake_save(token, chat_id, config):
            self._stored_config = config
            return "pin_id"

        p1 = mock.patch.object(group_config, "load_config", side_effect=fake_load)
        p2 = mock.patch.object(group_config, "save_config", side_effect=fake_save)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def test_get_set_autoapprove(self):
        self._enter_mock_load_save()
        self.assertFalse(group_config.get_autoapprove("t", "c"))
        group_config.set_autoapprove("t", "c", True)
        self.assertTrue(self._stored_config["autoapprove"])

    def test_get_set_filter(self):
        self._enter_mock_load_save()
        self.assertEqual(group_config.get_filter("t", "c"), "concise")
        group_config.set_filter("t", "c", "verbose")
        self.assertEqual(self._stored_config["filter"], "verbose")

    def test_get_rules_empty(self):
        self._enter_mock_load_save()
        self.assertEqual(group_config.get_rules("t", "c"), {})

    def test_add_rule(self):
        self._enter_mock_load_save()
        rules = group_config.add_rule("t", "c", "lang", "Reply in Chinese")
        self.assertEqual(rules, {"lang": "Reply in Chinese"})
        rules = group_config.add_rule("t", "c", "prod", "Don't touch prod")
        self.assertEqual(rules, {"lang": "Reply in Chinese", "prod": "Don't touch prod"})

    def test_remove_rule(self):
        self._enter_mock_load_save()
        group_config.add_rule("t", "c", "lang", "Reply in Chinese")
        group_config.add_rule("t", "c", "prod", "Don't touch prod")
        removed, remaining = group_config.remove_rule("t", "c", "lang")
        self.assertEqual(removed, "Reply in Chinese")
        self.assertEqual(remaining, {"prod": "Don't touch prod"})

    def test_remove_rule_nonexistent(self):
        self._enter_mock_load_save()
        removed, remaining = group_config.remove_rule("t", "c", "nope")
        self.assertIsNone(removed)
        self.assertEqual(remaining, {})

    def test_legacy_string_migration(self):
        """String rules are migrated to dict on read."""
        self._enter_mock_load_save()
        self._stored_config["rules"] = "old string rules"
        rules = group_config.get_rules("t", "c")
        self.assertEqual(rules, {"default": "old string rules"})

    def test_add_rule_migrates_string(self):
        """add_rule migrates legacy string format."""
        self._enter_mock_load_save()
        self._stored_config["rules"] = "old rule"
        rules = group_config.add_rule("t", "c", "new", "new rule")
        self.assertEqual(rules, {"default": "old rule", "new": "new rule"})

    def test_add_remove_guests(self):
        self._enter_mock_load_save()
        added, current = group_config.add_guests("t", "c", [
            {"open_id": "ou_1", "name": "Alice"},
            {"open_id": "ou_2", "name": "Bob"},
        ])
        self.assertEqual(len(added), 2)
        self.assertEqual(len(current), 2)

        # Add duplicate
        added2, current2 = group_config.add_guests("t", "c", [
            {"open_id": "ou_1", "name": "Alice"},
        ])
        self.assertEqual(len(added2), 0)
        self.assertEqual(len(current2), 2)

        # Remove
        removed, remaining = group_config.remove_guests("t", "c", ["ou_1"])
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["open_id"], "ou_1")
        self.assertEqual(len(remaining), 1)

    def test_get_member_roles(self):
        self._enter_mock_load_save()
        group_config.add_guests("t", "c", [
            {"open_id": "ou_1", "name": "Alice", "role": "coowner"},
            {"open_id": "ou_2", "name": "Bob"},
        ])
        roles = group_config.get_member_roles("t", "c")
        self.assertEqual(roles["ou_1"], "coowner")
        self.assertEqual(roles["ou_2"], "guest")


# ---------------------------------------------------------------------------
# _find_config_pin
# ---------------------------------------------------------------------------

class FindConfigPinTest(unittest.TestCase):
    """Tests for _find_config_pin."""

    @mock.patch("group_config.lark_im")
    def test_finds_config_card(self, mock_lark):
        """Finds the config card among multiple pins."""
        config = {"guests": [], "autoapprove": True, "filter": "verbose", "rules": {"test": "test rule"}}
        card = group_config._build_config_card(config)

        mock_lark.list_pins.return_value = [
            {"message_id": "msg_other"},
            {"message_id": "msg_config"},
        ]
        mock_lark.get_message.side_effect = [
            # First pin: a text message
            {"msg_type": "text", "body": {"content": "hello"}},
            # Second pin: our config card
            {"msg_type": "interactive", "body": {"content": json.dumps(card)}},
        ]

        msg_id, result = group_config._find_config_pin("token", "chat_1")
        self.assertEqual(msg_id, "msg_config")
        self.assertEqual(result, config)

    @mock.patch("group_config.lark_im")
    def test_no_config_pin(self, mock_lark):
        """Returns (None, None) when no config card found."""
        mock_lark.list_pins.return_value = []
        msg_id, result = group_config._find_config_pin("token", "chat_2")
        self.assertIsNone(msg_id)
        self.assertIsNone(result)

    @mock.patch("group_config.lark_im")
    def test_ignores_non_config_cards(self, mock_lark):
        """Ignores interactive cards that don't match config schema."""
        non_config_card = {
            "elements": [{"text": {"content": '{"random": "data"}', "tag": "lark_md"}, "tag": "div"}],
        }
        mock_lark.list_pins.return_value = [{"message_id": "msg_1"}]
        mock_lark.get_message.return_value = {
            "msg_type": "interactive",
            "body": {"content": json.dumps(non_config_card)},
        }
        msg_id, result = group_config._find_config_pin("token", "chat_3")
        self.assertIsNone(msg_id)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
