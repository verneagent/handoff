#!/usr/bin/env python3
"""Send a response to Lark and immediately wait for the next reply.

Combines send_to_group.py + wait_for_reply.py into a single blocking call.
This eliminates the decision gap between sending and waiting, preventing
the model from exiting the handoff loop prematurely.

Output: JSON with the next reply (same format as wait_for_reply.py).
"""

import argparse
import json
import os
import random
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import handoff_config
import handoff_db
import handoff_worker
import lark_im
import send_to_group
import wait_for_reply


def warn(msg):
    print(f"[handoff] {msg}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Send response to Lark and wait for next reply",
    )
    parser.add_argument("message", help="Message text to send")
    parser.add_argument("--color", default="blue", help="Card color (for --card mode)")
    parser.add_argument("--title", default="", help="Title (optional)")
    parser.add_argument(
        "--card",
        action="store_true",
        help="Send as interactive card instead of rich text",
    )
    parser.add_argument(
        "--buttons",
        default="",
        help='JSON array of buttons: [["label","action_value","type"], ...]',
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Max seconds to wait for reply (default: 540 for GPT, 0 for everything else)",
    )
    parser.add_argument(
        "--mention-user-id",
        default=None,
        help="Open ID to @-mention in the message (overrides auto-detected default)",
    )
    args = parser.parse_args()

    # Interpret \n from CLI as real newlines
    args.message = args.message.replace("\\n", "\n")

    # Parse buttons
    buttons = None
    if args.buttons:
        try:
            buttons = json.loads(args.buttons)
        except json.JSONDecodeError:
            print("Error: invalid --buttons JSON", file=sys.stderr)
            sys.exit(1)
        args.card = True

    # --- Send phase (reuse send_to_group.send) ---
    try:
        ctx = lark_im.resolve_session_context()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    token, session_id, chat_id = ctx["token"], ctx["session_id"], ctx["chat_id"]

    # Apply model-based default timeout if not explicitly provided
    if args.timeout is None:
        args.timeout = handoff_config.default_poll_timeout(ctx.get("session"))

    # When need_mention is set, @-mention the target user so they get notified.
    # --mention-user-id overrides (for guest replies); otherwise default to operator.
    session_for_send = ctx.get("session") or {}
    mention_user_id = args.mention_user_id if hasattr(args, "mention_user_id") else None
    if not mention_user_id and session_for_send.get("need_mention"):
        mention_user_id = session_for_send.get("operator_open_id") or None

    # Clear the "thinking" reaction before sending the response
    wait_for_reply.clear_ack_reaction()

    send_to_group.send(
        token,
        chat_id,
        args.title or "",
        args.message,
        args.card,
        args.color,
        buttons=buttons,
        mention_user_id=mention_user_id,
    )

    warn("Response sent. Waiting for next message...")

    # --- Wait phase (reuse wait_for_reply logic) ---
    # Re-read session to get fresh last_checked, operator_open_id, bot_open_id, need_mention, guests
    session = handoff_db.get_session(session_id)
    _profile = session.get("config_profile", "default") if session else "default"
    worker_url = handoff_config.load_worker_url(profile=_profile)
    if not worker_url:
        json.dump({"error": "no_worker_url"}, sys.stdout)
        return
    since = session.get("last_checked") if session else None
    operator_open_id = session.get("operator_open_id", "") if session else ""
    bot_open_id = session.get("bot_open_id", "") if session else ""
    need_mention = session.get("need_mention", False) if session else False
    guests = (session.get("guests") or []) if session else []
    member_roles = {g["open_id"]: g.get("role", "guest") for g in guests} if guests else {}
    if not since:
        since = str(int(time.time() * 1000) - 5000)

    deadline = None if args.timeout <= 0 else time.time() + args.timeout
    backoff = 3
    quota_warned = False

    while deadline is None or time.time() < deadline:
        # --- Try WebSocket first ---
        try:
            result = handoff_worker.poll_worker_ws(worker_url, chat_id, since, profile=_profile)
            if result.get("takeover"):
                json.dump({"takeover": True}, sys.stdout)
                return
            replies = result.get("replies", [])
            if replies:
                replies = wait_for_reply.filter_self_bot(replies, bot_open_id)
            if replies:
                if member_roles:
                    replies = wait_for_reply.filter_by_allowed_senders(
                        replies, operator_open_id, member_roles)
                else:
                    replies = wait_for_reply.filter_by_operator(replies, operator_open_id)
            if need_mention and replies:
                replies = wait_for_reply.filter_bot_interactions(replies, bot_open_id)
            if replies:
                wait_for_reply.handle_result(replies, worker_url, chat_id, session_id)
                return
            error = result.get("error")
            if error:
                raise Exception(error)
            continue
        except Exception as e:
            warn(f"WebSocket error: {e} — falling back to HTTP")

        # --- HTTP long-poll fallback ---
        try:
            replies, takeover, error = wait_for_reply.fetch_replies_http(
                worker_url, chat_id, since, profile=_profile
            )
            if error:
                if not quota_warned and handoff_worker.is_do_quota_error(error):
                    quota_warned = wait_for_reply._send_quota_warning(chat_id)
                jitter = random.uniform(0, backoff)
                warn(f"{error} — retrying in {jitter:.1f}s")
                time.sleep(jitter)
                backoff = min(backoff * 2, 60)
                continue

            backoff = 3
            if takeover:
                json.dump({"takeover": True}, sys.stdout)
                return
            if replies:
                last_checked = replies[-1]["create_time"]
                handoff_worker.ack_worker_replies(worker_url, chat_id, last_checked, profile=_profile)
                replies = wait_for_reply.filter_self_bot(replies, bot_open_id)
            if replies:
                if member_roles:
                    replies = wait_for_reply.filter_by_allowed_senders(
                        replies, operator_open_id, member_roles)
                else:
                    replies = wait_for_reply.filter_by_operator(replies, operator_open_id)
                if need_mention:
                    replies = wait_for_reply.filter_bot_interactions(replies, bot_open_id)
                if replies:
                    wait_for_reply.handle_result(
                        replies, worker_url, chat_id, session_id
                    )
                    return
        except Exception as e:
            jitter = random.uniform(0, backoff)
            warn(f"fetch error: {e} — retrying in {jitter:.1f}s")
            time.sleep(jitter)
            backoff = min(backoff * 2, 60)

    json.dump({"timeout": True, "replies": [], "count": 0}, sys.stdout)


if __name__ == "__main__":
    main()
