#!/usr/bin/env python3
"""Lark IM API client for handoff messaging.

This module contains Lark-specific API functions. Non-Lark functionality
has been moved to:
  - handoff_config: credentials, worker URL, project identity, utilities
  - handoff_db: SQLite database (sessions, messages, guests, working state)
  - handoff_worker: Cloudflare Worker communication (HTTP/WS polling)
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from handoff_config import (
    CONFIG_FILE,
    handoff_tmp_dir,
)
from handoff_db import get_session, resolve_session

BASE_URL = "https://open.larksuite.com/open-apis"

from lark_auth import LarkAuth  # noqa: E402

_auth = LarkAuth(CONFIG_FILE)


def _cleanup_old_downloads(max_age_hours=24):
    """Remove downloaded files older than max_age_hours to prevent accumulation.

    Called automatically at module import time to clean up handoff-images/
    and handoff-files/ directories. Non-critical — errors are ignored.
    """
    try:
        base = handoff_tmp_dir()
        cutoff = time.time() - (max_age_hours * 3600)
        for subdir in ("handoff-images", "handoff-files"):
            dir_path = os.path.join(base, subdir)
            if not os.path.isdir(dir_path):
                continue
            for entry in os.scandir(dir_path):
                try:
                    if entry.is_file() and entry.stat().st_mtime < cutoff:
                        os.unlink(entry.path)
                except Exception:
                    pass
    except Exception:
        pass


_cleanup_old_downloads()


def get_tenant_token(app_id, app_secret):
    """Get or refresh tenant access token."""
    return _auth._get_tenant_token(app_id, app_secret)


def resolve_session_context():
    """Load credentials, get token, and resolve active handoff session.

    Convenience helper that combines the repeated boilerplate of:
    1. load_credentials()
    2. HANDOFF_SESSION_ID from env
    3. get_tenant_token()
    4. get_session() + chat_id extraction

    Returns:
        dict with keys: token, session_id, chat_id, session

    Raises:
        RuntimeError: on any missing or invalid state.
    """
    from handoff_config import load_credentials

    credentials = load_credentials()
    if not credentials:
        raise RuntimeError("No credentials configured")

    session_id = os.environ.get("HANDOFF_SESSION_ID", "")
    if not session_id:
        raise RuntimeError("HANDOFF_SESSION_ID is not set")

    token = get_tenant_token(credentials["app_id"], credentials["app_secret"])

    session = resolve_session(session_id)
    if not session:
        raise RuntimeError(f"No active session for {session_id}")

    chat_id = session.get("chat_id")
    if not chat_id:
        raise RuntimeError(f"Session {session_id} has no chat_id")

    return {
        "token": token,
        "session_id": session_id,
        "chat_id": chat_id,
        "session": session,
    }


# ---------------------------------------------------------------------------
# Card builders
# ---------------------------------------------------------------------------


def build_card(title, body="", color="blue", buttons=None, chat_id=None, nonce=None,
               extra_value=None):
    """Build a card dict with optional action buttons.

    buttons: list of (label, action_value, button_type) tuples.
        button_type: "primary", "danger", or "default".
    chat_id: chat ID for routing callbacks.
    nonce: optional unique ID for correlating this card's button clicks
        with the specific poll loop waiting for them.
    extra_value: optional dict merged into each button's value payload.
    """
    elements = []
    if body and body.strip():
        elements.append(
            {
                "tag": "div",
                "text": {"content": body, "tag": "lark_md"},
            }
        )
    _value_base = {
        "chat_id": chat_id or "",
        "title": title,
        "body": body[:500] if body else "",
    }
    if nonce:
        _value_base["nonce"] = nonce
    if extra_value:
        _value_base.update(extra_value)
    if buttons:
        actions = []
        for label, action_value, button_type in buttons:
            value = {**_value_base, "action": action_value}
            actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": label},
                    "type": button_type,
                    "value": value,
                }
            )
        elements.append({"tag": "action", "actions": actions})
    return {
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
        },
        "elements": elements,
    }


def build_markdown_card(content, title="", color=""):
    """Build a Card V2 with markdown content for rich text rendering.

    Uses Card JSON 2.0 schema with a markdown element. Supports full markdown
    including bold, italic, lists, code blocks, inline code, and blockquotes.

    content: markdown text to render.
    title: optional card header title. If empty, no header is shown.
    color: header color template (e.g. "blue", "green", "grey").
    """
    card = {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {
            "direction": "vertical",
            "elements": [
                {"tag": "markdown", "content": content},
            ],
        },
    }
    if title:
        card["header"] = {
            "title": {"tag": "plain_text", "content": title},
        }
        if color:
            card["header"]["template"] = color
    return card


def build_working_card(content, title="", color="grey", chat_id="",
                       show_stop=True):
    """Build a Card V2 markdown card with an optional Stop button.

    Used for the "Working..." card during tool execution. The Stop button
    sends a ``__stop__`` card action that the worker stores as a flag.
    """
    elements = [{"tag": "markdown", "content": content}]
    if show_stop:
        stop_value = {"action": "__stop__", "chat_id": chat_id}
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Stop"},
                "type": "default",
                "value": stop_value,
            }],
        })
    card = {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {"direction": "vertical", "elements": elements},
    }
    if title:
        card["header"] = {
            "title": {"tag": "plain_text", "content": title},
        }
        if color:
            card["header"]["template"] = color
    return card


def build_form_card(
    title,
    body="",
    color="blue",
    selects=None,
    inputs=None,
    checkers=None,
    submit_label="Submit",
    cancel_label=None,
    chat_id=None,
):
    """Build a Card V2 form card with select menus, inputs, and/or checkers.

    Uses Card JSON 2.0 with a form container. When the user clicks Submit,
    all form values are sent as a single callback with form_value dict.

    selects: list of (name, placeholder, options[, default[, label]]) tuples.
        name: field name in the form_value dict.
        placeholder: placeholder text for the dropdown.
        options: list of (label, value) tuples.
        default: optional default value. If omitted, first option is selected.
        label: optional bold label rendered above the dropdown.
    inputs: list of (name, placeholder) tuples.
        name: field name in the form_value dict.
        placeholder: placeholder text for the input.
    checkers: list of (name, label, checked) tuples.
        name: field name in the form_value dict.
        label: display text for the checkbox.
        checked: default checked state (bool).
    submit_label: text for the submit button.
    cancel_label: text for cancel button (rendered outside form). None to omit.
    chat_id: chat ID for routing callbacks.
    """
    form_elements = []
    if selects:
        for sel in selects:
            name, placeholder, options = sel[0], sel[1], sel[2]
            default = sel[3] if len(sel) > 3 else options[0][1] if options else None
            sel_label = sel[4] if len(sel) > 4 else None
            if sel_label:
                form_elements.append({"tag": "markdown", "content": f"**{sel_label}**"})
            el = {
                "tag": "select_static",
                "name": name,
                "placeholder": {"content": placeholder},
                "options": [
                    {"text": {"content": lbl}, "value": val} for lbl, val in options
                ],
            }
            if default is not None:
                el["initial_option"] = default
            form_elements.append(el)
    if checkers:
        for name, label, checked in checkers:
            form_elements.append(
                {
                    "tag": "checker",
                    "name": name,
                    "checked": checked,
                    "text": {"tag": "plain_text", "content": label},
                }
            )
    if inputs:
        for name, placeholder in inputs:
            form_elements.append(
                {
                    "tag": "input",
                    "name": name,
                    "placeholder": {"content": placeholder},
                }
            )
    form_elements.append(
        {
            "tag": "button",
            "text": {"content": submit_label},
            "type": "primary",
            "action_type": "form_submit",
            "name": "submit",
            "value": {
                "action": "form_submit",
                "chat_id": chat_id or "",
                "title": title,
                "body": body[:500] if body else "",
            },
        }
    )

    body_elements = []
    if body and body.strip():
        body_elements.append({"tag": "markdown", "content": body})
    body_elements.append(
        {
            "tag": "form",
            "name": "form",
            "elements": form_elements,
        }
    )
    if cancel_label:
        body_elements.append(
            {
                "tag": "button",
                "text": {"content": cancel_label},
                "type": "default",
                "value": {
                    "action": "__cancel__",
                    "chat_id": chat_id or "",
                },
            }
        )

    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
        },
        "body": {"elements": body_elements},
    }


# ---------------------------------------------------------------------------
# Card V2 fallback helpers
# ---------------------------------------------------------------------------

# Lark Card V2 server-side rendering error — intermittent outage
_CARD_CREATE_ERROR = 230099


def _is_v2_card(card):
    return isinstance(card, dict) and card.get("schema") == "2.0"


def _extract_card_text(card):
    """Extract title and body text from a card dict."""
    header = card.get("header", {})
    t = header.get("title", {})
    title = t.get("content", "") if isinstance(t, dict) else str(t)

    parts = []
    if _is_v2_card(card):
        for el in card.get("body", {}).get("elements", []):
            tag = el.get("tag")
            if tag == "markdown":
                parts.append(el.get("content", ""))
            elif tag == "form":
                for fel in el.get("elements", []):
                    if fel.get("tag") == "markdown":
                        parts.append(fel.get("content", ""))
    else:
        for el in card.get("elements", []):
            text = el.get("text", {})
            if isinstance(text, dict):
                parts.append(text.get("content", ""))

    return title, "\n".join(parts)


def _card_to_v1_fallback(card):
    """Convert a card to V1 with a degradation note."""
    title, body = _extract_card_text(card)
    note = "\n\n---\n_Lark Card V2 down — interactive elements disabled_"
    color = card.get("header", {}).get("template", "blue")
    return build_card(title, body=(body + note), color=color)


def _card_to_text_fallback(card):
    """Convert a card to plain text for ultimate fallback."""
    title, body = _extract_card_text(card)
    prefix = f"[{title}]\n" if title else ""
    return prefix + body + "\n(Lark Card V2 down)"


# ---------------------------------------------------------------------------
# Lark IM API
# ---------------------------------------------------------------------------


def _im_post(url, token, payload):
    """Send a POST to the Lark IM API. Returns response JSON dict.

    Handles HTTP error responses (4xx/5xx) by reading the JSON body from
    the HTTPError, so callers can inspect ``data["code"]`` for fallback logic.
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            raise e


