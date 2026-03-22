#!/usr/bin/env python3
"""PreCompact hook: warn the Lark chat that context is being compacted.

Sends a warning card when auto-compaction is triggered so the user
knows the session may briefly lose context.
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import handoff_config
import handoff_db
import lark_im


def warn(msg):
    print(f"[handoff-pre-compact] {msg}", file=sys.stderr)


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception as e:
        warn(f"invalid PreCompact hook input JSON: {e}")
        return

    session_id = hook_input.get("session_id", "")
    if not session_id:
        return

    session = handoff_db.resolve_session(session_id)
    if not session:
        return

    # In sidecar mode, skip compaction warnings (external group, minimal noise)
    if session.get("sidecar_mode", False):
        return

    trigger = hook_input.get("trigger", "auto")

    chat_id = session["chat_id"]
    credentials = handoff_config.load_credentials()
    if not credentials:
        return

    try:
        token = lark_im.get_tenant_token(
            credentials["app_id"],
            credentials["app_secret"],
        )
    except Exception as e:
        warn(f"failed to get tenant token: {e}")
        return

    if trigger == "auto":
        body = "Context window is full. Auto-compacting conversation history."
    else:
        body = "Manual context compaction requested."

    card = lark_im.build_markdown_card(
        body, title="Context compacting...", color="orange"
    )

    try:
        msg_id = lark_im.send_message(token, chat_id, card)
        try:
            handoff_db.record_sent_message(
                msg_id, text=body, title="Context compacting...", chat_id=chat_id
            )
        except Exception:
            pass
    except Exception as e:
        warn(f"failed to send compaction warning to chat {chat_id}: {e}")


if __name__ == "__main__":
    main()
