#!/usr/bin/env python3
"""PreToolUse hook for Bash: suppress noisy pattern warnings.

Claude Code raises PreToolUse-level permission dialogs for commands containing
backticks, $() substitution, or newlines. These never reach PermissionRequest,
so they bypass Lark in handoff mode and show as CLI prompts in normal mode.

This hook pre-approves all Bash commands EXCEPT those with
dangerouslyDisableSandbox=true, which are deferred to PermissionRequest
(and routed to Lark in handoff mode via permission_bridge.py).
"""

import json
import sys


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    if tool_input.get("dangerouslyDisableSandbox", False):
        sys.exit(0)  # Defer to PermissionRequest (CLI prompt or Lark)

    print('{"decision": "approve"}')


if __name__ == "__main__":
    main()