def send_message(token, chat_id, card):
    """Send an interactive card to a chat. Returns message_id.

    If card creation fails (error 230099 — Lark Card V2 outage), falls back:
    V2 card → V1 card → plain text.
    """
    url = f"{BASE_URL}/im/v1/messages?receive_id_type=chat_id"
    payload = {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card),
    }
    data = _im_post(url, token, payload)

    if data.get("code") == 0:
        return data["data"]["message_id"]

    if data.get("code") == _CARD_CREATE_ERROR:
        print(
            f"[handoff] Card creation failed (230099), trying V1 fallback",
            file=sys.stderr,
        )
        fallback = _card_to_v1_fallback(card)
        payload["content"] = json.dumps(fallback)
        data = _im_post(url, token, payload)
        if data.get("code") == 0:
            return data["data"]["message_id"]

        # V1 also failed — send as plain text
        print(
            f"[handoff] V1 fallback also failed, sending as text",
            file=sys.stderr,
        )
        text = _card_to_text_fallback(card)
        payload["msg_type"] = "text"
        payload["content"] = json.dumps({"text": text})
        data = _im_post(url, token, payload)
        if data.get("code") == 0:
            return data["data"]["message_id"]

    raise RuntimeError(f"Failed to send message: {data}")


def update_card_message(token, message_id, card):
    """Update (PATCH) an existing card message with new content."""
    url = f"{BASE_URL}/im/v1/messages/{message_id}"
    payload = {"msg_type": "interactive", "content": json.dumps(card)}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read())
        except Exception:
            raise e
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to update message: {data}")


