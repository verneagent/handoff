#!/usr/bin/env python3
"""Group config stored in a Lark pinned card, with local SQLite cache.

Source of truth is a pinned interactive card in the Lark group with title
"__handoff_config__". The card body contains JSON config:

    {
        "guests": [{"open_id": "ou_xxx", "name": "Alice", "role": "coowner"}],
        "autoapprove": true,
        "filter": "concise",
        "rules": "Reply in Chinese. ..."
    }

Local SQLite cache (group_config_cache table) avoids hitting the API on
every process invocation. Cache is refreshed when stale or on write.

When PATCH fails (14-day card expiry), the card is deleted and re-created.
"""

import json
import sys
import time

import handoff_db
import lark_im

CONFIG_CARD_TITLE = "__handoff_config__"
CACHE_TTL_SECONDS = 300  # 5 minutes

DEFAULT_CONFIG = {
    "guests": [],
    "autoapprove": False,
    "filter": "concise",
    "rules": "",
}


def _warn(msg):
    print(f"[group_config] {msg}", file=sys.stderr)


def _build_config_card(config):
    """Build an interactive card that stores config JSON in the body."""
    body_json = json.dumps(config, ensure_ascii=False, indent=2)
    return {
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": CONFIG_CARD_TITLE},
            "template": "grey",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"```json\n{body_json}\n```",
                },
            }
        ],
    }


def _parse_config_from_card(message_item):
    """Extract config dict from a card message item returned by get_message."""
    try:
        body = message_item.get("body", {})
        content_str = body.get("content", "")
        if not content_str:
            return None
        content = json.loads(content_str)
        # Navigate the card structure to find the JSON in markdown
        elements = content.get("elements", [])
        for el in elements:
            text_obj = el.get("text", {})
            md = text_obj.get("content", "")
            # Strip ```json ... ``` fences
            if md.startswith("```json\n") and md.endswith("\n```"):
                raw = md[len("```json\n"):-len("\n```")]
                return json.loads(raw)
            if md.startswith("```\n") and md.endswith("\n```"):
                raw = md[len("```\n"):-len("\n```")]
                return json.loads(raw)
            # Try parsing the raw content as JSON
            try:
                return json.loads(md)
            except (json.JSONDecodeError, TypeError):
                continue
    except Exception as e:
        _warn(f"failed to parse config card: {e}")
    return None


def _find_config_pin(token, chat_id):
    """Find the pinned config card in a chat.

    Returns (message_id, config_dict) or (None, None).
    """
    try:
        pins = lark_im.list_pins(token, chat_id)
    except Exception as e:
        _warn(f"failed to list pins for {chat_id}: {e}")
        return None, None

    for pin in pins:
        msg_id = pin.get("message_id", "")
        if not msg_id:
            continue
        try:
            msg = lark_im.get_message(token, msg_id)
        except Exception as e:
            _warn(f"failed to get message {msg_id}: {e}")
            continue
        # Check if this is our config card
        msg_type = msg.get("msg_type", "")
        if msg_type != "interactive":
            continue
        config = _parse_config_from_card(msg)
        if config is not None:
            # Verify it's our config card by checking the parsed content
            # is a dict with expected keys (not some random card)
            if isinstance(config, dict) and any(
                k in config for k in ("guests", "autoapprove", "filter", "rules")
            ):
                return msg_id, config
    return None, None


def _create_config_card(token, chat_id, config):
    """Send a new config card and pin it. Returns message_id."""
    card = _build_config_card(config)
    msg_id = lark_im.send_message(token, chat_id, card)
    lark_im.create_pin(token, msg_id)
    return msg_id


def _update_config_card(token, chat_id, pin_message_id, config):
    """Update existing config card. On PATCH failure, delete and re-create.

    Returns the (possibly new) message_id.
    """
    card = _build_config_card(config)
    try:
        lark_im.update_card_message(token, pin_message_id, card)
        return pin_message_id
    except RuntimeError as e:
        _warn(f"PATCH failed ({e}), re-creating config card")
        # Delete old pin and message, then create new
        try:
            lark_im.delete_pin(token, pin_message_id)
        except Exception:
            pass
        try:
            lark_im.delete_message(token, pin_message_id)
        except Exception:
            pass
        return _create_config_card(token, chat_id, config)


