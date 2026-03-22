#!/usr/bin/env python3
"""Block until a new Lark reply arrives in the handoff group.

Connects via WebSocket (preferred) for instant push with minimal quota
usage, falling back to HTTP long-polling if WebSocket fails.

Outputs the reply JSON to stdout and exits.
Used by handoff mode to wait for user input from Lark.
"""

import argparse
import json
import os
import random
import re
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import handoff_config
import handoff_db
import handoff_worker
import lark_im


def warn(msg):
    print(f"[handoff] {msg}", file=sys.stderr)


def filter_self_bot(replies, bot_open_id):
    """Filter out messages sent by the bot itself.

    Server-side no longer filters bot messages so that other bots in the
    group can be heard. This client-side filter removes only messages from
    the bot's own open_id to prevent self-echo loops.
    """
    if not bot_open_id:
        return replies
    return [
        r for r in replies
        if not (r.get("sender_type") == "app" and r.get("sender_id") == bot_open_id)
    ]


def filter_by_operator(replies, operator_open_id):
    """Filter replies to only those from the operator (by open_id).

    Applied regardless of mode — ensures only the configured operator's
    messages are processed.  Relay messages (sender_type == "relay") always
    pass through.  Does not distinguish between human and bot senders —
    only sender_id matters (the bot's own messages are already removed
    by filter_self_bot upstream).
    """
    if not operator_open_id:
        return replies
    return [
        r for r in replies
        if r.get("sender_id") == operator_open_id
        or r.get("sender_type") == "relay"
    ]


def filter_by_allowed_senders(replies, operator_open_id, member_roles):
    """Filter replies to operator + whitelisted members.

    member_roles: dict mapping open_id → role ("guest" or "coowner").
    Tags each reply with "privilege": "owner", "coowner", or "guest".
    Applied when members are configured (both regular and sidecar mode).
    """
    allowed = {operator_open_id} if operator_open_id else set()
    roles = dict(member_roles or {})
    allowed |= set(roles)
    if not allowed:
        return replies
    filtered = []
    for r in replies:
        # Relay messages always pass through.
        if r.get("sender_type") == "relay":
            filtered.append(r)
            continue
        # Check sender_id regardless of sender_type (human or bot).
        # The bot's own messages are already removed by filter_self_bot upstream.
        sid = r.get("sender_id", "")
        if sid not in allowed:
            continue
        if sid == operator_open_id:
            r = dict(r, privilege="owner")
        elif sid in roles:
            r = dict(r, privilege=roles[sid])
        filtered.append(r)
    return filtered


def filter_bot_interactions(replies, bot_open_id):
    """Filter replies to only bot-directed interactions.

    A message passes the filter if ANY of these conditions is true:
    1. The message @-mentions the bot (mention markers are stripped from text)
    2. The message is a reply to a bot-sent message (parent_id in local DB)
    3. The message is a reaction/sticker (msg_type == "reaction" or "sticker")

    This replaces the narrower filter_bot_mentions — same @-mention logic
    plus reply-to-bot and reaction awareness.
    """
    filtered = []
    for r in replies:
        msg_type = r.get("msg_type", "")
        # Relay and reaction/sticker messages always pass through.
        if msg_type in ("relay", "reaction", "sticker"):
            filtered.append(r)
            continue

        # Condition 1: @-mention
        mentions = r.get("mentions") or []
        is_mentioned = bot_open_id and any(
            m.get("id") == bot_open_id for m in mentions
        )

        # Condition 2: reply to a bot-sent message
        parent_id = r.get("parent_id", "")
        is_reply_to_bot = bool(parent_id) and handoff_db.is_bot_sent_message(parent_id)

        if not is_mentioned and not is_reply_to_bot:
            continue

        # Strip @-mention markers from text if present
        text = r.get("text", "")
        if is_mentioned:
            for m in mentions:
                key = m.get("key", "")
                if key:
                    text = text.replace(key, "").strip()
            text = re.sub(r"\s+", " ", text).strip()
            r = dict(r, text=text)

        filtered.append(r)
    return filtered


def _extract_system_warnings(replies, chat_id):
    """Remove system_warning replies and send them as Lark cards.

    Returns the remaining (non-warning) replies.
    """
    warnings = [r for r in replies if r.get("msg_type") == "system_warning"]
    remaining = [r for r in replies if r.get("msg_type") != "system_warning"]
    for w in warnings:
        _send_system_warning(chat_id, w.get("text", "Unknown error"))
    return remaining


def _send_system_warning(chat_id, message):
    """Send a warning card to Lark about a worker-side error."""
    try:
        import handoff_config
        creds = handoff_config.load_credentials()
        if not creds:
            return
        token = lark_im.get_tenant_token(creds["app_id"], creds["app_secret"])
        card = {
            "header": {
                "title": {"tag": "plain_text", "content": "Worker warning"},
                "template": "orange",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"⚠️ {message}",
                    },
                },
            ],
        }
        lark_im.send_message(token, chat_id, card)
        warn(f"sent system warning card: {message}")
    except Exception as e:
        warn(f"failed to send system warning card: {e}")


