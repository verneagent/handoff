#!/usr/bin/env python3
"""Shared permission-bridge core logic for Claude and OpenCode."""

import os
import random
import sys
import time
import uuid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import handoff_config
import handoff_db


ALLOW_ALWAYS_TEXTS = {"always", "yes always", "always allow"}
ALLOW_TEXTS = {"y", "yes", "approve", "ok", "1"}
DENY_TEXTS = {"n", "no", "deny", "reject", "0"}
ACK_ALL_BEFORE_MAX_MS = "9999999999999"


def normalize_decision_text(text):
    return str(text or "").strip().lower()


def classify_decision(text):
    t = normalize_decision_text(text)
    if t in ALLOW_ALWAYS_TEXTS:
        return "always"
    if t in ALLOW_TEXTS:
        return "allow"
    if t in DENY_TEXTS:
        return "deny"
    return None


def build_permission_body(tool_name, message):
    return f"**Tool:** `{tool_name}`\n{message}"


def permission_buttons():
    return [
        ("Approve", "y", "primary"),
        ("Approve All", "always", "default"),
        ("Deny", "n", "danger"),
    ]


def send_permission_request_card(
    lark_im_mod, token, chat_id, tool_name, message, nonce=None,
    approver_ids=None,
):
    """Send a permission request card. Returns message_id."""
    body = build_permission_body(tool_name, message)
    extra_value = {}
    if approver_ids:
        extra_value["approvers"] = sorted(approver_ids)
    card = lark_im_mod.build_card(
        "Permission Request",
        body=body,
        color="orange",
        buttons=permission_buttons(),
        chat_id=chat_id,
        nonce=nonce,
        extra_value=extra_value or None,
    )
    return lark_im_mod.send_message(token, chat_id, card)


def send_permission_denied_card(lark_im_mod, token, chat_id, tool_name):
    card = lark_im_mod.build_card(
        "Permission Denied",
        body=f"`{tool_name}` — denied",
        color="red",
    )
    lark_im_mod.send_message(token, chat_id, card)


def update_permission_card(lark_im_mod, token, message_id, decision, tool_name, body):
    """Update the permission request card to reflect the decision.

    Changes the title and color based on whether the permission was
    approved, always-approved, or denied.
    """
    if not message_id:
        return
    title_map = {
        "allow": "Approved ✓",
        "always": "Approved ✓ (always)",
        "deny": "Denied ✗",
    }
    color_map = {
        "allow": "green",
        "always": "green",
        "deny": "red",
    }
    title = title_map.get(decision, "Permission Request")
    color = color_map.get(decision, "orange")
    card = lark_im_mod.build_card(title, body=body, color=color)
    try:
        lark_im_mod.update_card_message(token, message_id, card)
    except Exception:
        pass  # Non-critical — card may have been deleted


def generate_nonce():
    """Generate a unique nonce for correlating a permission request with its reply.

    The nonce is used as a Durable Object routing key so that each permission
    card's button click is delivered to the specific poll loop waiting for it,
    preventing cross-request confusion when multiple prompts are in flight.
    """
    return f"perm:{uuid.uuid4().hex[:12]}"


def prepare_permission_request(
    lark_im_mod, token, chat_id, tool_name, message, ack_fn, log_fn=None,
    approver_ids=None,
):
    """Generate nonce, ack stale replies, and send the permission card.

    Args:
        ack_fn: callable(key, before) — platform-specific ack implementation.
        log_fn: optional callable(msg) for debug logging.

    Returns:
        (nonce, message_id) tuple on success.

    Raises:
        Exception if the card send fails (caller should handle).
    """
    nonce = generate_nonce()
    if log_fn:
        log_fn(f"nonce={nonce}")

    # Ack stale replies on the nonce-keyed DO (should be empty, but defensive).
    try:
        ack_fn(key=nonce, before=ACK_ALL_BEFORE_MAX_MS)
    except Exception:
        if log_fn:
            log_fn("pre-ack on nonce DO failed (non-critical)")

    if log_fn:
        log_fn("acked old replies")

    msg_id = send_permission_request_card(
        lark_im_mod, token, chat_id, tool_name, message, nonce=nonce,
        approver_ids=approver_ids,
    )

    if log_fn:
        log_fn("card sent")

    return nonce, msg_id


