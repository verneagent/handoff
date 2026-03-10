#!/usr/bin/env python3
"""Single entrypoint for handoff script checks + tests.

Designed to be CI-friendly. Works from any location — paths are resolved
relative to this script's directory.

  python3 scripts/run_tests.py              # from repo root
  python3 .claude/skills/handoff/scripts/run_tests.py  # from project root
"""

import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.join(SCRIPT_DIR, "tests")


def _run(cmd):
    print(f"[handoff-test] $ {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode


def _script(name):
    return os.path.join(SCRIPT_DIR, name)


def _test(name):
    return os.path.join(TESTS_DIR, name)


def main():
    rc = _run(
        [
            sys.executable,
            "-m",
            "py_compile",
            _script("lark_im.py"),
            _script("send_to_group.py"),
            _script("wait_for_reply.py"),
            _script("permission_bridge.py"),
            _script("permission_core.py"),
            _script("worker_http.py"),
            _script("on_session_start.py"),
            _script("on_session_end.py"),
            _script("on_notification.py"),
            _script("on_post_tool_use.py"),
            _script("handoff_ops.py"),
            _script("send_and_wait.py"),
            _script("preflight.py"),
            _test("test_handoff_db_unit.py"),
            _test("test_handoff_simulation.py"),
            _test("test_handoff_ops_unit.py"),
            _test("test_permission_core.py"),
            _test("test_worker_http.py"),
            _test("test_permission_bridge.py"),
            _test("test_on_notification.py"),
            _test("test_send_and_wait.py"),
            _test("test_preflight.py"),
            _test("test_on_post_tool_use.py"),
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
            TESTS_DIR,
            "-p",
            "test_*.py",
        ]
    )


if __name__ == "__main__":
    sys.exit(main())
