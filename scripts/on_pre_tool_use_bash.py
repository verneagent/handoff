#!/usr/bin/env python3
"""PreToolUse hook: stop flag check (all tools) + Bash auto-approve.

Runs on every tool call during handoff:
1. Checks if the user pressed Stop in Lark — if so, denies the tool call.
2. For Bash only: pre-approves commands EXCEPT those with
   dangerouslyDisableSandbox=true, which are deferred to PermissionRequest
   (and routed to Lark in handoff mode via permission_bridge.py).
3. For non-Bash tools: exits with no decision (default handling).
"""

import json
import os
import sys


def _stop_flag_path():
    """Return the stop flag file path for the current session."""
    session_id = os.environ.get("HANDOFF_SESSION_ID", "")
    if not session_id:
        return None
    tmp_dir = os.environ.get("HANDOFF_TMP_DIR", "/tmp/handoff")
    return os.path.join(tmp_dir, f"stop-{session_id}.flag")


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
