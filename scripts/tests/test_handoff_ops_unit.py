#!/usr/bin/env python3

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

import sys

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

import handoff_config  # type: ignore
import handoff_db  # type: ignore
import handoff_ops  # type: ignore


class _Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class HandoffOpsUnitTest(unittest.TestCase):
    def setUp(self):
        self._old_home = os.environ.get("HOME")
        self._old_project = os.environ.get("HANDOFF_PROJECT_DIR")
        self._old_session = os.environ.get("HANDOFF_SESSION_ID")
        self._old_tool = os.environ.get("HANDOFF_SESSION_TOOL")
        self._old_handoff_home = handoff_config.HANDOFF_HOME

        self.tmp = tempfile.TemporaryDirectory()
        self.project_dir = os.path.join(self.tmp.name, "project")
        os.makedirs(self.project_dir, exist_ok=True)

        os.environ["HOME"] = self.tmp.name
        os.environ["HANDOFF_PROJECT_DIR"] = self.project_dir
        os.environ["HANDOFF_SESSION_TOOL"] = "Claude Code"
        os.environ.pop("HANDOFF_SESSION_ID", None)
        handoff_config.HANDOFF_HOME = os.path.join(self.tmp.name, ".handoff")

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

        if self._old_session is None:
            os.environ.pop("HANDOFF_SESSION_ID", None)
        else:
            os.environ["HANDOFF_SESSION_ID"] = self._old_session

        if self._old_tool is None:
            os.environ.pop("HANDOFF_SESSION_TOOL", None)
        else:
            os.environ["HANDOFF_SESSION_TOOL"] = self._old_tool

        handoff_config.HANDOFF_HOME = self._old_handoff_home
        self.tmp.cleanup()

    def test_config_current_returns_boolean_field(self):
        handoff_home = os.path.join(self.tmp.name, ".handoff")
        old_home_dir = handoff_config.HANDOFF_HOME
        try:
            handoff_config.HANDOFF_HOME = handoff_home
            os.makedirs(handoff_home, exist_ok=True)
            with open(os.path.join(handoff_home, "config.json"), "w") as f:
                f.write("{}")

            out = io.StringIO()
            with redirect_stdout(out):
                rc = handoff_ops.cmd_config_current(_Args())

            self.assertEqual(rc, 0)
            payload = json.loads(out.getvalue())
            self.assertIn("config_exists", payload)
            self.assertIsInstance(payload["config_exists"], bool)
            self.assertTrue(payload["config_exists"])
        finally:
            handoff_config.HANDOFF_HOME = old_home_dir

    def test_session_check_returns_zero_when_already_active(self):
        handoff_db.register_session("s1", "chat-1", "opus")
        os.environ["HANDOFF_SESSION_ID"] = "s1"

        out = io.StringIO()
        with redirect_stdout(out):
            rc = handoff_ops.cmd_session_check(_Args(session_id=""))

        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["already_active"])
        self.assertEqual(payload["chat_id"], "chat-1")

    def test_render_status_pretty_stable_shape(self):
        status_obj = {
            "workspace": "ws-1",
            "database": "/tmp/db.sqlite",
            "db_exists": True,
            "groups": [
                {
                    "name": "g1",
                    "chat_id": "chat-1",
                    "is_current_session": True,
                    "active": True,
                    "session": {
                        "session_id": "sid-1",
                        "session_tool": "Claude Code",
                        "session_model": "claude-opus-4",
                        "activated_at_human": "2026-02-17 10:00:00",
                        "last_checked_human": "2026-02-17 10:05:00",
                    },
                },
                {
                    "name": "g2",
                    "chat_id": "chat-2",
                    "is_current_session": False,
                    "active": False,
                    "session": None,
                },
            ],
        }

        text = handoff_ops._render_status_pretty(status_obj)
        self.assertIn("Workspace: ws-1", text)
        self.assertIn("Database: /tmp/db.sqlite (exists)", text)
        self.assertIn("Groups: 2", text)
        self.assertIn("- g1 [current] (active)", text)
        self.assertIn("  session_id: sid-1", text)
        self.assertIn("  session_tool: Claude Code", text)
        self.assertIn("  session_model: claude-opus-4", text)
        self.assertIn("  activated_at: 2026-02-17 10:00:00", text)
        self.assertIn("  last_checked: 2026-02-17 10:05:00", text)
        self.assertIn("- g2 (idle)", text)

    def test_session_tool_model_strips_prefix_before_slash(self):
        handoff_db.register_session("s1", "chat-1", "opus")
        os.environ["HANDOFF_SESSION_ID"] = "s1"

        args = _Args(session_model="opencode/k2p5")
        chat_id, tool, model = handoff_ops._session_tool_model(args)
        self.assertEqual(model, "k2p5")
        self.assertEqual(tool, "Claude Code")  # from env, not DB
        self.assertEqual(chat_id, "chat-1")

    def test_session_tool_model_keeps_name_without_slash(self):
        handoff_db.register_session("s1", "chat-1", "opus")
        os.environ["HANDOFF_SESSION_ID"] = "s1"

        args = _Args(session_model="claude-sonnet-4")
        chat_id, tool, model = handoff_ops._session_tool_model(args)
        self.assertEqual(model, "claude-sonnet-4")
        self.assertEqual(tool, "Claude Code")  # from env, not DB
        self.assertEqual(chat_id, "chat-1")


    def test_known_session_tab_names_returns_tool_and_model(self):
        handoff_db.register_session("s1", "chat-tabs", "opus 4.6")
        names = handoff_ops._known_session_tab_names("chat-tabs")
        self.assertIn("Claude Code", names)
        self.assertIn("opus 4.6", names)

    def test_known_session_tab_names_empty_for_unknown_chat(self):
        names = handoff_ops._known_session_tab_names("nonexistent-chat")
        self.assertEqual(names, set())

    def test_tabs_start_preserves_user_created_tabs(self):
        """Verify that tabs-start only removes known session tabs, not user-created ones."""
        handoff_db.register_session("s1", "chat-tab-test", "opus 4.6")
        os.environ["HANDOFF_SESSION_ID"] = "s1"

        # Simulate tabs: session tabs + a user-created tab
        fake_tabs = [
            {"tab_id": "1", "tab_type": "message"},
            {"tab_id": "2", "tab_name": "old-tool", "tab_type": "url", "tab_content": {"url": "https://x.com"}},
            {"tab_id": "3", "tab_name": "OLU Demo", "tab_type": "url", "tab_content": {"url": "https://demo.olu.tech"}},
        ]

        # _known_session_tab_names for this chat should return {"Claude Code", "opus 4.6"}
        known = handoff_ops._known_session_tab_names("chat-tab-test")
        keep = {"Claude Code", "opus 4.6"}

        # "old-tool" is NOT in known session names → should NOT be deleted
        # "OLU Demo" is NOT in known session names → should NOT be deleted
        stale_ids = [
            tab["tab_id"]
            for tab in fake_tabs
            if tab.get("tab_type") == "url"
            and tab.get("tab_name") in known
            and tab.get("tab_name") not in keep
            and tab.get("tab_id")
        ]
        self.assertEqual(stale_ids, [])  # Nothing should be deleted

    def test_tabs_start_stale_logic_removes_session_tabs_only(self):
        """Verify the stale-tab filter logic removes session tabs but keeps user tabs."""
        # Session has tool="Claude Code" and model="opus 4.6"
        handoff_db.register_session("s1", "chat-stale", "opus 4.6")

        known = handoff_ops._known_session_tab_names("chat-stale")
        self.assertIn("opus 4.6", known)
        self.assertIn("Claude Code", known)
        self.assertNotIn("OLU Demo", known)

        fake_tabs = [
            {"tab_id": "10", "tab_name": "Claude Code", "tab_type": "url"},
            {"tab_id": "11", "tab_name": "opus 4.6", "tab_type": "url"},
            {"tab_id": "12", "tab_name": "OLU Demo", "tab_type": "url"},
        ]

        # Simulate keeping current session tabs, removing stale ones
        keep = {"Claude Code", "opus 4.6"}
        stale_ids = [
            tab["tab_id"]
            for tab in fake_tabs
            if tab.get("tab_type") == "url"
            and tab.get("tab_name") in known
            and tab.get("tab_name") not in keep
            and tab.get("tab_id")
        ]
        # Nothing stale — both known names are in keep set, "OLU Demo" is not in known
        self.assertEqual(stale_ids, [])

        # Now simulate a different keep set (new session with different model)
        keep_new = {"Claude Code", "sonnet 4"}
        stale_ids_new = [
            tab["tab_id"]
            for tab in fake_tabs
            if tab.get("tab_type") == "url"
            and tab.get("tab_name") in known
            and tab.get("tab_name") not in keep_new
            and tab.get("tab_id")
        ]
        # "opus 4.6" is known AND not in keep_new → stale
        # "OLU Demo" is NOT known → safe
        self.assertEqual(stale_ids_new, ["11"])


    def test_autoapprove_default_off(self):
        handoff_db.register_session("s1", "chat-aa", "opus")
        session = handoff_db.get_session("s1")
        self.assertFalse(session["autoapprove"])

    def test_autoapprove_set_on_off(self):
        handoff_db.register_session("s1", "chat-aa2", "opus")
        handoff_db.set_autoapprove("chat-aa2", True)
        self.assertTrue(handoff_db.get_autoapprove("chat-aa2"))
        session = handoff_db.get_session("s1")
        self.assertTrue(session["autoapprove"])

        handoff_db.set_autoapprove("chat-aa2", False)
        self.assertFalse(handoff_db.get_autoapprove("chat-aa2"))
        session = handoff_db.get_session("s1")
        self.assertFalse(session["autoapprove"])

    def test_set_autoapprove_command(self):
        handoff_db.register_session("s1", "chat-aa3", "opus")
        os.environ["HANDOFF_SESSION_ID"] = "s1"

        buf = io.StringIO()
        with redirect_stdout(buf):
            handoff_ops.cmd_set_autoapprove(_Args(enabled="on"))
        result = json.loads(buf.getvalue())
        self.assertTrue(result["ok"])
        self.assertTrue(result["autoapprove"])
        self.assertTrue(handoff_db.get_autoapprove("chat-aa3"))

        buf = io.StringIO()
        with redirect_stdout(buf):
            handoff_ops.cmd_set_autoapprove(_Args(enabled="off"))
        result = json.loads(buf.getvalue())
        self.assertTrue(result["ok"])
        self.assertFalse(result["autoapprove"])
        self.assertFalse(handoff_db.get_autoapprove("chat-aa3"))


if __name__ == "__main__":
    unittest.main()
