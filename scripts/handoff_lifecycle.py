#!/usr/bin/env python3
"""Handoff lifecycle: reusable start/end logic for CLI and agent modes.

Extracted from start_and_wait.py and end_and_cleanup.py so both the CLI
handoff loop and the Agent SDK agent process can share the same lifecycle code.
"""

import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import handoff_config
import handoff_db
import lark_im


def _get_credentials(session=None):
    """Load credentials, optionally using the session's profile."""
    profile = (session or {}).get("config_profile", "default")
    return handoff_config.load_credentials(profile=profile)


def _get_token(session=None):
    """Get a Lark tenant token using the session's profile credentials."""
    credentials = _get_credentials(session)
    if not credentials:
        return None
    return lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])


def send_start_card(session_id, model, tool_name="Claude Agent SDK"):
    """Send the handoff start card to the Lark chat."""
    session = handoff_db.get_session(session_id)
    if not session:
        return None
    token = _get_token(session)
    if not token:
        return None
    chat_id = session["chat_id"]

    title = f"Handoff from {tool_name} ({model})"
    body = "Handed off to Lark. Reply here to continue working."
    # Green for CLI, blue for Agent SDK — lets users distinguish modes at a glance
    color = "blue" if tool_name == "Claude Agent SDK" else "green"
    card = lark_im.build_card(title, body=body, color=color)
    msg_id = lark_im.send_message(token, chat_id, card)
    handoff_db.record_sent_message(msg_id, text=body, title=title, chat_id=chat_id)
    return msg_id


def send_end_card(session_id, model, tool_name="Claude Agent SDK", body=""):
    """Send the handoff end card to the Lark chat."""
    session = handoff_db.get_session(session_id)
    if not session:
        return None
    token = _get_token(session)
    if not token:
        return None
    chat_id = session["chat_id"]

    title = f"Hand back to {tool_name}"
    if not body:
        body = "Handing back to CLI."
    color = "blue" if tool_name == "Claude Agent SDK" else "green"
    card = lark_im.build_card(title, body=body, color=color)
    msg_id = lark_im.send_message(token, chat_id, card)
    handoff_db.record_sent_message(msg_id, text=body, title=title, chat_id=chat_id)
    return msg_id


def reset_working_card(session_id):
    """Update the 'Working...' card to 'Done ✓' and clear state/stop flag."""
    import fcntl

    session = handoff_db.get_session(session_id)
    tmp_dir = os.environ.get("HANDOFF_TMP_DIR", "/tmp/handoff")
    os.makedirs(tmp_dir, exist_ok=True)
    lock_path = os.path.join(tmp_dir, f"working-{session_id}.lock")

    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            msg_id = handoff_db.get_working_message(session_id)
            if msg_id:
                try:
                    token = _get_token(session)
                    if token:
                        _, created_at, _ = handoff_db.get_working_state(session_id)
                        elapsed = int(time.time()) - created_at if created_at else 0
                        if elapsed < 60:
                            body = f"Completed in {elapsed}s"
                        else:
                            mins = elapsed // 60
                            secs = elapsed % 60
                            body = f"Completed in {mins}m {secs}s"
                        done_card = lark_im.build_card("Done ✓", body=body, color="green")
                        lark_im.update_card_message(token, msg_id, done_card)
                except Exception:
                    pass
            handoff_db.clear_working_message(session_id)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)

    # Clear stop flag
    flag_path = os.path.join(tmp_dir, f"stop-{session_id}.flag")
    try:
        os.unlink(flag_path)
    except FileNotFoundError:
        pass


def activate(session_id, chat_id, model, operator_open_id="",
             bot_open_id="", sidecar_mode=False, config_profile="default"):
    """Activate a handoff session in the DB."""
    handoff_db.activate_handoff(
        session_id, chat_id,
        session_model=model,
        operator_open_id=operator_open_id,
        bot_open_id=bot_open_id,
        sidecar_mode=sidecar_mode,
        config_profile=config_profile,
    )


def deactivate(session_id):
    """Deactivate a handoff session. Returns chat_id or None."""
    handoff_db.clear_working_message(session_id)
    handoff_db.clear_autoapprove_message(session_id)
    return handoff_db.deactivate_handoff(session_id)


def _run_tabs(action, session_id, model):
    """Run tabs-start or tabs-end via handoff_ops.py."""
    import subprocess
    cmd = [
        sys.executable, os.path.join(SCRIPT_DIR, "handoff_ops.py"),
        f"tabs-{action}", "--session-model", model,
    ]
    try:
        subprocess.run(cmd, timeout=10, capture_output=True)
    except Exception:
        pass


def handoff_start(session_id, model, tool_name="Claude Agent SDK", silence=False):
    """Full handoff start sequence: optional silence → tabs → status card.

    Returns the message_id of the start card, or None on failure.
    """
    if silence:
        try:
            import subprocess
            subprocess.run(
                [sys.executable, os.path.join(SCRIPT_DIR, "iterm2_silence.py"), "on"],
                timeout=5, capture_output=True,
            )
        except Exception:
            pass

    _run_tabs("start", session_id, model)
    return send_start_card(session_id, model, tool_name)


def handoff_end(session_id, model, tool_name="Claude Agent SDK", body="",
                dissolve=False, silence=True):
    """Full handoff end sequence: working reset → end card → deactivate → optional silence.

    Returns the chat_id that was deactivated.
    """
    reset_working_card(session_id)
    send_end_card(session_id, model, tool_name, body)
    _run_tabs("end", session_id, model)

    chat_id = deactivate(session_id)

    if dissolve and chat_id:
        session = handoff_db.get_session(session_id)
        token = _get_token(session)
        if token:
            try:
                lark_im.remove_user_from_chat(token, chat_id)
            except Exception:
                pass
            try:
                lark_im.dissolve_chat(token, chat_id)
            except Exception:
                pass
        handoff_db.cleanup_sessions_for_chat(chat_id)

    if silence:
        try:
            import subprocess
            subprocess.run(
                [sys.executable, os.path.join(SCRIPT_DIR, "iterm2_silence.py"), "off"],
                timeout=5, capture_output=True,
            )
        except Exception:
            pass

    return chat_id