def _send_quota_warning(chat_id):
    """Send a warning card to the Lark chat about DO quota exhaustion.

    Returns True if the warning was sent successfully, False otherwise.
    """
    try:
        import handoff_config
        creds = handoff_config.load_credentials()
        if not creds:
            return False
        token = lark_im.get_tenant_token(creds["app_id"], creds["app_secret"])
        card = {
            "header": {
                "title": {"tag": "plain_text", "content": "Worker quota exhausted"},
                "template": "orange",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            "⚠️ The Cloudflare Durable Objects free tier quota has been "
                            "exceeded. Messages sent here **will not reach Claude** until "
                            "the quota resets (daily).\n\n"
                            "Claude is still waiting and will resume automatically once "
                            "the quota resets.\n\n"
                            "To fix permanently: upgrade to Workers Paid ($5/month) in the "
                            "Cloudflare dashboard."
                        ),
                    },
                },
            ],
        }
        lark_im.send_message(token, chat_id, card)
        warn("sent DO quota warning card to Lark chat")
        return True
    except Exception as e:
        warn(f"failed to send DO quota warning card: {e}")
        return False


def fetch_replies_http(worker_url, chat_id, since):
    """HTTP long-poll the worker for replies. Returns (replies, takeover, error)."""
    result = handoff_worker.poll_worker(worker_url, chat_id, since)
    return result["replies"], result["takeover"], result["error"]


_ACK_REACTION_FILE = os.path.join(
    f"/private/tmp/claude-{os.getuid()}", "handoff-ack-reaction.json"
)


def _ack_with_reaction(replies):
    """Add a 'thinking' reaction to the last user message to show it was received.

    Saves the reaction info to a temp file so send scripts can remove it later.
    """
    last = replies[-1] if replies else None
    if not last or not last.get("message_id"):
        return
    # Only react to human/bot text messages, not reactions/stickers/relay
    if last.get("msg_type") in ("reaction", "sticker", "relay"):
        return
    # Clear any previous reaction before adding a new one
    clear_ack_reaction()

    try:
        creds = handoff_config.load_credentials()
        if not creds:
            return
        token = lark_im.get_tenant_token(creds["app_id"], creds["app_secret"])
        reaction_id = lark_im.add_reaction(token, last["message_id"], "THINKING")
        # Save reaction info for removal by send scripts
        with open(_ACK_REACTION_FILE, "w") as f:
            json.dump({
                "message_id": last["message_id"],
                "reaction_id": reaction_id,
            }, f)
    except Exception as e:
        warn(f"failed to add thinking reaction: {e}")


def clear_ack_reaction():
    """Remove the THINKING reaction added by _ack_with_reaction.

    Called by send scripts before sending a response, to signal
    that processing is complete. Fails silently.
    """
    try:
        if not os.path.isfile(_ACK_REACTION_FILE):
            return
        with open(_ACK_REACTION_FILE) as f:
            data = json.load(f)
        os.unlink(_ACK_REACTION_FILE)
        creds = handoff_config.load_credentials()
        if not creds:
            return
        token = lark_im.get_tenant_token(creds["app_id"], creds["app_secret"])
        lark_im.remove_reaction(token, data["message_id"], data["reaction_id"])
    except Exception as e:
        warn(f"failed to clear thinking reaction: {e}")


def handle_result(replies, worker_url, chat_id, session_id):
    """Update last_checked and output reply JSON."""
    # Acknowledge receipt with a reaction so the user sees we're processing
    _ack_with_reaction(replies)

    for r in replies:
        try:
            handoff_db.record_received_message(
                chat_id=chat_id,
                text=r.get("text", ""),
                title="",
                source_message_id=r.get("message_id", ""),
                message_time=r.get("create_time"),
            )
        except Exception as e:
            warn(f"failed to record received message for chat {chat_id}: {e}")

    last_checked = replies[-1]["create_time"]
    if session_id:
        try:
            handoff_db.set_session_last_checked(session_id, last_checked)
        except Exception as e:
            warn(f"failed to persist last_checked for session {session_id}: {e}")
    json.dump({"replies": replies, "count": len(replies)}, sys.stdout)


