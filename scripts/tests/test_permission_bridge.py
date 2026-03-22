#!/usr/bin/env python3
"""Tests for permission_bridge.py (Claude Code version).

Covers format_tool_description, deny_and_exit, and main() flow.
"""

import io
import json
import os
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

import permission_bridge  # type: ignore
import permission_core  # type: ignore


# ---------------------------------------------------------------------------
# format_tool_description
# ---------------------------------------------------------------------------

class FormatToolDescriptionTest(unittest.TestCase):
    """Tests for format_tool_description() rich card content generation."""

    def test_empty_input_returns_empty(self):
        self.assertEqual(permission_bridge.format_tool_description("Bash", None), "")

    def test_empty_dict_returns_empty(self):
        self.assertEqual(permission_bridge.format_tool_description("Bash", {}), "")

    # -- Bash --
    def test_bash_command_only(self):
        result = permission_bridge.format_tool_description("Bash", {"command": "ls -la"})
        self.assertIn("`ls -la`", result)

    def test_bash_description_only(self):
        result = permission_bridge.format_tool_description("Bash", {"description": "List files"})
        self.assertEqual(result, "List files")

    def test_bash_command_and_description(self):
        result = permission_bridge.format_tool_description(
            "Bash", {"command": "ls -la", "description": "List files"}
        )
        self.assertIn("List files", result)
        self.assertIn("`ls -la`", result)
        # Description comes first
        self.assertLess(result.index("List files"), result.index("`ls -la`"))

    def test_bash_long_command_truncated(self):
        long_cmd = "x" * 300
        result = permission_bridge.format_tool_description("Bash", {"command": long_cmd})
        self.assertIn("...", result)
        self.assertLessEqual(len(result.split("`")[1]), 204)  # 200 + "..."

    def test_bash_exact_200_not_truncated(self):
        cmd = "x" * 200
        result = permission_bridge.format_tool_description("Bash", {"command": cmd})
        self.assertNotIn("...", result)

    # -- Write/Edit --
    def test_write_file_path(self):
        result = permission_bridge.format_tool_description("Write", {"file_path": "/src/main.py"})
        self.assertEqual(result, "File: `/src/main.py`")

    def test_edit_file_path(self):
        result = permission_bridge.format_tool_description("Edit", {"file_path": "/src/app.ts"})
        self.assertEqual(result, "File: `/src/app.ts`")

    # -- Read --
    def test_read_file_path(self):
        result = permission_bridge.format_tool_description("Read", {"file_path": "/etc/config"})
        self.assertEqual(result, "File: `/etc/config`")

    # -- AskUserQuestion --
    def test_ask_user_question_with_options(self):
        tool_input = {
            "questions": [
                {
                    "question": "Which lib?",
                    "options": [
                        {"label": "React", "description": "UI framework"},
                        {"label": "Vue", "description": "Progressive framework"},
                    ],
                }
            ]
        }
        result = permission_bridge.format_tool_description("AskUserQuestion", tool_input)
        self.assertIn("Which lib?", result)
        self.assertIn("1. **React**", result)
        self.assertIn("2. **Vue**", result)

    def test_ask_user_question_no_description(self):
        tool_input = {
            "questions": [{"question": "Pick:", "options": [{"label": "A"}]}]
        }
        result = permission_bridge.format_tool_description("AskUserQuestion", tool_input)
        self.assertIn("1. **A**", result)

    def test_ask_user_empty_questions(self):
        """Empty questions list falls through to generic handler."""
        result = permission_bridge.format_tool_description("AskUserQuestion", {"questions": []})
        # Falls through to generic key=value format since no question parts
        self.assertIn("questions", result)

    # -- Generic --
    def test_generic_tool(self):
        result = permission_bridge.format_tool_description(
            "CustomTool", {"url": "https://example.com", "method": "POST"}
        )
        self.assertIn("**url:** `https://example.com`", result)
        self.assertIn("**method:** `POST`", result)

    def test_generic_truncates_long_values(self):
        long_val = "v" * 200
        result = permission_bridge.format_tool_description("CustomTool", {"key": long_val})
        self.assertIn("...", result)

    def test_generic_max_five_pairs(self):
        tool_input = {f"key{i}": f"val{i}" for i in range(10)}
        result = permission_bridge.format_tool_description("CustomTool", tool_input)
        self.assertEqual(result.count("**key"), 5)


# ---------------------------------------------------------------------------
# deny_and_exit
# ---------------------------------------------------------------------------

