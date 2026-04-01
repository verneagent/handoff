#!/usr/bin/env python3
"""Tests for start_and_wait.py and end_and_cleanup.py wrappers."""

import contextlib
import io
import os
import subprocess
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))

import end_and_cleanup  # type: ignore
import start_and_wait  # type: ignore


class StartAndWaitTest(unittest.TestCase):
    def setUp(self):
        self._orig_run_tool = start_and_wait.script_utils.run_tool
        self._orig_subprocess_run = start_and_wait.subprocess.run
        self.run_calls = []
        self.wait_calls = []
        self.fail_on = None

        def fake_run_tool(description, script, *args, **kwargs):  # type: ignore
            self.run_calls.append((description, script, args, kwargs))
            if self.fail_on == description:
                raise subprocess.CalledProcessError(1, [script])
            return subprocess.CompletedProcess([script], 0, stdout="", stderr="")

        def fake_subprocess_run(cmd, *args, **kwargs):  # type: ignore
            self.wait_calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        start_and_wait.script_utils.run_tool = fake_run_tool  # type: ignore
        start_and_wait.subprocess.run = fake_subprocess_run  # type: ignore

    def tearDown(self):
        start_and_wait.script_utils.run_tool = self._orig_run_tool  # type: ignore
        start_and_wait.subprocess.run = self._orig_subprocess_run  # type: ignore

    def _run(self, argv):
        old_argv = sys.argv
        sys.argv = ["start_and_wait.py"] + argv
        try:
            return start_and_wait.main()
        finally:
            sys.argv = old_argv

    def test_runs_full_sequence(self):
        rc = self._run(["--session-model", "opus"])
        self.assertEqual(rc, 0)
        expected = [
            ("silence on", "iterm2_silence.py"),
            ("tabs-start", "handoff_ops.py"),
            ("send status card", "handoff_ops.py"),
        ]
        self.assertEqual([c[:2] for c in self.run_calls], expected)
        self.assertEqual(len(self.wait_calls), 1)
        wait_cmd, _ = self.wait_calls[0]
        self.assertIn("wait_for_reply.py", wait_cmd[-1])

    def test_skips_and_timeout_flags(self):
        rc = self._run(
            [
                "--session-model",
                "opus",
                "--skip-silence",
                "--skip-tabs",
                "--skip-card",
                "--timeout",
                "7",
                "--interval",
                "5",
                "--no-ws",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.run_calls, [])
        wait_cmd, _ = self.wait_calls[0]
        self.assertIn("--timeout", wait_cmd)
        self.assertIn("7", wait_cmd)
        self.assertIn("--interval", wait_cmd)
        self.assertIn("5", wait_cmd)
        self.assertIn("--no-ws", wait_cmd)

    def test_restores_profile_on_failure(self):
        self.fail_on = "tabs-start"
        with contextlib.redirect_stderr(io.StringIO()):
            rc = self._run(["--session-model", "opus"])
        self.assertNotEqual(rc, 0)
        restore_calls = [c for c in self.run_calls if c[0] == "restore iTerm profile"]
        self.assertEqual(len(restore_calls), 1)


class EndAndCleanupTest(unittest.TestCase):
    def setUp(self):
        self._orig_run_tool = end_and_cleanup.script_utils.run_tool
        self._orig_reset_working = end_and_cleanup.handoff_lifecycle.reset_working_card
        self.run_calls = []

        def fake_run_tool(description, script, *args, **kwargs):  # type: ignore
            self.run_calls.append((description, script, args, kwargs))
            stdout = ""
            if script == "handoff_ops.py" and args[:1] == ("deactivate",):
                stdout = '{"chat_id":"chat-123"}'
            return subprocess.CompletedProcess([script], 0, stdout=stdout, stderr="")

        end_and_cleanup.script_utils.run_tool = fake_run_tool  # type: ignore
        end_and_cleanup.handoff_lifecycle.reset_working_card = lambda sid: None  # type: ignore

    def tearDown(self):
        end_and_cleanup.script_utils.run_tool = self._orig_run_tool  # type: ignore
        end_and_cleanup.handoff_lifecycle.reset_working_card = self._orig_reset_working  # type: ignore

    def _run(self, argv):
        old_argv = sys.argv
        sys.argv = ["end_and_cleanup.py"] + argv
        try:
            return end_and_cleanup.main()
        finally:
            sys.argv = old_argv

    def test_normal_flow_calls_all_steps(self):
        rc = self._run(["--session-model", "opus"])
        self.assertEqual(rc, 0)
        expected_scripts = [
            ("send handback card", "handoff_ops.py"),
            ("tabs-end", "handoff_ops.py"),
            ("deactivate", "handoff_ops.py"),
            ("restore iTerm profile", "iterm2_silence.py"),
        ]
        self.assertEqual([c[:2] for c in self.run_calls], expected_scripts)

    def test_dissolve_uses_chat_id_from_deactivate(self):
        rc = self._run(["--session-model", "opus", "--dissolve"])
        self.assertEqual(rc, 0)
        scripts = [c[1:3] for c in self.run_calls]
        self.assertIn(
            ("handoff_ops.py", ("remove-user", "--chat-id", "chat-123")), scripts
        )
        self.assertIn(
            ("handoff_ops.py", ("dissolve-chat", "--chat-id", "chat-123")), scripts
        )
        self.assertIn(
            ("handoff_ops.py", ("cleanup-sessions", "--chat-id", "chat-123")), scripts
        )

    def test_dissolve_requires_chat_id_when_skipping_deactivate(self):
        buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = buf
        try:
            rc = self._run(
                [
                    "--session-model",
                    "opus",
                    "--dissolve",
                    "--skip-deactivate",
                ]
            )
        finally:
            sys.stderr = old_stderr
        self.assertEqual(rc, 1)
        self.assertIn("chat_id is required", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