def load_config(token, chat_id, *, force=False):
    """Load group config. Uses cache if fresh, otherwise fetches from Lark.

    Returns config dict with keys: guests, autoapprove, filter, rules.
    """
    if not force:
        cached = handoff_db.get_cached_group_config(chat_id)
        if cached:
            config, pin_msg_id, last_synced = cached
            if time.time() - last_synced < CACHE_TTL_SECONDS:
                return config

    # Fetch from Lark
    msg_id, config = _find_config_pin(token, chat_id)
    if config is None:
        config = dict(DEFAULT_CONFIG)
        msg_id = ""

    # Update cache
    handoff_db.set_cached_group_config(chat_id, config, msg_id or "")
    return config


def save_config(token, chat_id, config):
    """Save group config to Lark (create or update pinned card) and update cache.

    Returns the pin message_id.
    """
    # Check cache for existing pin message_id
    cached = handoff_db.get_cached_group_config(chat_id)
    pin_msg_id = cached[1] if cached else ""

    if not pin_msg_id:
        # Check Lark for existing card we don't have cached
        pin_msg_id, _ = _find_config_pin(token, chat_id)

    if pin_msg_id:
        msg_id = _update_config_card(token, chat_id, pin_msg_id, config)
    else:
        msg_id = _create_config_card(token, chat_id, config)

    handoff_db.set_cached_group_config(chat_id, config, msg_id)
    return msg_id


# ---------------------------------------------------------------------------
# Convenience helpers for individual config fields
# ---------------------------------------------------------------------------


def get_guests(token, chat_id):
    """Get guest list from group config."""
    config = load_config(token, chat_id)
    return config.get("guests", [])


def set_guests(token, chat_id, guests):
    """Replace guest list in group config."""
    config = load_config(token, chat_id)
    config["guests"] = guests
    save_config(token, chat_id, config)


def add_guests(token, chat_id, new_guests):
    """Add guests (skip duplicates). Returns (added, current)."""
    config = load_config(token, chat_id)
    current = config.get("guests", [])
    existing_ids = {g["open_id"] for g in current}
    added = []
    for g in new_guests:
        if g["open_id"] not in existing_ids:
            current.append(g)
            existing_ids.add(g["open_id"])
            added.append(g)
    if added:
        config["guests"] = current
        save_config(token, chat_id, config)
    return added, current


def remove_guests(token, chat_id, open_ids):
    """Remove guests by open_id. Returns (removed, remaining)."""
    config = load_config(token, chat_id)
    current = config.get("guests", [])
    ids_to_remove = set(open_ids)
    removed = [g for g in current if g["open_id"] in ids_to_remove]
    remaining = [g for g in current if g["open_id"] not in ids_to_remove]
    if removed:
        config["guests"] = remaining
        save_config(token, chat_id, config)
    return removed, remaining


def get_member_roles(token, chat_id):
    """Get {open_id: role} mapping from group config."""
    guests = get_guests(token, chat_id)
    return {g["open_id"]: g.get("role", "guest") for g in guests}


def get_autoapprove(token, chat_id):
    """Check if autoapprove is enabled."""
    config = load_config(token, chat_id)
    return bool(config.get("autoapprove", False))


def set_autoapprove(token, chat_id, enabled):
    """Set autoapprove flag."""
    config = load_config(token, chat_id)
    config["autoapprove"] = bool(enabled)
    save_config(token, chat_id, config)


def get_filter(token, chat_id):
    """Get message filter level."""
    config = load_config(token, chat_id)
    return config.get("filter", "concise")


def set_filter(token, chat_id, level):
    """Set message filter level."""
    config = load_config(token, chat_id)
    config["filter"] = level
    save_config(token, chat_id, config)


def get_rules(token, chat_id):
    """Get group rules text."""
    config = load_config(token, chat_id)
    return config.get("rules", "")


def set_rules(token, chat_id, rules):
    """Set group rules text."""
    config = load_config(token, chat_id)
    config["rules"] = rules
    save_config(token, chat_id, config)