class IsHandoffInternalCommandTest(unittest.TestCase):
    """Tests for is_handoff_internal_command()."""

    def test_full_path_match(self):
        self.assertTrue(permission_bridge.is_handoff_internal_command(
            "Bash", {"command": "python3 /home/user/.claude/skills/handoff/scripts/wait_for_reply.py --timeout 0"}))

    def test_skill_scripts_variable_match(self):
        cmd = ('SKILL_SCRIPTS=$(python3 -c "import os; p=\'.claude/skills/handoff/scripts\'; '
               'print(p if os.path.isdir(p) else os.path.expanduser(\'~/.claude/skills/handoff/scripts\'))") '
               '&& python3 $SKILL_SCRIPTS/wait_for_reply.py --timeout 0')
        self.assertTrue(permission_bridge.is_handoff_internal_command("Bash", {"command": cmd}))

    def test_send_and_wait_matches(self):
        self.assertTrue(permission_bridge.is_handoff_internal_command(
            "Bash", {"command": "python3 $SKILL_SCRIPTS/send_and_wait.py 'hello' --timeout 0"}))

    def test_handoff_ops_matches(self):
        self.assertTrue(permission_bridge.is_handoff_internal_command(
            "Bash", {"command": "python3 $SKILL_SCRIPTS/handoff_ops.py guest-list"}))

    def test_non_handoff_command_rejected(self):
        self.assertFalse(permission_bridge.is_handoff_internal_command(
            "Bash", {"command": "rm -rf /tmp/something"}))

    def test_non_bash_tool_rejected(self):
        self.assertFalse(permission_bridge.is_handoff_internal_command(
            "Edit", {"file_path": "/handoff/scripts/wait_for_reply.py"}))

    def test_script_name_without_handoff_path_rejected(self):
        """Script name alone without handoff path context should not match."""
        self.assertFalse(permission_bridge.is_handoff_internal_command(
            "Bash", {"command": "python3 wait_for_reply.py"}))

    def test_enter_handoff_matches(self):
        self.assertTrue(permission_bridge.is_handoff_internal_command(
            "Bash", {"command": "python3 $SKILL_SCRIPTS/enter_handoff.py --session-model opus"}))

    def test_end_and_cleanup_matches(self):
        self.assertTrue(permission_bridge.is_handoff_internal_command(
            "Bash", {"command": "python3 $SKILL_SCRIPTS/end_and_cleanup.py --session-model opus"}))


class DenyAndExitTest(unittest.TestCase):
    def test_deny_output_format(self):
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            with self.assertRaises(SystemExit) as ctx:
                permission_bridge.deny_and_exit("Bash")
            self.assertEqual(ctx.exception.code, 0)
            output = json.loads(sys.stdout.getvalue())
            self.assertEqual(
                output["hookSpecificOutput"]["decision"]["behavior"], "deny"
            )
            self.assertIn("Bash", output["hookSpecificOutput"]["decision"]["message"])
        finally:
            sys.stdout = old_stdout

    def test_deny_with_reason(self):
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            with self.assertRaises(SystemExit):
                permission_bridge.deny_and_exit("Edit", "timeout")
            output = json.loads(sys.stdout.getvalue())
            msg = output["hookSpecificOutput"]["decision"]["message"]
            self.assertIn("timeout", msg)
        finally:
            sys.stdout = old_stdout


# ---------------------------------------------------------------------------
# main() integration tests
# ---------------------------------------------------------------------------