def delete_message(token, message_id):
    """Delete a bot-sent message from a chat."""
    url = f"{BASE_URL}/im/v1/messages/{message_id}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read())
        except Exception:
            raise e
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to delete message: {data}")


def reply_message(token, message_id, card):
    """Reply to a message (creates/continues thread). Returns new message_id.

    Same fallback logic as send_message for card creation failures.
    """
    url = f"{BASE_URL}/im/v1/messages/{message_id}/reply"
    payload = {
        "msg_type": "interactive",
        "content": json.dumps(card),
    }
    data = _im_post(url, token, payload)

    if data.get("code") == 0:
        return data["data"]["message_id"]

    if data.get("code") == _CARD_CREATE_ERROR:
        print(
            f"[handoff] Card creation failed (230099), trying V1 fallback",
            file=sys.stderr,
        )
        fallback = _card_to_v1_fallback(card)
        payload["content"] = json.dumps(fallback)
        data = _im_post(url, token, payload)
        if data.get("code") == 0:
            return data["data"]["message_id"]

        print(
            f"[handoff] V1 fallback also failed, sending as text",
            file=sys.stderr,
        )
        text = _card_to_text_fallback(card)
        payload["msg_type"] = "text"
        payload["content"] = json.dumps({"text": text})
        data = _im_post(url, token, payload)
        if data.get("code") == 0:
            return data["data"]["message_id"]

    raise RuntimeError(f"Failed to reply: {data}")


