#!/usr/bin/env python3
"""Send a message to the current handoff's Lark chat group.

Used by handoff mode to send Claude's output to Lark.
Each project gets its own Lark group. Messages are sent as
top-level messages (no threading).
"""

import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import handoff_config
import handoff_db
import handoff_worker
import lark_im


def warn(msg):
    print(f"[handoff] {msg}", file=sys.stderr)


def get_worktree_name():
    """Get worktree name from git, falling back to project folder name."""
    return handoff_config.get_worktree_name() or os.path.basename(handoff_config._require_project_dir())


def _workspace_tag_matches(desc, tag):
    """Check if description contains the exact workspace tag (not a prefix).

    The tag (e.g. 'workspace:CarbonMac-Users-foo-bar') must appear as a
    complete token — followed by whitespace, newline, or end of string.
    This prevents 'workspace:A-B' from matching 'workspace:A-B-C'.
    """
    start = 0
    while True:
        idx = desc.find(tag, start)
        if idx < 0:
            return False
        end = idx + len(tag)
        if end >= len(desc) or desc[end] in (" ", "\n", "\r", "\t"):
            return True
        start = end


def find_groups_for_workspace(token, workspace_id, open_id=None):
    """Find all Lark groups tagged with this workspace ID.

    Groups store the workspace ID in their description field.
    Requires workspace:{id} tag to match. If open_id is provided,
    only returns chats where the user is an actual member.
    Returns list of dicts: [{chat_id, name, description}].
    """
    workspace_tag = f"workspace:{workspace_id}"
    groups = []
    try:
        chats = lark_im.list_bot_chats(token)
        for chat in chats:
            cid = chat.get("chat_id", "")
            if not cid:
                continue
            try:
                info = lark_im.get_chat_info(token, cid)
                desc = info.get("description") or ""
                if not _workspace_tag_matches(desc, workspace_tag):
                    continue
                # If open_id is provided, verify user is a member of this chat
                if open_id:
                    try:
                        members = lark_im.list_chat_members(token, cid)
                        member_ids = {m.get("member_id") for m in members}
                        if open_id not in member_ids:
                            continue
                    except Exception as e:
                        warn(f"failed to check members of chat {cid}: {e}")
                        continue
                groups.append(
                    {
                        "chat_id": cid,
                        "name": info.get("name", ""),
                        "description": desc,
                    }
                )
            except Exception as e:
                warn(f"failed to inspect chat {cid}: {e}")
                continue
    except Exception as e:
        warn(f"failed to list bot chats: {e}")
    return groups


def find_external_groups(token, open_id=None):
    """Find Lark groups where bot is a member but NOT workspace-tagged.

    These are "external" groups the bot was manually added to.
    If open_id is provided, only returns chats where the user is a member.
    Returns list of dicts: [{chat_id, name, description}].
    """
    groups = []
    try:
        chats = lark_im.list_bot_chats(token)
        for chat in chats:
            cid = chat.get("chat_id", "")
            if not cid:
                continue
            try:
                info = lark_im.get_chat_info(token, cid)
                desc = info.get("description") or ""
                # Skip workspace-tagged groups (those are regular handoff groups)
                if "workspace:" in desc:
                    continue
                # If open_id is provided, verify user is a member of this chat
                if open_id:
                    try:
                        members = lark_im.list_chat_members(token, cid)
                        member_ids = {m.get("member_id") for m in members}
                        if open_id not in member_ids:
                            continue
                    except Exception as e:
                        warn(f"failed to check members of chat {cid}: {e}")
                        continue
                groups.append(
                    {
                        "chat_id": cid,
                        "name": info.get("name", ""),
                        "description": desc,
                    }
                )
            except Exception as e:
                warn(f"failed to inspect chat {cid}: {e}")
                continue
    except Exception as e:
        warn(f"failed to list bot chats: {e}")
    return groups