def resolve_permission_context(lark_im_mod, session_id):
    """Resolve active session + Lark auth context.

    Returns dict:
      ok: bool
      error: one of no_session_id|inactive|no_chat_id|invalid_chat_id|no_credentials|token_error|no_worker_url
      chat_id, token, worker_url when ok

    Note: lark_im_mod parameter is kept for API compatibility but only
    get_tenant_token is used from it. Other functions are imported directly
    from handoff_config and handoff_db.
    """
    if not session_id:
        return {"ok": False, "error": "no_session_id"}

    session = handoff_db.resolve_session(session_id)
    if not session:
        return {"ok": False, "error": "inactive"}

    chat_id = session.get("chat_id")
    if not chat_id:
        return {"ok": False, "error": "no_chat_id"}

    if not handoff_config.is_valid_chat_id(chat_id):
        return {"ok": False, "error": "invalid_chat_id"}

    profile = session.get("config_profile", "default")
    credentials = handoff_config.load_credentials(profile=profile)
    if not credentials:
        return {"ok": False, "error": "no_credentials", "chat_id": chat_id}

    try:
        token = lark_im_mod.get_tenant_token(
            credentials["app_id"],
            credentials["app_secret"],
        )
    except Exception as e:
        return {
            "ok": False,
            "error": "token_error",
            "error_detail": str(e),
            "chat_id": chat_id,
        }

    worker_url = handoff_config.load_worker_url(profile=profile)
    if not worker_url:
        return {
            "ok": False,
            "error": "no_worker_url",
            "chat_id": chat_id,
            "token": token,
        }

    return {
        "ok": True,
        "chat_id": chat_id,
        "token": token,
        "worker_url": worker_url,
    }


def run_permission_poll_loop(
    *,
    poll_fn,
    ack_fn,
    record_received_fn,
    set_last_checked_fn,
    on_deny_fn,
    chat_id,
    session_id,
    since,
    timeout_seconds,
    log_fn,
    operator_open_id="",
    approver_ids=None,
):
    """Poll replies until a decision is reached.

    Returns tuple: (decision, last_time)
    where decision is one of: "allow", "always", "deny".

    approver_ids: set of open_ids that can approve/deny. If provided,
        only accept decisions from these users.
    operator_open_id: legacy param — used to build approver_ids if
        approver_ids is not provided.
    """
    # Build approver set: explicit param > legacy operator_open_id
    if approver_ids is None:
        approver_ids = {operator_open_id} if operator_open_id else set()

    deadline = None if timeout_seconds <= 0 else time.time() + timeout_seconds
    backoff = 0
    cursor = since or "0"

    while deadline is None or time.time() < deadline:
        if backoff > 0:
            time.sleep(random.uniform(backoff * 0.5, backoff))

        try:
            result = poll_fn(chat_id=chat_id, since=cursor)
            replies = result.get("replies", [])
            takeover = result.get("takeover", False)
            error = result.get("error")

            if takeover:
                return "deny", cursor

            if error:
                # Exponential backoff with jitter: 1, 2, 4, 8, 16s cap
                backoff = min((backoff * 2 if backoff else 1), 16)
                if log_fn:
                    log_fn(f"poll error: {error} (backoff={backoff})")
                continue

            backoff = 0
            if not replies:
                continue

            # Filter to approved senders only
            if approver_ids:
                replies = [
                    r for r in replies
                    if r.get("sender_id") in approver_ids
                ]
                if not replies:
                    continue

            for r in replies:
                try:
                    record_received_fn(
                        chat_id=chat_id,
                        text=r.get("text", ""),
                        title="",
                        source_message_id=r.get("message_id", ""),
                        message_time=r.get("create_time"),
                    )
                except Exception:
                    if log_fn:
                        log_fn("failed to record permission reply")

            last_time = replies[-1].get("create_time", cursor)

            for r in replies:
                decision = classify_decision(r.get("text", ""))
                if not decision:
                    continue

                ts = r.get("create_time", last_time)
                if ts:
                    try:
                        set_last_checked_fn(session_id, ts)
                    except Exception:
                        if log_fn:
                            log_fn("failed to persist last_checked")
                    try:
                        ack_fn(chat_id=chat_id, before=ts)
                    except Exception:
                        if log_fn:
                            log_fn("failed to ack permission reply")

                if decision == "deny":
                    try:
                        on_deny_fn()
                    except Exception:
                        if log_fn:
                            log_fn("failed to send deny confirmation")

                return decision, ts

            # No decision in this batch; advance cursor and continue.
            cursor = last_time
        except Exception as e:
            # Exponential backoff: 1, 2, 4, 8, 16s cap
            backoff = min((backoff * 2 if backoff else 1), 16)
            if log_fn:
                log_fn(f"poll loop error: {e} (backoff={backoff})")

    return "deny", cursor
