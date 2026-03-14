#!/usr/bin/env python3
"""PreToolUse hook: stop flag check (all tools) + Bash auto-approve.

Runs on every tool call during handoff:
1. Checks if the user pressed Stop in Lark — if so, denies the tool call.
   First checks the local flag file (fast). If not found, polls the worker's
   /stop/ endpoint to catch stop signals that PostToolUse hasn't seen yet
   (e.g. when Claude is doing Read/Grep/Glob calls that don't trigger
   PostToolUse).
2. For Bash only: pre-approves commands EXCEPT those with
   dangerouslyDisableSandbox=true, which are deferred to PermissionRequest
   (and routed to Lark in handoff mode via permission_bridge.py).
3. For non-Bash tools: exits with no decision (default handling).
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
            # Write local flag file so future checks are instant
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

    # Check stop flag — user pressed Stop in Lark (applies to ALL tools)
    flag_path = _stop_flag_path()
    if flag_path and os.path.exists(flag_path):
        print(json.dumps({
            "decision": "deny",
            "reason": "Stopped by user from Lark. The user pressed Stop on the working card.",
        }))
        return

    # No local flag — check worker endpoint (catches stop signals that
    # PostToolUse hasn't seen yet, e.g. during Read/Grep/Glob calls)
    if flag_path and _check_worker_stop():
        print(json.dumps({
            "decision": "deny",
            "reason": "Stopped by user from Lark. The user pressed Stop on the working card.",
        }))
        return

    # Bash-specific: auto-approve unless dangerouslyDisableSandbox
    tool_name = hook_input.get("tool_name", "")
    if tool_name == "Bash":
        tool_input = hook_input.get("tool_input", {})
        if tool_input.get("dangerouslyDisableSandbox", False):
            sys.exit(0)  # Defer to PermissionRequest (CLI prompt or Lark)
        print('{"decision": "approve"}')
        return

    # Non-Bash tools: no decision (default handling)
    sys.exit(0)


if __name__ == "__main__":
    main()