def find_group_by_name(token, name, open_id=None):
    """Find a bot group by name and return it with an ``external`` flag.

    Searches all groups the bot is a member of for a case-insensitive name
    match.  Returns ``{chat_id, name, description, external}`` where
    ``external`` is True when the group has no ``workspace:`` tag (i.e. not
    created by handoff).  Returns ``None`` if no match is found.

    If *open_id* is provided, only matches groups where that user is a member.
    """
    try:
        chats = lark_im.list_bot_chats(token)
        for chat in chats:
            cid = chat.get("chat_id", "")
            if not cid:
                continue
            try:
                info = lark_im.get_chat_info(token, cid)
                chat_name = info.get("name", "")
                if chat_name.lower() != name.lower():
                    continue
                desc = info.get("description") or ""
                if open_id:
                    try:
                        members = lark_im.list_chat_members(token, cid)
                        member_ids = {m.get("member_id") for m in members}
                        if open_id not in member_ids:
                            continue
                    except Exception as e:
                        warn(f"failed to check members of chat {cid}: {e}")
                        continue
                return {
                    "chat_id": cid,
                    "name": chat_name,
                    "description": desc,
                    "external": "workspace:" not in desc,
                }
            except Exception as e:
                warn(f"failed to inspect chat {cid}: {e}")
                continue
    except Exception as e:
        warn(f"failed to list bot chats: {e}")
    return None


def compute_next_group_name(worktree, machine, existing_names):
    """Compute the next numbered group name.

    First group: {worktree}@{machine}
    Subsequent: {worktree}2@{machine}, {worktree}3@{machine}, etc.
    """
    base = f"{worktree}@{machine}"
    if base not in existing_names:
        return base
    # Find highest suffix
    max_n = 1
    for name in existing_names:
        if name == base:
            continue
        # Match {worktree}N@{machine}
        prefix = f"{worktree}"
        suffix = f"@{machine}"
        if name.startswith(prefix) and name.endswith(suffix):
            mid = name[len(prefix) : -len(suffix)]
            try:
                n = int(mid)
                max_n = max(max_n, n)
            except ValueError:
                pass
    return f"{worktree}{max_n + 1}@{machine}"


def create_handoff_group(
    token, open_id, worktree, machine, existing_names, workspace_id=None
):
    """Create a new handoff group with a numbered name.

    Returns chat_id of the new group.
    """
    group_name = compute_next_group_name(worktree, machine, existing_names)
    if not workspace_id:
        workspace_id = handoff_config.get_workspace_id()
    description = f"workspace:{workspace_id}"

    chat_id = lark_im.create_chat(token, group_name, description=description)
    try:
        lark_im.add_chat_members(token, chat_id, [open_id])
    except Exception as e:
        try:
            lark_im.dissolve_chat(token, chat_id)
        except Exception as cleanup_error:
            warn(f"failed to cleanup partially created chat {chat_id}: {cleanup_error}")
        raise RuntimeError(f"Failed to add user {open_id} to group: {e}")
    # Set group avatar
    avatar_path = os.path.join(SCRIPT_DIR, "handoff_avatar.png")
    if os.path.exists(avatar_path):
        try:
            lark_im.update_chat_avatar(token, chat_id, avatar_path)
        except Exception as e:
            warn(f"failed to set avatar for chat {chat_id}: {e}")
    return chat_id


def _reset_working_state():
    """Update working card to 'Done', then clear state and stop flag.

    Acquires the same file lock used by on_post_tool_use._send_or_update_working()
    to prevent a race where a concurrent PostToolUse hook reads the old msg_id,
    then overwrites the "Done" card back to "Working..." after we clear the DB.
    """
    import fcntl

    session_id = os.environ.get("HANDOFF_SESSION_ID", "")
    if not session_id:
        return

    tmp_dir = os.environ.get("HANDOFF_TMP_DIR", "/tmp/handoff")
    os.makedirs(tmp_dir, exist_ok=True)
    lock_path = os.path.join(tmp_dir, f"working-{session_id}.lock")

    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            # Update working card to "Done" before clearing
            msg_id = handoff_db.get_working_message(session_id)
            if msg_id:
                try:
                    _session = handoff_db.get_session(session_id)
                    _profile = _session.get("config_profile", "default") if _session else "default"
                    credentials = handoff_config.load_credentials(profile=_profile)
                    if credentials:
                        token = lark_im.get_tenant_token(
                            credentials["app_id"], credentials["app_secret"],
                        )
                        _, created_at, _ = handoff_db.get_working_state(session_id)
                        import time as _time
                        elapsed = int(_time.time()) - created_at if created_at else 0
                        if elapsed < 60:
                            body = f"Completed in {elapsed}s"
                        else:
                            mins = elapsed // 60
                            secs = elapsed % 60
                            body = f"Completed in {mins}m {secs}s"
                        done_card = lark_im.build_card("Done ✓", body=body, color="green")
                        lark_im.update_card_message(token, msg_id, done_card)
                except Exception:
                    pass  # Non-critical — card may already be gone
            handoff_db.clear_working_message(session_id)
            handoff_db.clear_autoapprove_message(session_id)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)

    # Clear stop flag — user sent a new message, so stop is stale
    _clear_stop_flag(session_id)