def list_chat_messages(token, chat_id):
    """List recent messages in a chat. Returns items list."""
    params = urllib.parse.urlencode(
        {
            "container_id_type": "chat",
            "container_id": chat_id,
            "sort_type": "ByCreateTimeDesc",
            "page_size": "50",
        }
    )
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/messages?{params}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to list messages: {data}")

    return data.get("data", {}).get("items", [])


def get_thread_replies(token, chat_id, root_message_id):
    """Get human replies to a specific thread.

    Returns:
        List of dicts with keys: text, msg_type, sender_type, create_time,
        message_id, and optionally image_key.
    """
    items = list_chat_messages(token, chat_id)
    replies = []
    for item in items:
        # Only replies to our thread from human users
        if item.get("root_id") != root_message_id:
            continue
        sender = item.get("sender", {})
        if sender.get("sender_type") == "app":
            continue
        create_time = item.get("create_time", "0")
        # Extract content based on message type
        msg_type = item.get("msg_type", "unknown")
        text = ""
        image_key = ""
        try:
            content = json.loads(item.get("body", {}).get("content", "{}"))
        except (json.JSONDecodeError, TypeError):
            content = {}

        if msg_type == "text":
            text = content.get("text", "")
        elif msg_type == "image":
            image_key = content.get("image_key", "")
            text = "[image]"
        else:
            text = f"[{msg_type} message]"

        reply = {
            "text": text,
            "msg_type": msg_type,
            "sender_type": sender.get("sender_type", "unknown"),
            "create_time": create_time,
            "message_id": item.get("message_id", ""),
        }
        if image_key:
            reply["image_key"] = image_key
        replies.append(reply)

    # Return in chronological order (API returns desc)
    replies.reverse()
    return replies


