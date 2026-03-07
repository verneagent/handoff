#!/usr/bin/env python3
"""Single entrypoint for handoff script checks + tests.

Designed to be CI-friendly:
  python3 .claude/skills/handoff/scripts/run_tests.py
"""

import subprocess
import sys


def _run(cmd):
    print(f"[handoff-test] $ {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode


def main():
    rc = _run(
        [
            sys.executable,
            "-m",
            "py_compile",
            ".claude/skills/handoff/scripts/lark_im.py",
            ".claude/skills/handoff/scripts/send_to_group.py",
            ".claude/skills/handoff/scripts/wait_for_reply.py",
            ".claude/skills/handoff/scripts/permission_bridge.py",
            ".claude/skills/handoff/scripts/permission_core.py",
            ".claude/skills/handoff/scripts/worker_http.py",
            ".claude/skills/handoff/scripts/on_session_start.py",
            ".claude/skills/handoff/scripts/on_session_end.py",
            ".claude/skills/handoff/scripts/on_notification.py",
            ".claude/skills/handoff/scripts/on_post_tool_use.py",
            ".claude/skills/handoff/scripts/handoff_ops.py",
            ".claude/skills/handoff/scripts/send_and_wait.py",
            ".claude/skills/handoff/scripts/preflight.py",
            ".claude/skills/handoff/scripts/tests/test_handoff_db_unit.py",
            ".claude/skills/handoff/scripts/tests/test_handoff_simulation.py",
            ".claude/skills/handoff/scripts/tests/test_handoff_ops_unit.py",
            ".claude/skills/handoff/scripts/tests/test_permission_core.py",
            ".claude/skills/handoff/scripts/tests/test_worker_http.py",
            ".claude/skills/handoff/scripts/tests/test_permission_bridge.py",
            ".claude/skills/handoff/scripts/tests/test_on_notification.py",
            ".claude/skills/handoff/scripts/tests/test_send_and_wait.py",
            ".claude/skills/handoff/scripts/tests/test_preflight.py",
            ".claude/skills/handoff/scripts/tests/test_on_post_tool_use.py",
        ]
    )
    if rc != 0:
        return rc

    return _run(
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            ".claude/skills/handoff/scripts/tests",
            "-p",
            "test_*.py",
        ]
    )


if __name__ == "__main__":
    sys.exit(main())