def _clear_stop_flag(session_id):
    """Remove the stop flag file for a session."""
    tmp_dir = os.environ.get("HANDOFF_TMP_DIR", "/tmp/handoff")
    flag_path = os.path.join(tmp_dir, f"stop-{session_id}.flag")
    try:
        os.unlink(flag_path)
    except FileNotFoundError:
        pass


def send(token, chat_id, title, message, is_card, color, buttons=None,
         mention_user_id=None):
    """Send a top-level message to the group. Returns message_id.

    mention_user_id: if provided, prepend an @-mention to the message body.
    Used when need_mention is set to notify the operator of each response.
    """
    _reset_working_state()
    # Prepend @-mention if requested (Lark Card V2 markdown syntax)
    if mention_user_id:
        message = f"<at id={mention_user_id}></at>\n{message}"
    if is_card:
        card = lark_im.build_card(
            title, body=message, color=color, buttons=buttons, chat_id=chat_id
        )
        msg_id = lark_im.send_message(token, chat_id, card)
    else:
        # Use card for rich markdown formatting (lists, code, bold, etc.)
        card = lark_im.build_markdown_card(message, title=title)
        msg_id = lark_im.send_message(token, chat_id, card)
    # Track heartbeat-style cards (--card, grey/no color, "Working" title) as
    # working messages so _reset_working_state() can update them to "Done ✓"
    # when the AI sends the real response via send_and_wait.py.
    if is_card and color == "grey":
        session_id = os.environ.get("HANDOFF_SESSION_ID", "")
        if session_id:
            try:
                handoff_db.set_working_message(session_id, msg_id)
            except Exception:
                pass
    # Record in local DB so we can resolve parent_id on replies
    try:
        handoff_db.record_sent_message(msg_id, text=message, title=title, chat_id=chat_id)
    except Exception as e:
        warn(f"failed to record sent message {msg_id}: {e}")
    # Register with worker so reactions can be routed to this chat
    try:
        session_id = os.environ.get("HANDOFF_SESSION_ID", "")
        _s = handoff_db.get_session(session_id) if session_id else None
        _send_profile = (_s.get("config_profile", "default") if _s else "default")
        worker_url = handoff_config.load_worker_url(profile=_send_profile)
        if worker_url:
            handoff_worker.register_message(worker_url, msg_id, chat_id, profile=_send_profile)
    except Exception as e:
        warn(f"failed to register message {msg_id} with worker: {e}")
    return msg_id


def main():
    parser = argparse.ArgumentParser(
        description="Send message to Lark handoff group",
    )
    parser.add_argument("message", help="Message text to send")
    parser.add_argument("--color", default="blue", help="Card color (for --card mode)")
    parser.add_argument("--title", default="", help="Title (optional)")
    parser.add_argument(
        "--card",
        action="store_true",
        help="Send as interactive card instead of rich text post",
    )
    parser.add_argument(
        "--buttons",
        default="",
        help='JSON array of buttons: [["label","action_value","type"], ...]',
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
        # Buttons require card mode
        args.card = True

    try:
        ctx = lark_im.resolve_session_context()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    token, chat_id = ctx["token"], ctx["chat_id"]
    title = args.title or ""

    # Clear the "thinking" reaction before sending
    try:
        import wait_for_reply
        wait_for_reply.clear_ack_reaction()
    except Exception:
        pass

    send(
        token,
        chat_id,
        title,
        args.message,
        args.card,
        args.color,
        buttons=buttons,
    )

    print("Sent.")


if __name__ == "__main__":
    main()
