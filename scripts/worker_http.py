#!/usr/bin/env python3
"""Shared urllib-based worker HTTP helpers.

Used by OpenCode permission bridge (BunShell-safe) and other scripts that need
direct worker polling/ack behavior without shelling out to curl.
"""

import json
import urllib.error
import urllib.request


def _poll_error_result(error):
    return {"replies": [], "takeover": False, "error": error}


def build_worker_headers(api_key=""):
    headers = {
        "User-Agent": "curl/8.0",
        "Accept": "*/*",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def poll_worker_urllib(worker_url, chat_id, since="0", timeout=25, api_key="", key=None):
    """Poll worker replies using urllib.

    key: optional DO routing key. When provided, polls this key instead of
        ``chat:{chat_id}``. Used by permission bridge for nonce-keyed DOs.

    Returns dict: {replies: [], takeover: bool, error: str|None}
    """
    do_key = key or f"chat:{chat_id}"
    url = f"{worker_url}/poll/{do_key}?timeout={int(timeout)}"
    if since:
        url += f"&since={since}"

    req = urllib.request.Request(url, headers=build_worker_headers(api_key))

    try:
        with urllib.request.urlopen(req, timeout=int(timeout) + 5) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return _poll_error_result(f"HTTP {e.code}")
    except Exception as e:
        return _poll_error_result(str(e))

    if data.get("error"):
        return _poll_error_result(data["error"])

    return {
        "replies": data.get("replies", []),
        "takeover": data.get("takeover", False),
        "error": None,
    }


def ack_worker_urllib(
    worker_url,
    chat_id,
    before,
    api_key="",
    timeout=10,
    log_fn=None,
    key=None,
):
    """Ack worker replies using urllib.

    key: optional DO routing key. When provided, acks this key instead of
        ``chat:{chat_id}``. Used by permission bridge for nonce-keyed DOs.

    Returns True on success, False on error.
    """
    do_key = key or f"chat:{chat_id}"
    url = f"{worker_url}/replies/{do_key}/ack?before={before}"
    req = urllib.request.Request(
        url,
        method="POST",
        data=b"",
        headers=build_worker_headers(api_key),
    )
    try:
        with urllib.request.urlopen(req, timeout=int(timeout)) as resp:
            resp.read()
        return True
    except Exception as e:
        if log_fn:
            log_fn(f"ack failed: {e}")
        return False
