#!/usr/bin/env python3
"""PreToolUse hook: stop flag check for all tools.

Runs on every tool call during handoff. Checks if the user pressed Stop
in Lark — if so, denies the tool call. First checks the local flag file
(fast). If not found, polls the worker's /stop/ endpoint to catch stop
signals that PostToolUse hasn't seen yet (e.g. during Read/Grep/Glob
calls that don't trigger PostToolUse).
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)


def _stop_flag_path():
    """Return the stop flag file path for the current session."""
    session_id = os.environ.get("HANDOFF_SESSION_ID", "")
    if not session_id:
        return None
    tmp_dir = os.environ.get("HANDOFF_TMP_DIR", "/tmp/handoff")
    return os.path.join(tmp_dir, f"stop-{session_id}.flag")


def _check_worker_stop():
    """Poll the worker's /stop/ endpoint and write local flag if stop is set.

    Returns True if stop signal was found, False otherwise.
    """
    try:
        import handoff_config
        import handoff_db

        session_id = os.environ.get("HANDOFF_SESSION_ID", "")
        if not session_id:
            return False

        session = handoff_db.get_session(session_id)
        if not session:
            return False

        chat_id = session.get("chat_id", "")
        if not chat_id:
            return False

        worker_url = handoff_config.load_worker_url()
        if not worker_url:
            return False

        api_key = handoff_config.load_api_key()
        if not api_key:
            return False

        import urllib.request
        stop_url = f"{worker_url}/stop/chat:{chat_id}"
        req = urllib.request.Request(
            stop_url,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())

        if data.get("stop"):
            flag_path = _stop_flag_path()
            if flag_path:
                os.makedirs(os.path.dirname(flag_path), exist_ok=True)
                with open(flag_path, "w") as f:
                    f.write("1")
            return True
    except Exception:
        pass  # Network errors should not block tool execution
    return False


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    # Only active during handoff sessions
    flag_path = _stop_flag_path()
    if not flag_path:
        sys.exit(0)

    # Check local stop flag (fast)
    if os.path.exists(flag_path):
        print(json.dumps({
            "decision": "deny",
            "reason": "Stopped by user from Lark. The user pressed Stop on the working card.",
        }))
        return

    # Check worker endpoint (catches stop signals PostToolUse hasn't seen)
    if _check_worker_stop():
        print(json.dumps({
            "decision": "deny",
            "reason": "Stopped by user from Lark. The user pressed Stop on the working card.",
        }))
        return

    # No stop signal — no decision (default handling)
    sys.exit(0)


if __name__ == "__main__":
    main()
