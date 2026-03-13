#!/usr/bin/env python3
"""PreToolUse hook for Bash: suppress noisy pattern warnings + stop flag.

Claude Code raises PreToolUse-level permission dialogs for commands containing
backticks, $() substitution, or newlines. These never reach PermissionRequest,
so they bypass Lark in handoff mode and show as CLI prompts in normal mode.

This hook:
1. Checks if the user pressed Stop in Lark — if so, denies the tool call.
2. Pre-approves all Bash commands EXCEPT those with
   dangerouslyDisableSandbox=true, which are deferred to PermissionRequest
   (and routed to Lark in handoff mode via permission_bridge.py).
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

    # Check stop flag — user pressed Stop in Lark
    flag_path = _stop_flag_path()
    if flag_path and os.path.exists(flag_path):
        print(json.dumps({
            "decision": "deny",
            "reason": "Stopped by user from Lark. The user pressed Stop on the working card.",
        }))
        return

    tool_input = hook_input.get("tool_input", {})
    if tool_input.get("dangerouslyDisableSandbox", False):
        sys.exit(0)  # Defer to PermissionRequest (CLI prompt or Lark)

    print('{"decision": "approve"}')


if __name__ == "__main__":
    main()