def get_bot_info(token):
    """Get the bot's own info (open_id, app_name). Uses GET /bot/v3/info."""
    req = urllib.request.Request(
        f"{BASE_URL}/bot/v3/info",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to get bot info: {data}")
    bot = data.get("bot", {})
    return {
        "open_id": bot.get("open_id", ""),
        "app_name": bot.get("app_name", ""),
    }


def list_bot_chats(token):
    """List all chats the bot is in. Paginates automatically."""
    all_items = []
    page_token = ""
    while True:
        params = "page_size=100"
        if page_token:
            params += f"&page_token={page_token}"
        req = urllib.request.Request(
            f"{BASE_URL}/im/v1/chats?{params}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())

        if data.get("code") != 0:
            raise RuntimeError(f"Failed to list chats: {data}")

        all_items.extend(data.get("data", {}).get("items", []))
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data.get("data", {}).get("page_token", "")
        if not page_token:
            break
    return all_items


def create_chat(token, name, description=""):
    """Create a new chat group. Returns chat_id."""
    payload = {
        "name": name,
        "description": description,
        "chat_mode": "group",
    }
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to create chat: {data}")

    return data["data"]["chat_id"]


def dissolve_chat(token, chat_id):
    """Dissolve (delete) a chat group. Returns True on success."""
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}",
        headers={"Authorization": f"Bearer {token}"},
        method="DELETE",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to dissolve chat: {data}")

    return True


def update_chat_avatar(token, chat_id, image_path):
    """Upload an image and set it as the chat group avatar."""
    image_key = upload_image(token, image_path, image_type="avatar")
    payload = {"avatar": image_key}
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="PUT",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to update chat avatar: {data}")


def add_chat_members(token, chat_id, open_ids):
    """Add members to a chat group by their open_ids."""
    payload = {"id_list": open_ids}
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}/members?member_id_type=open_id",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to add members: {data}")

    return data.get("data", {})


def remove_chat_members(token, chat_id, open_ids):
    """Remove members from a chat group by their open_ids."""
    payload = json.dumps({"id_list": open_ids}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}/members?member_id_type=open_id",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="DELETE",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to remove members: {data}")

    return data.get("data", {})


def get_chat_info(token, chat_id):
    """Get chat group info. Returns dict with name, owner_id, etc."""
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to get chat info: {data}")

    return data.get("data", {})


def list_chat_tabs(token, chat_id):
    """List chat tabs from left to right order."""
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}/chat_tabs/list_tabs",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to list chat tabs: {data}")

    return data.get("data", {}).get("chat_tabs", [])


def create_chat_tabs(token, chat_id, chat_tabs):
    """Create chat tabs. Returns full chat_tabs list."""
    payload = {"chat_tabs": chat_tabs}
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}/chat_tabs",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to create chat tabs: {data}")

    return data.get("data", {}).get("chat_tabs", [])


def update_chat_tabs(token, chat_id, chat_tabs):
    """Update chat tabs. Returns full chat_tabs list."""
    payload = {"chat_tabs": chat_tabs}
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}/chat_tabs/update_tabs",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to update chat tabs: {data}")

    return data.get("data", {}).get("chat_tabs", [])


def delete_chat_tabs(token, chat_id, tab_ids):
    """Delete chat tabs by IDs. Returns full chat_tabs list."""
    payload = {"tab_ids": tab_ids}
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}/chat_tabs/delete_tabs",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
        method="DELETE",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to delete chat tabs: {data}")

    return data.get("data", {}).get("chat_tabs", [])


def sort_chat_tabs(token, chat_id, tab_ids):
    """Sort chat tabs from left to right. Returns full chat_tabs list."""
    payload = {"tab_ids": tab_ids}
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}/chat_tabs/sort_tabs",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to sort chat tabs: {data}")

    return data.get("data", {}).get("chat_tabs", [])


