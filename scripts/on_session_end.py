#!/usr/bin/env python3
"""SessionEnd hook: notify Lark and clean up when a Claude session ends.

If handoff was active when the session ends (abnormal termination),
sends a notification to the Lark group and deactivates handoff.
Also restores terminal notifications.
"""

import json
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import handoff_config
import handoff_db
import lark_im


def warn(msg):
    print(f"[handoff] {msg}", file=sys.stderr)


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception as e:
        warn(f"invalid SessionEnd hook input JSON: {e}")
        hook_input = {}

    # Restore terminal notifications (safe to call even if not silenced)
    try:
        subprocess.run(
            [sys.executable, os.path.join(SCRIPT_DIR, "iterm2_silence.py"), "off"],
            timeout=5,
        )
    except Exception as e:
        warn(f"failed to restore terminal notifications: {e}")

    # Check if this session has an active handoff
    session_id = hook_input.get("session_id", "")
    session = handoff_db.get_session(session_id) if session_id else None
    if not session:
        return

    # Set active profile from session so config loads use the right profile
    handoff_config.set_active_profile(session.get("config_profile", "default"))

    chat_id = session.get("chat_id", "")

    sidecar_mode = session.get("sidecar_mode", False)

    # Deactivate handoff
    handoff_db.deactivate_handoff(session_id)

    if not chat_id:
        return

    # In sidecar mode, skip the Session Ended card (external group, no modifications)
    if sidecar_mode:
        return

    # Send notification to Lark
    credentials = handoff_config.load_credentials()
    if not credentials:
        return

    try:
        token = lark_im.get_tenant_token(
            credentials["app_id"],
            credentials["app_secret"],
        )
    except Exception as e:
        warn(f"failed to get tenant token for session-end notice: {e}")
        return

    try:
        msg_text = (
            "The Claude session has ended. Start a new session and "
            "use `/handoff` to reconnect."
        )
        card = lark_im.build_markdown_card(
            msg_text,
            title="Session Ended",
            color="grey",
        )
        msg_id = lark_im.send_message(token, chat_id, card)
        try:
            handoff_db.record_sent_message(
                msg_id, text=msg_text, title="Session Ended", chat_id=chat_id
            )
        except Exception as e:
            warn(f"failed to record session-end message {msg_id}: {e}")
    except Exception as e:
        warn(f"failed to send session-end message to chat {chat_id}: {e}")


if __name__ == "__main__":
    main()
