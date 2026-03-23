#!/usr/bin/env python3
"""PermissionRequest hook: bridge permission prompts to Lark during handoff.

When handoff mode is active, this hook intercepts permission prompts, sends
them to the Lark chat group, polls for user approval, and returns allow/deny.

When handoff mode is NOT active, this hook exits 1 to fall through to the
normal CLI permission prompt.
"""

import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import handoff_db
import handoff_worker
import lark_im
from permission_core import (  # type: ignore
    build_permission_body,
    prepare_permission_request,
    resolve_permission_context,
    run_permission_poll_loop,
    send_permission_denied_card,
    update_permission_card,
)

# How long to wait for the user to respond (seconds).
# 0 = wait indefinitely (until the hook timeout in settings.json).
POLL_TIMEOUT = 0

# Debug log file (stderr goes to Claude Code, file log persists for diagnosis)
_LOG_FILE = os.path.join(
    os.environ.get("HANDOFF_TMP_DIR") or "/tmp/handoff",
    "permission-bridge-cc.log",
)


_LOG_MAX_BYTES = 256 * 1024  # 256 KB
_LOG_KEEP_BYTES = 128 * 1024  # keep newest 128 KB after rotation


def _rotate_log_if_needed():
    """Truncate log file to _LOG_KEEP_BYTES when it exceeds _LOG_MAX_BYTES."""
    try:
        size = os.path.getsize(_LOG_FILE)
        if size <= _LOG_MAX_BYTES:
            return
        with open(_LOG_FILE, "rb") as f:
            f.seek(size - _LOG_KEEP_BYTES)
            tail = f.read()
        # Skip to next newline to avoid a partial first line
        nl = tail.find(b"\n")
        if nl >= 0:
            tail = tail[nl + 1 :]
        with open(_LOG_FILE, "wb") as f:
            f.write(tail)
    except Exception:
        pass


def _log(msg):
    """Write to persistent log file for post-mortem diagnosis."""
    try:
        os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)
        _rotate_log_if_needed()
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(_LOG_FILE, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def warn(msg):
    print(f"[handoff] {msg}", file=sys.stderr)
    _log(msg)


def format_tool_description(tool_name, tool_input):
    """Build a human-readable description from tool_name and tool_input."""
    if not tool_input:
        return ""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        desc = tool_input.get("description", "")
        parts = []
        if desc:
            parts.append(desc)
        if cmd:
            # Truncate long commands
            display = cmd if len(cmd) <= 200 else cmd[:200] + "..."
            parts.append(f"`{display}`")
        return "\n".join(parts)
    if tool_name in ("Write", "Edit"):
        path = tool_input.get("file_path", "")
        if path:
            return f"File: `{path}`"
    if tool_name == "Read":
        path = tool_input.get("file_path", "")
        if path:
            return f"File: `{path}`"
    if tool_name == "AskUserQuestion":
        questions = tool_input.get("questions", [])
        if questions:
            parts = []
            for q in questions:
                text = q.get("question", "")
                if text:
                    parts.append(text)
                options = q.get("options", [])
                for i, opt in enumerate(options, 1):
                    label = opt.get("label", "")
                    desc = opt.get("description", "")
                    if desc:
                        parts.append(f"{i}. **{label}** — {desc}")
                    elif label:
                        parts.append(f"{i}. **{label}**")
            return "\n".join(parts)
    # Generic: show key=value pairs, truncated
    parts = []
    for k, v in tool_input.items():
        sv = str(v)
        if len(sv) > 100:
            sv = sv[:100] + "..."
        parts.append(f"**{k}:** `{sv}`")
    return "\n".join(parts[:5])


def _poll_worker(worker_url, chat_id, since, key=None, profile="default"):
    try:
        result = handoff_worker.poll_worker_ws(worker_url, chat_id, since, key=key, profile=profile)
        error = result.get("error")
        if error:
            raise Exception(error)
        return result
    except Exception as e:
        warn(f"WebSocket error: {e} — falling back to HTTP")
        return handoff_worker.poll_worker(worker_url, chat_id, since, key=key, profile=profile)


def _tool_summary(tool_name, tool_input):
    """Build a short one-line summary for a tool invocation."""
    if tool_name == "Bash":
        desc = tool_input.get("description", "")
        cmd = tool_input.get("command", "")
        if desc:
            return f"`Bash`: {desc}"
        if cmd:
            display = cmd if len(cmd) <= 80 else cmd[:77] + "..."
            return f"`Bash`: `{display}`"
        return "`Bash`"
    if tool_name in ("Edit", "Write", "Read"):
        path = tool_input.get("file_path", "")
        name = os.path.basename(path) if path else "file"
        return f"`{tool_name}: {name}`"
    return f"`{tool_name}`"


def _send_or_update_autoapprove(session_id, token, chat_id, tool_name, tool_input):
    """Send or update the merged 'Auto-approved' card, same pattern as Working hard."""
    import fcntl

    summary = _tool_summary(tool_name, tool_input)
    card = lark_im.build_card("Auto-approved", body=summary, color="grey")

    lock_dir = os.environ.get("HANDOFF_TMP_DIR", "/tmp/handoff")
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, f"autoapprove-{session_id}.lock")

    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            existing_msg_id = handoff_db.get_autoapprove_message(session_id)
            if existing_msg_id:
                try:
                    lark_im.update_card_message(token, existing_msg_id, card)
                    return
                except Exception as e:
                    _log(f"failed to update autoapprove card: {e}")
            try:
                msg_id = lark_im.send_message(token, chat_id, card)
                handoff_db.set_autoapprove_message(session_id, msg_id)
            except Exception as e:
                _log(f"failed to send autoapprove card: {e}")
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def deny_and_exit(tool_name, reason=""):
    """Return an explicit deny decision to Claude Code."""
    msg = f"Permission to use {tool_name} was denied via Lark."
    if reason:
        msg += f" ({reason})"
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": msg,
            },
        },
    }
    json.dump(output, sys.stdout)
    sys.exit(0)