def list_chat_members(token, chat_id):
    """List all members of a chat group. Paginates automatically."""
    all_items = []
    page_token = ""
    while True:
        params = f"member_id_type=open_id&page_size=100"
        if page_token:
            params += f"&page_token={page_token}"
        req = urllib.request.Request(
            f"{BASE_URL}/im/v1/chats/{chat_id}/members?{params}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())

        if data.get("code") != 0:
            raise RuntimeError(f"Failed to list members: {data}")

        all_items.extend(data.get("data", {}).get("items", []))
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data.get("data", {}).get("page_token", "")
        if not page_token:
            break
    return all_items


def lookup_open_id_by_email(token, email):
    """Look up a user's open_id by their Lark email.

    Uses the contact.v3.user.batch_get_id API. Requires the
    ``contact:user.id:readonly`` scope on the app.

    Args:
        token: Tenant access token.
        email: The user's Lark email address.

    Returns:
        The user's open_id string, or None if not found.
    """
    payload = json.dumps({"emails": [email]}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/contact/v3/users/batch_get_id?user_id_type=open_id",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    if data.get("code") != 0:
        raise RuntimeError(f"batch_get_id failed: {data}")
    user_list = data.get("data", {}).get("user_list", [])
    if user_list and user_list[0].get("user_id"):
        return user_list[0]["user_id"]
    return None


# ---------------------------------------------------------------------------
# Upload / download
# ---------------------------------------------------------------------------


def upload_image(token, image_path, image_type="message"):
    """Upload an image to Lark. Returns image_key.

    Args:
        token: Tenant access token.
        image_path: Local path to the image file.
        image_type: "message" for chat images, "avatar" for avatars.

    Returns:
        The image_key for the uploaded image.
    """
    import subprocess

    result = subprocess.run(
        [
            "curl",
            "-s",
            "--max-time",
            "30",
            "-X",
            "POST",
            f"{BASE_URL}/im/v1/images",
            "-H",
            f"Authorization: Bearer {token}",
            "-F",
            f"image_type={image_type}",
            "-F",
            f"image=@{image_path}",
        ],
        capture_output=True,
        text=True,
        timeout=35,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to upload image: {result.stderr}")

    data = json.loads(result.stdout)
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to upload image: {data}")

    return data["data"]["image_key"]


def reply_image(token, message_id, image_key):
    """Reply to a message with an image. Returns new message_id."""
    payload = {
        "msg_type": "image",
        "content": json.dumps({"image_key": image_key}),
    }
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/messages/{message_id}/reply",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to reply with image: {data}")

    return data["data"]["message_id"]


def upload_file(token, file_path, file_type="stream"):
    """Upload a file to Lark. Returns file_key.

    Args:
        token: Tenant access token.
        file_path: Local path to the file.
        file_type: One of "opus", "mp4", "pdf", "doc", "xls", "ppt",
                   "stream" (generic). Default "stream".

    Returns:
        The file_key for the uploaded file.
    """
    import subprocess

    file_name = os.path.basename(file_path)
    result = subprocess.run(
        [
            "curl",
            "-s",
            "--max-time",
            "30",
            "-X",
            "POST",
            f"{BASE_URL}/im/v1/files",
            "-H",
            f"Authorization: Bearer {token}",
            "-F",
            f"file_type={file_type}",
            "-F",
            f"file_name={file_name}",
            "-F",
            f"file=@{file_path}",
        ],
        capture_output=True,
        text=True,
        timeout=35,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to upload file: {result.stderr}")

    data = json.loads(result.stdout)
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to upload file: {data}")

    return data["data"]["file_key"]


def reply_file(token, message_id, file_key):
    """Reply to a message with a file attachment. Returns new message_id."""
    payload = {
        "msg_type": "file",
        "content": json.dumps({"file_key": file_key}),
    }
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/messages/{message_id}/reply",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to reply with file: {data}")

    return data["data"]["message_id"]


_MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB
_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
_DOWNLOAD_CHUNK = 64 * 1024  # 64 KB


def _download_with_limit(resp, save_path, max_bytes):
    """Stream *resp* to *save_path*, raising if *max_bytes* is exceeded."""
    written = 0
    with open(save_path, "wb") as f:
        while True:
            chunk = resp.read(_DOWNLOAD_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                f.close()
                try:
                    os.unlink(save_path)
                except OSError:
                    pass
                raise RuntimeError(
                    f"Download exceeds size limit "
                    f"({written} > {max_bytes} bytes): {save_path}"
                )
            f.write(chunk)
    return save_path


def download_image(token, image_key, message_id):
    """Download an image from Lark by image_key using the message resource API.

    Args:
        token: Tenant access token.
        image_key: The image key (e.g. "img_v3_xxx").
        message_id: The message ID that contains the image.

    Returns:
        The path where the image was saved.
    """
    img_dir = os.path.join(handoff_tmp_dir(), "handoff-images")
    os.makedirs(img_dir, exist_ok=True)
    save_path = os.path.join(img_dir, f"{image_key}.png")

    url = f"{BASE_URL}/im/v1/messages/{message_id}/resources/{image_key}?type=image"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        _download_with_limit(resp, save_path, _MAX_IMAGE_SIZE)
    return save_path


def download_file(token, file_key, message_id, file_name=None):
    """Download a file from Lark by file_key using the message resource API.

    Args:
        token: Tenant access token.
        file_key: The file key (e.g. "file_v3_xxx").
        message_id: The message ID that contains the file.
        file_name: Original filename. Used to determine save path.

    Returns:
        The path where the file was saved.
    """
    file_dir = os.path.join(handoff_tmp_dir(), "handoff-files")
    os.makedirs(file_dir, exist_ok=True)
    base = _safe_local_filename(file_name or file_key)
    # Prefix with message_id to prevent collisions when different messages
    # have attachments with the same filename.
    msg_prefix = message_id.replace("/", "_")[:20] if message_id else ""
    name = f"{msg_prefix}_{base}" if msg_prefix else base
    save_path = os.path.join(file_dir, name)

    url = f"{BASE_URL}/im/v1/messages/{message_id}/resources/{file_key}?type=file"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        _download_with_limit(resp, save_path, _MAX_FILE_SIZE)
    return save_path


def _safe_local_filename(name):
    """Return a filesystem-safe basename for local downloads.

    Prevents path traversal by stripping directory components and replacing
    path separators. Falls back to a timestamped name if input is empty.
    """
    raw = str(name or "").strip()
    if not raw:
        return f"file-{int(time.time() * 1000)}"

    normalized = raw.replace("\\", "/")
    base = os.path.basename(normalized)
    if not base or base in (".", ".."):
        base = f"file-{int(time.time() * 1000)}"

    return base.replace("/", "_").replace("\\", "_")


# ---------------------------------------------------------------------------
# Reactions and message retrieval
# ---------------------------------------------------------------------------


def add_reaction(token, message_id, emoji_type):
    """Add a reaction (emoji) to a message.

    Args:
        token: Tenant access token.
        message_id: The message ID to react to.
        emoji_type: Emoji type string, e.g. "THUMBSUP", "SMILE", "OK",
                    "THANKS", "MUSCLE", "APPLAUSE", "FISTBUMP", "DONE",
                    "LAUGH", "LOL", "LOVE", "FACEPALM", "SOB", "THINKING",
                    "JIAYI", "FINGERHEART", "BLUSH", "SMIRK", "WINK",
                    "PROUD", "WITTY", "SMART", "SCOWL", "CRY", "HAUGHTY",
                    "NOSEPICK", "ERROR".

    Returns:
        The reaction_id.
    """
    payload = {"reaction_type": {"emoji_type": emoji_type}}
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/messages/{message_id}/reactions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to add reaction: {data}")

    return data.get("data", {}).get("reaction_id", "")


def remove_reaction(token, message_id, reaction_id):
    """Remove a reaction from a message.

    Args:
        token: Tenant access token.
        message_id: The message ID the reaction is on.
        reaction_id: The reaction_id returned by add_reaction.
    """
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/messages/{message_id}/reactions/{reaction_id}",
        method="DELETE",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to remove reaction: {data}")


def get_message(token, message_id):
    """Fetch a single message by its ID. Returns the message item dict."""
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to get message: {data}")

    items = data.get("data", {}).get("items", [])
    return items[0] if items else {}


def list_merge_forward_messages(token, message_id):
    """List all child messages inside a merge_forward container.

    Uses GET /im/v1/messages/{message_id} which returns the merge_forward
    message itself plus all child messages. Children have upper_message_id
    set to the merge_forward message_id.

    Args:
        token: Tenant access token.
        message_id: The message_id of the merge_forward message.

    Returns:
        List of child message item dicts in chronological order (excludes
        the merge_forward container itself). Each has keys like msg_type,
        body.content, sender, create_time, upper_message_id, etc.
    """
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to get merge_forward messages: {data}")

    all_items = data.get("data", {}).get("items", [])
    # Filter to only child messages (those with upper_message_id)
    children = [
        item for item in all_items if item.get("upper_message_id") == message_id
    ]
    # Sort by create_time ascending
    children.sort(key=lambda x: x.get("create_time", "0"))
    return children


def extract_message_text(message_item):
    """Extract readable text from a message item returned by the Lark API.

    Handles text, post, image, file, card actions, and other message types.

    Note: wait_for_reply.py does NOT call this function — it returns raw JSON
    and Claude reads the ``text`` field directly. This function is mainly used
    by SKILL.md inline Python code for processing merge_forward child messages.

    Returns:
        A tuple of (text, msg_type).
    """
    msg_type = message_item.get("msg_type", "unknown")
    try:
        content = json.loads(message_item.get("body", {}).get("content", "{}"))
    except (json.JSONDecodeError, TypeError, AttributeError):
        content = {}

    if msg_type == "text":
        return content.get("text", ""), msg_type
    elif msg_type == "post":
        post = content
        if not isinstance(content.get("content"), list):
            locale = next(iter(content), None)
            post = content.get(locale, {}) if locale else {}
        paragraphs = post.get("content", [])
        parts = []
        for para in paragraphs:
            for elem in para:
                if elem.get("text"):
                    parts.append(elem["text"])
                elif elem.get("tag") == "img":
                    parts.append("[image]")
        title = post.get("title", "")
        text = "\n".join(parts)
        if title:
            text = f"{title}\n{text}"
        return text or "[post]", msg_type
    elif msg_type == "image":
        return "[image]", msg_type
    elif msg_type == "file":
        return f"[file: {content.get('file_name', 'unknown')}]", msg_type
    elif msg_type == "interactive":
        # Card message — the Lark API returns Card V2 content in a degraded
        # format (rendered preview image + empty text), so we can only extract
        # whatever text elements survive the conversion.
        title = content.get("title") or ""
        parts = []
        if title:
            parts.append(title)
        for row in content.get("elements", []):
            for elem in row:
                if elem.get("text"):
                    parts.append(elem["text"])
        return "\n".join(parts) or "[card]", msg_type
    elif msg_type in ("button_action", "form_action", "select_action", "input_action"):
        # Card callback actions — these come from the Cloudflare worker when
        # a user clicks a button, submits a form, or selects a dropdown option.
        # The worker stores the action value in the "text" field of the raw
        # JSON, but if someone calls extract_message_text on a Lark API message
        # item for these types, the content is just the action value as text.
        return content.get("text", "") or str(content), msg_type
    elif msg_type == "merge_forward":
        return "[merge_forward]", msg_type
    else:
        return f"[{msg_type} message]", msg_type


def reply_sticker(token, message_id, file_key):
    """Reply to a message with a sticker. Returns new message_id.

    Args:
        token: Tenant access token.
        message_id: The message ID to reply to.
        file_key: The sticker's file_key.
    """
    payload = {
        "msg_type": "sticker",
        "content": json.dumps({"file_key": file_key}),
    }
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/messages/{message_id}/reply",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to reply with sticker: {data}")

    return data["data"]["message_id"]
