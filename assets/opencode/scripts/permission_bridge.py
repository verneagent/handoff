#!/usr/bin/env python3
"""Permission bridge for opencode handoff plugin.

Called by the opencode handoff plugin's permission.ask hook. Checks if
handoff is active, sends a permission card to Lark, polls for the user's
decision, and prints the result to stdout.

Environment variables:
    HANDOFF_TOOL_TYPE    — tool type (e.g. "bash", "edit", "read")
    HANDOFF_TOOL_MESSAGE — human-readable description of the action
    HANDOFF_SESSION_ID   — current opencode session ID
    HANDOFF_PROJECT_DIR  — project directory (for DB path computation)

Output (stdout): "allow", "always", "deny", or "ask"
    "ask"    — not in handoff mode, let TUI handle it
    "allow"  — user approved this once via Lark
    "always" — user approved and wants to always allow this type
    "deny"   — user denied, or error prevented bridging
"""

import os
import sys
import time

# Debug logging to file (stdout is reserved for the decision)
LOG = os.path.join(
    (os.environ.get("HANDOFF_TMP_DIR") or "/tmp/handoff"),
    "permission-bridge.log",
)


def _log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


# Import from shared scripts directory
# .opencode/scripts/permission_bridge.py → up 3 levels to project root
SHARED_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".claude",
    "skills",
    "handoff",
    "scripts",
)
_log(f"SHARED_SCRIPTS={SHARED_SCRIPTS} exists={os.path.isdir(SHARED_SCRIPTS)}")
sys.path.insert(0, SHARED_SCRIPTS)

import lark_im  # type: ignore
from permission_core import (  # type: ignore
    prepare_permission_request,
    resolve_permission_context,
    run_permission_poll_loop,
    send_permission_denied_card,
)
from worker_http import ack_worker_urllib, poll_worker_urllib  # type: ignore


def main():
    tool_type = os.environ.get("HANDOFF_TOOL_TYPE", "unknown")
    message = os.environ.get("HANDOFF_TOOL_MESSAGE", "")
    session_id = os.environ.get("HANDOFF_SESSION_ID", "")

    context = resolve_permission_context(lark_im, session_id)
    if not context["ok"] and context["error"] in ("no_session_id", "inactive"):
        print("ask")
        return

    if not context["ok"]:
        # Once we know handoff is active, NEVER print "ask" here.
        # Returning "ask" may let the TUI auto-approve.
        _log(f"context resolve failed: {context.get('error')}")
        print("deny")
        return

    chat_id = context["chat_id"]
    token = context["token"]
    worker_url = context["worker_url"]
    api_key = lark_im.load_api_key() or ""

    # Resolve operator_open_id and coowner approvers from session
    session = lark_im.get_session(session_id)
    operator_open_id = session.get("operator_open_id", "") if session else ""
    approver_ids = {operator_open_id} if operator_open_id else set()
    if session:
        for g in session.get("guests") or []:
            if g.get("role") == "coowner":
                approver_ids.add(g["open_id"])

    _log(f"chat_id={chat_id}, worker_url={worker_url}, operator={operator_open_id}")

    # Generate nonce, ack stale replies, send card — all in one step.
    # Use urllib-based ack (curl is blocked by BunShell's subprocess env)
    try:
        nonce = prepare_permission_request(
            lark_im, token, chat_id, tool_type, message,
            ack_fn=lambda key, before: ack_worker_urllib(
                worker_url, chat_id, before, api_key=api_key, log_fn=_log, key=key,
            ),
            log_fn=_log,
        )
    except Exception as e:
        _log(f"card send failed: {e}")
        print("deny")
        return

    def _poll(chat_id, since):
        # WebSocket primary (pure stdlib sockets, no curl needed).
        # HTTP long-poll blocks the DO from processing pushes, causing
        # card action button clicks to be delayed or missed.
        # max_duration=60 ensures the WS returns periodically so
        # run_permission_poll_loop can check its deadline.
        try:
            result = lark_im.poll_worker_ws(
                worker_url, chat_id, since, max_duration=60, key=nonce
            )
            error = result.get("error")
            if error:
                raise Exception(error)
            return result
        except Exception as e:
            _log(f"WebSocket error: {e} — falling back to HTTP")
        return poll_worker_urllib(
            worker_url,
            chat_id,
            since=since,
            timeout=25,
            api_key=api_key,
            key=nonce,
        )

    def _on_deny():
        send_permission_denied_card(lark_im, token, chat_id, tool_type)

    _log("starting poll loop")
    decision, _ = run_permission_poll_loop(
        poll_fn=_poll,
        ack_fn=lambda chat_id, before: ack_worker_urllib(
            worker_url,
            chat_id,
            before,
            api_key=api_key,
            log_fn=_log,
            key=nonce,
        ),
        record_received_fn=lark_im.record_received_message,
        set_last_checked_fn=lark_im.set_session_last_checked,
        on_deny_fn=_on_deny,
        chat_id=chat_id,
        session_id=session_id,
        since="0",
        timeout_seconds=300,
        log_fn=_log,
        approver_ids=approver_ids,
    )

    _log(f"decision={decision}")

    if decision == "always":
        print("always")
        return
    if decision == "allow":
        print("allow")
        return
    print("deny")


if __name__ == "__main__":
    main()
