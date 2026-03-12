#!/usr/bin/env python3
"""Hook script: send a Lark message when Claude Code needs human attention.

Uses the Lark IM API. Routes to the active handoff group.
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import handoff_config
import handoff_db
import lark_im

COLORS = {
    "permission_prompt": "orange",
    "idle_prompt": "blue",
    "elicitation_dialog": "purple",
    "quota_exceeded": "red",
    "usage_warning": "orange",
    "rate_limit": "orange",
}


def warn(msg):
    print(f"[handoff] {msg}", file=sys.stderr)


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception as e:
        warn(f"invalid Notification hook input JSON: {e}")
        hook_input = {}

    notification_type = hook_input.get("notification_type", "unknown")
    message = hook_input.get("message", "Claude Code needs your attention")

    # Check if this session has an active handoff
    session_id = hook_input.get("session_id", "")
    session = handoff_db.get_session(session_id) if session_id else None
    if not session:
        return  # No active handoff for this session

    # Set active profile from session so config loads use the right profile
    handoff_config.set_active_profile(session.get("config_profile", "default"))

    # During handoff, permission_prompt is handled by permission_bridge.py
    if notification_type == "permission_prompt":
        return

    # Always forward quota/usage/rate limit notifications to Lark
    critical_types = ("quota_exceeded", "usage_warning", "rate_limit")

    # Skip tool-running progress notifications — noise during handoff
    # But allow critical notifications and specific dialog types
    if (
        notification_type not in ("idle_prompt", "elicitation_dialog")
        and notification_type not in critical_types
    ):
        return

    # idle_prompt during handoff: the model may have lost the handoff loop
    # (e.g. after context compaction). Output recovery instructions to stdout
    # as a best-effort nudge — this text may or may not reach the model
    # depending on how Claude Code processes hook output during idle.
    if notification_type == "idle_prompt":
        chat_id = session.get("chat_id", "")
        print(
            f"[Handoff loop recovery] Handoff is active (chat_id: {chat_id}) "
            f"but the session went idle. Resume the loop:\n"
            f"1. python3 .claude/skills/handoff/scripts/wait_for_reply.py --timeout 0 "
            f"(dangerouslyDisableSandbox: true, Bash timeout: 600000)\n"
            f"2. Process reply, send response via send_to_group.py "
            f"(dangerouslyDisableSandbox: true)\n"
            f"3. Go to step 1"
        )
        return

    chat_id = session["chat_id"]

    color = COLORS.get(notification_type, "blue")
    prefix = handoff_config.get_worktree_name()
    title = f"[{prefix}] {message}"

    credentials = handoff_config.load_credentials()
    if not credentials:
        return

    try:
        token = lark_im.get_tenant_token(
            credentials["app_id"],
            credentials["app_secret"],
        )
    except Exception as e:
        warn(f"failed to get tenant token for notification: {e}")
        return

    card = lark_im.build_markdown_card(message, title=title, color=color)

    # Send to active handoff group
    try:
        msg_id = lark_im.send_message(token, chat_id, card)
        try:
            handoff_db.record_sent_message(
                msg_id, text=message, title=title, chat_id=chat_id
            )
        except Exception as e:
            warn(f"failed to record notification message {msg_id}: {e}")
    except Exception as e:
        warn(f"failed to send notification to chat {chat_id}: {e}")


if __name__ == "__main__":
    main()