def is_handoff_internal_command(tool_name, tool_input):
    """Check if this is a handoff internal command that should be auto-approved.

    Matches both full paths (/handoff/scripts/foo.py) and $SKILL_SCRIPTS
    variable patterns ($SKILL_SCRIPTS/foo.py) that the AI uses after
    resolving the scripts directory.
    """
    if tool_name != "Bash":
        return False

    cmd = tool_input.get("command", "")

    # Handoff internal scripts that should never require user approval.
    # Each script is matched by filename — works regardless of whether the
    # command uses a full path or the $SKILL_SCRIPTS variable.
    internal_scripts = [
        "check_active.py",
        "wait_for_reply.py",
        "send_and_wait.py",
        "send_to_group.py",
        "iterm2_silence.py",
        "enter_handoff.py",
        "start_and_wait.py",
        "end_and_cleanup.py",
        "handoff_ops.py",
        "team_status.py",
        "on_notification.py",
        "on_post_tool_use.py",
        "on_pre_compact.py",
        "on_session_start.py",
        "on_session_end.py",
        "preflight.py",
    ]

    # Check for SKILL_SCRIPTS variable usage or /handoff/scripts/ path
    if "$SKILL_SCRIPTS/" in cmd or "/handoff/scripts/" in cmd:
        return any(script in cmd for script in internal_scripts)

    return False


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception as e:
        warn(f"invalid hook input JSON: {e}")
        hook_input = {}

    tool_name = hook_input.get("tool_name", "unknown")
    tool_input = hook_input.get("tool_input", {})

    # Auto-approve handoff internal commands
    if is_handoff_internal_command(tool_name, tool_input):
        _log(f"auto-approve: handoff internal command")
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            },
        }
        json.dump(output, sys.stdout)
        sys.exit(0)

    message = format_tool_description(tool_name, tool_input)
    if not message:
        message = "Claude needs your permission"

    session_id = hook_input.get("session_id", "")
    _log(f"tool={tool_name} session={session_id}")
    context = resolve_permission_context(lark_im, session_id)
    if not context["ok"] and context["error"] in ("no_session_id", "inactive"):
        _log(f"exit(1): {context['error']}")
        sys.exit(1)  # No active handoff for this session, fall through to CLI

    if not context["ok"]:
        _log(f"deny: {context['error']}")
        deny_and_exit(tool_name, context["error"])
        return

    chat_id = context["chat_id"]
    token = context["token"]
    worker_url = context["worker_url"]
    _log(f"active: chat_id={chat_id}")

    # Resolve operator_open_id and coowner approvers from session
    session = handoff_db.get_session(session_id)
    operator_open_id = session.get("operator_open_id", "") if session else ""
    approver_ids = {operator_open_id} if operator_open_id else set()
    if session:
        for g in session.get("guests") or []:
            if g.get("role") == "coowner":
                approver_ids.add(g["open_id"])

    # Autoapprove: skip the permission card, send/update lightweight notification, auto-allow
    if session and session.get("autoapprove"):
        _log(f"autoapprove: auto-allowing {tool_name}")
        # Send or update the merged autoapprove notification card
        _send_or_update_autoapprove(session_id, token, chat_id, tool_name, tool_input)
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            },
        }
        json.dump(output, sys.stdout)
        sys.exit(0)

    # Generate nonce, ack stale replies, send card — all in one step.
    _perm_profile = session.get("config_profile", "default") if session else "default"
    perm_body = build_permission_body(tool_name, message)
    try:
        nonce, perm_msg_id = prepare_permission_request(
            lark_im,
            token,
            chat_id,
            tool_name,
            message,
            ack_fn=lambda key, before: handoff_worker.ack_worker_replies(
                worker_url, chat_id, before, key=key, profile=_perm_profile
            ),
            log_fn=warn,
            approver_ids=approver_ids,
        )
    except Exception as e:
        _log(f"card send failed: {e}")
        warn(f"failed to send permission request card: {e}")
        deny_and_exit(tool_name, "Failed to send permission request to Lark")
        return

    def on_deny():
        update_permission_card(lark_im, token, perm_msg_id, "deny", tool_name, perm_body)

    # Poll the nonce-keyed DO (not chat:{chatId}) for correlated replies only.
    decision, _ = run_permission_poll_loop(
        poll_fn=lambda chat_id, since: _poll_worker(
            worker_url, chat_id, since, key=nonce, profile=_perm_profile
        ),
        ack_fn=lambda chat_id, before: handoff_worker.ack_worker_replies(
            worker_url, chat_id, before, key=nonce, profile=_perm_profile
        ),
        record_received_fn=handoff_db.record_received_message,
        set_last_checked_fn=handoff_db.set_session_last_checked,
        on_deny_fn=on_deny,
        chat_id=chat_id,
        session_id=session_id,
        since="0",
        timeout_seconds=POLL_TIMEOUT,
        log_fn=warn,
        approver_ids=approver_ids,
    )

    _log(f"decision={decision}")

    # Update the original permission card to reflect the decision
    if decision in ("allow", "always"):
        update_permission_card(lark_im, token, perm_msg_id, decision, tool_name, perm_body)

    # Return decision to Claude Code
    behavior = "allow" if decision in ("allow", "always") else "deny"
    decision_obj = {"behavior": behavior}

    if decision == "always":
        # Pass permission_suggestions as updatedPermissions so Claude Code
        # persists "always allow" for this tool going forward.
        suggestions = hook_input.get("permission_suggestions", [])
        if suggestions:
            decision_obj["updatedPermissions"] = suggestions
            _log(f"updatedPermissions: {suggestions}")

    if decision == "deny":
        decision_obj["message"] = f"Permission to use {tool_name} was denied via Lark."

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision_obj,
        },
    }
    json.dump(output, sys.stdout)
    sys.exit(0)


if __name__ == "__main__":
    main()