def main():
    parser = argparse.ArgumentParser(description="Wait for Lark handoff reply")
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Max seconds to wait (default: 540 for GPT, 0 for everything else)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=3,
        help="Backoff interval on errors in seconds (default: 3)",
    )
    parser.add_argument(
        "--no-ws",
        action="store_true",
        help="Disable WebSocket, force HTTP long-polling",
    )
    args = parser.parse_args()

    try:
        ctx = lark_im.resolve_session_context()
    except RuntimeError as e:
        json.dump({"error": str(e)}, sys.stdout)
        return

    session_id, chat_id = ctx["session_id"], ctx["chat_id"]
    session = ctx["session"]
    since = session.get("last_checked")
    operator_open_id = session.get("operator_open_id", "")
    bot_open_id = session.get("bot_open_id", "")
    sidecar_mode = session.get("sidecar_mode", False)
    guests = session.get("guests") or []
    member_roles = {g["open_id"]: g.get("role", "guest") for g in guests} if guests else {}

    # Apply model-based default timeout if not explicitly provided
    if args.timeout is None:
        args.timeout = handoff_config.default_poll_timeout(session)

    # Check for unprocessed messages (received but never processed by Claude,
    # e.g. after an API crash). Replay them before polling the worker.
    unprocessed = handoff_db.get_unprocessed_messages(chat_id)
    if unprocessed:
        warn(f"replaying {len(unprocessed)} unprocessed message(s)")
        json.dump({"replies": unprocessed, "count": len(unprocessed)}, sys.stdout)
        return

    _profile = session.get("config_profile", "default")
    worker_url = handoff_config.load_worker_url(profile=_profile)
    if not worker_url:
        json.dump({"error": "no_worker_url"}, sys.stdout)
        return

    # If no last_checked, set it to now (minus a buffer for clock skew
    # between this machine and the CF Worker / Lark server).
    if not since:
        since = str(int(time.time() * 1000) - 5000)

    deadline = None if args.timeout <= 0 else time.time() + args.timeout
    backoff = args.interval
    use_ws = not args.no_ws
    quota_warned = False  # Send the Lark warning card at most once per session

    while deadline is None or time.time() < deadline:
        # --- Try WebSocket first (much lower quota usage) ---
        if use_ws:
            try:
                result = handoff_worker.poll_worker_ws(worker_url, chat_id, since)
                if result.get("takeover"):
                    json.dump({"takeover": True}, sys.stdout)
                    return
                replies = result.get("replies", [])
                if replies:
                    replies = _extract_system_warnings(replies, chat_id)
                if replies:
                    replies = filter_self_bot(replies, bot_open_id)
                if replies:
                    if member_roles:
                        replies = filter_by_allowed_senders(
                            replies, operator_open_id, member_roles)
                    else:
                        replies = filter_by_operator(replies, operator_open_id)
                if sidecar_mode and replies:
                    replies = filter_bot_interactions(replies, bot_open_id)
                if replies:
                    handle_result(replies, worker_url, chat_id, session_id)
                    return
                error = result.get("error")
                if error:
                    raise Exception(error)
                # No replies and no error — shouldn't happen with WS (it blocks),
                # but treat as needing a retry.
                continue
            except Exception as e:
                print(
                    f"[handoff] WebSocket error: {e} — falling back to HTTP",
                    file=sys.stderr,
                )
                # Fall through to HTTP long-poll for this cycle

        # --- HTTP long-poll fallback ---
        try:
            replies, takeover, error = fetch_replies_http(
                worker_url,
                chat_id,
                since,
            )
            if error:
                if not quota_warned and handoff_worker.is_do_quota_error(error):
                    quota_warned = _send_quota_warning(chat_id)
                jitter = random.uniform(0, backoff)
                print(
                    f"[handoff] {error} — retrying in {jitter:.1f}s "
                    f"(Esc/Ctrl+C to end handoff)",
                    file=sys.stderr,
                )
                time.sleep(jitter)
                backoff = min(backoff * 2, 60)
                continue
            backoff = args.interval
            if takeover:
                json.dump({"takeover": True}, sys.stdout)
                return
            if replies:
                # Ack processed replies via HTTP (WS already acks inline)
                last_checked = replies[-1]["create_time"]
                handoff_worker.ack_worker_replies(worker_url, chat_id, last_checked)
                replies = _extract_system_warnings(replies, chat_id)
                if not replies:
                    continue
                replies = filter_self_bot(replies, bot_open_id)
                if not replies:
                    continue
                if member_roles:
                    replies = filter_by_allowed_senders(
                        replies, operator_open_id, member_roles)
                else:
                    replies = filter_by_operator(replies, operator_open_id)
                if sidecar_mode:
                    replies = filter_bot_interactions(replies, bot_open_id)
                if replies:
                    handle_result(replies, worker_url, chat_id, session_id)
                    return
        except Exception as e:
            jitter = random.uniform(0, backoff)
            print(
                f"[handoff] fetch error: {e} — retrying in {jitter:.1f}s "
                f"(Esc/Ctrl+C to end handoff)",
                file=sys.stderr,
            )
            time.sleep(jitter)
            backoff = min(backoff * 2, 60)

    json.dump({"timeout": True, "replies": [], "count": 0}, sys.stdout)


if __name__ == "__main__":
    main()