class MainTest(unittest.TestCase):
    """Integration tests for main() with mocked lark_im and stdin."""

    def setUp(self):
        # Save originals
        self._orig_get_session = permission_core.handoff_db.get_session
        self._orig_load_creds = permission_core.handoff_config.load_credentials
        self._orig_get_token = permission_bridge.lark_im.get_tenant_token
        self._orig_load_worker = permission_core.handoff_config.load_worker_url
        self._orig_ack = permission_bridge.handoff_worker.ack_worker_replies
        self._orig_send_msg = permission_bridge.lark_im.send_message
        self._orig_build_card = permission_bridge.lark_im.build_card
        self._orig_record = permission_bridge.handoff_db.record_received_message
        self._orig_set_lc = permission_bridge.handoff_db.set_session_last_checked
        self._orig_poll_ws = permission_bridge.handoff_worker.poll_worker_ws
        self._orig_poll = getattr(permission_bridge.handoff_worker, "poll_worker", None)

    def tearDown(self):
        permission_core.handoff_db.get_session = self._orig_get_session
        permission_core.handoff_config.load_credentials = self._orig_load_creds
        permission_bridge.lark_im.get_tenant_token = self._orig_get_token
        permission_core.handoff_config.load_worker_url = self._orig_load_worker
        permission_bridge.handoff_worker.ack_worker_replies = self._orig_ack
        permission_bridge.lark_im.send_message = self._orig_send_msg
        permission_bridge.lark_im.build_card = self._orig_build_card
        permission_bridge.handoff_db.record_received_message = self._orig_record
        permission_bridge.handoff_db.set_session_last_checked = self._orig_set_lc
        permission_bridge.handoff_worker.poll_worker_ws = self._orig_poll_ws
        if self._orig_poll is not None:
            permission_bridge.handoff_worker.poll_worker = self._orig_poll

    def _run_main(self, hook_input):
        old_stdin = sys.stdin
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdin = io.StringIO(json.dumps(hook_input))
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        try:
            permission_bridge.main()
            return sys.stdout.getvalue(), None
        except SystemExit as e:
            return sys.stdout.getvalue(), e.code
        finally:
            sys.stdin = old_stdin
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    def test_no_session_exits_1(self):
        """No active handoff → exit(1) to fall through to CLI prompt."""
        permission_core.handoff_db.get_session = lambda sid: None
        _, exit_code = self._run_main(
            {"tool_name": "Bash", "tool_input": {}, "session_id": "gone"}
        )
        self.assertEqual(exit_code, 1)

    def test_empty_session_id_exits_1(self):
        _, exit_code = self._run_main(
            {"tool_name": "Bash", "tool_input": {}, "session_id": ""}
        )
        self.assertEqual(exit_code, 1)

    def test_no_credentials_denies(self):
        permission_core.handoff_db.get_session = lambda sid: {"chat_id": "c1"}
        permission_core.handoff_config.load_credentials = lambda **kw: None
        output, exit_code = self._run_main(
            {"tool_name": "Bash", "tool_input": {}, "session_id": "s1"}
        )
        self.assertEqual(exit_code, 0)
        data = json.loads(output)
        self.assertEqual(data["hookSpecificOutput"]["decision"]["behavior"], "deny")

    def test_allow_decision(self):
        """Full allow flow: session active → card sent → user approves."""
        permission_core.handoff_db.get_session = lambda sid: {"chat_id": "c1"}
        permission_core.handoff_config.load_credentials = lambda **kw: {
            "app_id": "a", "app_secret": "b"
        }
        permission_bridge.lark_im.get_tenant_token = lambda a, b: "tok"
        permission_core.handoff_config.load_worker_url = lambda **kw: "https://w.example"
        permission_bridge.handoff_worker.ack_worker_replies = lambda *a, **kw: None
        permission_bridge.lark_im.send_message = lambda *a, **kw: "msg-1"
        permission_bridge.lark_im.build_card = lambda *a, **kw: {}
        permission_bridge.handoff_db.record_received_message = lambda **kw: None
        permission_bridge.handoff_db.set_session_last_checked = lambda *a: None
        permission_bridge.handoff_worker.poll_worker_ws = lambda *a, **kw: {
            "replies": [{"text": "y", "create_time": "100", "message_id": "r1"}],
            "takeover": False,
            "error": None,
        }

        output, exit_code = self._run_main(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s1"}
        )
        self.assertEqual(exit_code, 0)
        data = json.loads(output)
        self.assertEqual(data["hookSpecificOutput"]["decision"]["behavior"], "allow")

    def test_deny_decision(self):
        permission_core.handoff_db.get_session = lambda sid: {"chat_id": "c1"}
        permission_core.handoff_config.load_credentials = lambda **kw: {
            "app_id": "a", "app_secret": "b"
        }
        permission_bridge.lark_im.get_tenant_token = lambda a, b: "tok"
        permission_core.handoff_config.load_worker_url = lambda **kw: "https://w.example"
        permission_bridge.handoff_worker.ack_worker_replies = lambda *a, **kw: None
        permission_bridge.lark_im.send_message = lambda *a, **kw: "msg-1"
        permission_bridge.lark_im.build_card = lambda *a, **kw: {}
        permission_bridge.handoff_db.record_received_message = lambda **kw: None
        permission_bridge.handoff_db.set_session_last_checked = lambda *a: None
        permission_bridge.handoff_worker.poll_worker_ws = lambda *a, **kw: {
            "replies": [{"text": "n", "create_time": "100", "message_id": "r1"}],
            "takeover": False,
            "error": None,
        }

        output, exit_code = self._run_main(
            {"tool_name": "Bash", "tool_input": {}, "session_id": "s1"}
        )
        data = json.loads(output)
        self.assertEqual(data["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertIn("message", data["hookSpecificOutput"]["decision"])

    def test_always_decision_passes_updated_permissions(self):
        """'Approve All' returns allow + updatedPermissions from suggestions."""
        permission_core.handoff_db.get_session = lambda sid: {"chat_id": "c1"}
        permission_core.handoff_config.load_credentials = lambda **kw: {
            "app_id": "a", "app_secret": "b"
        }
        permission_bridge.lark_im.get_tenant_token = lambda a, b: "tok"
        permission_core.handoff_config.load_worker_url = lambda **kw: "https://w.example"
        permission_bridge.handoff_worker.ack_worker_replies = lambda *a, **kw: None
        permission_bridge.lark_im.send_message = lambda *a, **kw: "msg-1"
        permission_bridge.lark_im.build_card = lambda *a, **kw: {}
        permission_bridge.handoff_db.record_received_message = lambda **kw: None
        permission_bridge.handoff_db.set_session_last_checked = lambda *a: None
        permission_bridge.handoff_worker.poll_worker_ws = lambda *a, **kw: {
            "replies": [{"text": "always", "create_time": "100", "message_id": "r1"}],
            "takeover": False,
            "error": None,
        }

        suggestions = [{"type": "toolAlwaysAllow", "tool": "Bash"}]
        output, exit_code = self._run_main({
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "session_id": "s1",
            "permission_suggestions": suggestions,
        })
        self.assertEqual(exit_code, 0)
        data = json.loads(output)
        self.assertEqual(data["hookSpecificOutput"]["decision"]["behavior"], "allow")
        self.assertEqual(
            data["hookSpecificOutput"]["decision"]["updatedPermissions"],
            suggestions,
        )

    def test_always_decision_without_suggestions(self):
        """'Approve All' without suggestions still returns allow (no updatedPermissions)."""
        permission_core.handoff_db.get_session = lambda sid: {"chat_id": "c1"}
        permission_core.handoff_config.load_credentials = lambda **kw: {
            "app_id": "a", "app_secret": "b"
        }
        permission_bridge.lark_im.get_tenant_token = lambda a, b: "tok"
        permission_core.handoff_config.load_worker_url = lambda **kw: "https://w.example"
        permission_bridge.handoff_worker.ack_worker_replies = lambda *a, **kw: None
        permission_bridge.lark_im.send_message = lambda *a, **kw: "msg-1"
        permission_bridge.lark_im.build_card = lambda *a, **kw: {}
        permission_bridge.handoff_db.record_received_message = lambda **kw: None
        permission_bridge.handoff_db.set_session_last_checked = lambda *a: None
        permission_bridge.handoff_worker.poll_worker_ws = lambda *a, **kw: {
            "replies": [{"text": "always", "create_time": "100", "message_id": "r1"}],
            "takeover": False,
            "error": None,
        }

        output, exit_code = self._run_main({
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "session_id": "s1",
            # No permission_suggestions
        })
        self.assertEqual(exit_code, 0)
        data = json.loads(output)
        self.assertEqual(data["hookSpecificOutput"]["decision"]["behavior"], "allow")
        self.assertNotIn("updatedPermissions", data["hookSpecificOutput"]["decision"])

    def test_card_send_failure_denies(self):
        permission_core.handoff_db.get_session = lambda sid: {"chat_id": "c1"}
        permission_core.handoff_config.load_credentials = lambda **kw: {
            "app_id": "a", "app_secret": "b"
        }
        permission_bridge.lark_im.get_tenant_token = lambda a, b: "tok"
        permission_core.handoff_config.load_worker_url = lambda **kw: "https://w.example"
        permission_bridge.handoff_worker.ack_worker_replies = lambda *a, **kw: None
        permission_bridge.lark_im.build_card = lambda *a, **kw: {}

        def fail_send(*a, **kw):
            raise RuntimeError("network error")

        permission_bridge.lark_im.send_message = fail_send

        output, exit_code = self._run_main(
            {"tool_name": "Bash", "tool_input": {}, "session_id": "s1"}
        )
        self.assertEqual(exit_code, 0)
        data = json.loads(output)
        self.assertEqual(data["hookSpecificOutput"]["decision"]["behavior"], "deny")

    def test_invalid_json_stdin(self):
        """Malformed stdin JSON should be handled gracefully."""
        old_stdin = sys.stdin
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdin = io.StringIO("not json")
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()

        # No session → exit(1) (since session_id defaults to "")
        permission_core.handoff_db.get_session = lambda sid: None
        try:
            permission_bridge.main()
            stdout_val = sys.stdout.getvalue()
            exit_code = None
        except SystemExit as e:
            stdout_val = sys.stdout.getvalue()
            exit_code = e.code
        finally:
            sys.stdin = old_stdin
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
