#!/usr/bin/env python3
"""Handoff worker communication: HTTP polling, WebSocket, message registration.

Non-Lark-specific Cloudflare Worker communication extracted from lark_im.py.
"""

import base64
import json
import os
import socket
import ssl
import struct
import sys
import time
import urllib.parse

from handoff_config import load_api_key, _worker_auth_headers

# Errors that indicate the Cloudflare Durable Objects free tier quota is exhausted.
_DO_QUOTA_PATTERNS = [
    "exceeded allowed duration",
    "durable objects free tier",
    "exceeded its cpu time limit",
]


def is_do_quota_error(error_msg):
    """Return True if the error indicates a Durable Objects quota exhaustion."""
    if not error_msg:
        return False
    lower = error_msg.lower()
    return any(p in lower for p in _DO_QUOTA_PATTERNS)


def _is_sandbox_proxy(proxy_url):
    """Return True if proxy_url looks like a Claude Code sandbox proxy.

    The sandbox proxy listens on a high ephemeral port (>50000) on localhost,
    whereas user proxies like Surge use well-known ports (e.g. 6152).
    """
    if not proxy_url:
        return False
    try:
        parsed = urllib.parse.urlparse(proxy_url)
        host = parsed.hostname or ""
        port = parsed.port or 0
        return host in ("localhost", "127.0.0.1", "::1") and port > 50000
    except Exception:
        return False


def _worker_curl_env():
    """Return env dict for curl subprocesses that bypasses the sandbox proxy.

    Claude Code's sandbox injects ``https_proxy=http://localhost:<PORT>``
    that rejects CONNECT tunnels to Cloudflare Workers with 502.  We detect
    and strip only the sandbox proxy (high-port localhost) while preserving
    user proxies like Surge.
    """
    env = dict(os.environ)
    for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        if _is_sandbox_proxy(env.get(var, "")):
            del env[var]
    return env


def poll_worker(worker_url, chat_id, since=None, key=None):
    """Long-poll the worker for replies from a handoff group.

    Uses the /poll/ endpoint which blocks up to 25 seconds waiting for
    new replies, returning instantly when data arrives.

    key: optional DO routing key. When provided, polls this key instead of
        ``chat:{chat_id}``. Used by permission bridge to poll a nonce-keyed DO.

    Returns dict with keys: replies (list), takeover (bool), error (str|None).
    """
    import subprocess

    # 25s long-poll: stays under CF Workers' 30s wall-clock limit (5s margin)
    # while minimising reconnections to conserve free-tier quota.
    # curl --max-time 30 gives 5s for the response to arrive after the poll.
    # Python timeout=35 gives 5s for curl to clean up after --max-time fires.
    do_key = key or f"chat:{chat_id}"
    url = f"{worker_url}/poll/{do_key}?timeout=25"
    if since:
        url += f"&since={since}"
    result = subprocess.run(
        ["curl", "-s", "--noproxy", "*", "--max-time", "30",
         *_worker_auth_headers(), url],
        capture_output=True,
        text=True,
        timeout=35,
        env=_worker_curl_env(),
    )
    if result.returncode != 0:
        return {
            "replies": [],
            "takeover": False,
            "error": f"curl failed (exit {result.returncode})",
        }
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "replies": [],
            "takeover": False,
            "error": f"Worker returned non-JSON: {result.stdout[:200]}",
        }
    if data.get("error"):
        return {
            "replies": [],
            "takeover": False,
            "error": f"Worker error: {data['error']}",
        }
    return {
        "replies": data.get("replies", []),
        "takeover": data.get("takeover", False),
        "error": None,
    }


# ---------------------------------------------------------------------------
# WebSocket client (stdlib only — no external dependencies)
# ---------------------------------------------------------------------------


class _WebSocket:
    """Minimal WebSocket client for wss:// connections using only stdlib.

    Supports text frames, ping/pong, and close. Does not support
    extensions, compression, or continuation frames (not needed here —
    all messages are small JSON payloads).
    """

    def __init__(self, url, headers=None):
        parsed = urllib.parse.urlparse(url)
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.use_tls = parsed.scheme == "wss"
        self.extra_headers = headers or {}
        self._sock = None
        self._buf = b""

    @staticmethod
    def _get_http_proxy(target_host):
        """Detect HTTP(S) proxy from environment (respects https_proxy/no_proxy)."""
        # Check no_proxy — skip proxy for matching hosts
        no_proxy = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
        if no_proxy:
            for entry in no_proxy.split(","):
                entry = entry.strip().lower()
                if not entry:
                    continue
                host_lower = target_host.lower()
                # "*" matches everything
                if entry == "*":
                    return None, None
                # ".example.com" matches any subdomain; "example.com" matches exact
                if host_lower == entry or host_lower.endswith("." + entry.lstrip(".")):
                    return None, None

        proxy_url = (
            os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy")
            or os.environ.get("HTTP_PROXY")
        )
        if not proxy_url:
            return None, None
        parsed = urllib.parse.urlparse(proxy_url)
        default_port = 443 if parsed.scheme == "https" else 80
        return parsed.hostname, parsed.port or default_port

    def connect(self, timeout=10, bypass_sandbox_proxy=False):
        """Perform WebSocket upgrade handshake (with HTTP proxy tunneling).

        Args:
            bypass_sandbox_proxy: Skip sandbox proxy (high-port localhost)
                while still honoring user proxies like Surge.  The sandbox
                proxy rejects CONNECT tunnels to *.workers.dev with 502.
        """
        proxy_host, proxy_port = self._get_http_proxy(self.host)
        if bypass_sandbox_proxy and proxy_host in ("localhost", "127.0.0.1", "::1"):
            if proxy_port and proxy_port > 50000:
                proxy_host, proxy_port = None, None

        if proxy_host:
            # HTTP CONNECT tunnel through the proxy
            sock = socket.create_connection(
                (proxy_host, proxy_port),
                timeout=timeout,
            )
            connect_req = (
                f"CONNECT {self.host}:{self.port} HTTP/1.1\r\n"
                f"Host: {self.host}:{self.port}\r\n"
                f"\r\n"
            )
            sock.sendall(connect_req.encode())
            sock.settimeout(timeout)
            resp = b""
            deadline = time.time() + timeout
            MAX_PROXY_RESPONSE = 65536  # 64KB cap to prevent unbounded memory use
            while b"\r\n\r\n" not in resp:
                if time.time() > deadline:
                    sock.close()
                    raise ConnectionError(f"Proxy CONNECT timed out after {timeout}s")
                if len(resp) > MAX_PROXY_RESPONSE:
                    sock.close()
                    raise ConnectionError(
                        f"Proxy response exceeded {MAX_PROXY_RESPONSE} bytes"
                    )
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("Proxy closed during CONNECT")
                resp += chunk
            status_line = resp.split(b"\r\n")[0]
            try:
                status_code = int(status_line.split(b" ")[1])
            except (IndexError, ValueError):
                status_code = 0
            if status_code != 200:
                sock.close()
                raise ConnectionError(f"Proxy CONNECT failed: {status_line.decode()}")
        else:
            sock = socket.create_connection(
                (self.host, self.port),
                timeout=timeout,
            )

        if self.use_tls:
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=self.host)

        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {self.path} HTTP/1.1",
            f"Host: {self.host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            "User-Agent: curl/8.0",
        ]
        for k, v in self.extra_headers.items():
            lines.append(f"{k}: {v}")
        lines.append("")
        lines.append("")
        sock.sendall("\r\n".join(lines).encode())

        # Read response headers with deadline
        sock.settimeout(timeout)
        response = b""
        deadline = time.time() + timeout
        while b"\r\n\r\n" not in response:
            if time.time() > deadline:
                sock.close()
                raise ConnectionError(f"WebSocket handshake timed out after {timeout}s")
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Connection closed during handshake")
            response += chunk

        idx = response.index(b"\r\n\r\n") + 4
        self._buf = response[idx:]  # leftover data after headers

        status_line = response[:idx].split(b"\r\n")[0].decode()
        if "101" not in status_line:
            sock.close()
            raise ConnectionError(f"WebSocket upgrade failed: {status_line}")
        # Note: Sec-WebSocket-Accept verification is skipped because CF Workers
        # terminate WebSocket at the edge and proxy a new connection to the DO.
        # The Accept header won't match our key. HTTPS prevents MITM anyway.

        self._sock = sock

    def recv(self, timeout=None):
        """Receive one text message. Returns str, or None on close frame.

        Note: timeout is per-recv syscall, not per-message assembly. For the
        small JSON payloads here (<1KB), each message fits in one recv call,
        so the timeout effectively applies per-message.
        """
        if timeout is not None:
            self._sock.settimeout(timeout)
        while True:
            header = self._recv_exact(2)
            opcode = header[0] & 0x0F
            masked = (header[1] >> 7) & 1
            length = header[1] & 0x7F

            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]

            # Guard against oversized frames (expected payloads are <1KB JSON)
            if length > 1_048_576:  # 1 MB
                self.close()
                raise ConnectionError(
                    f"WebSocket frame too large: {length} bytes (max 1MB)"
                )

            mask_key = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length)

            if mask_key:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

            if opcode == 0x8:  # Close
                return None
            if opcode == 0x9:  # Ping — auto-respond with pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # Pong — ignore
                continue
            if opcode == 0x1:  # Text
                return payload.decode()
            # Binary or unknown — skip
            continue

    def send(self, text):
        """Send a text message."""
        self._send_frame(0x1, text.encode() if isinstance(text, str) else text)

    def close(self):
        """Send close frame and close the socket."""
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass
        self._sock = None

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        frame = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            frame += bytes([0x80 | length])
        elif length < 65536:
            frame += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            frame += bytes([0x80 | 127]) + struct.pack("!Q", length)
        frame += mask + masked
        self._sock.sendall(frame)

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self._sock.recv(max(n - len(self._buf), 4096))
            if not chunk:
                self.close()
                raise ConnectionError("Connection closed")
            self._buf += chunk
        result = self._buf[:n]
        self._buf = self._buf[n:]
        return result


def poll_worker_ws(worker_url, chat_id, since=None, max_duration=None, key=None):
    """Connect via WebSocket and wait for replies. Returns on first message.

    Uses a single persistent WebSocket connection instead of repeated HTTP
    long-polls. Dramatically reduces CF Workers request quota usage: 1 request
    per wait cycle instead of 1 every 25 seconds.

    Args:
        max_duration: Optional max seconds to wait before returning empty.
            When set, the WS poll returns ``{"replies": [], "error": None}``
            after this many seconds of no data, allowing callers to check
            their own deadlines. Default ``None`` = wait indefinitely.
        key: Optional DO routing key. When provided, connects to this key
            instead of ``chat:{chat_id}``. Used by permission bridge to poll
            a nonce-keyed DO.

    Returns dict with keys: replies (list), takeover (bool), error (str|None).
    """
    do_key = key or f"chat:{chat_id}"
    ws_url = worker_url.replace("https://", "wss://").replace("http://", "ws://")
    ws_url += f"/ws/{do_key}"
    if since:
        ws_url += f"?since={since}"

    api_key = load_api_key()
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    ws = _WebSocket(ws_url, headers=headers)
    ws.connect(timeout=10, bypass_sandbox_proxy=True)

    ws_start = time.time()
    try:
        while True:
            try:
                # 30s recv timeout — sends keepalive ping on timeout.
                # Many HTTP proxies (e.g. Surge) close idle CONNECT tunnels
                # after ~50s, so we ping well before that. ~120 DO reqs/hr
                # when idle, but each is tiny (pong response).
                msg = ws.recv(timeout=30)
            except socket.timeout:
                # Check max_duration before pinging — allows callers to
                # enforce their own deadlines (e.g. permission bridge timeout).
                if max_duration and (time.time() - ws_start) >= max_duration:
                    return {"replies": [], "takeover": False, "error": None}
                try:
                    ws.send(json.dumps({"ping": True}))
                except Exception:
                    return {"replies": [], "takeover": False, "error": "ping_failed"}
                continue

            if msg is None:  # Close frame
                return {"replies": [], "takeover": False, "error": "ws_closed"}

            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue

            if data.get("pong"):
                continue

            if data.get("takeover"):
                return {"replies": [], "takeover": True, "error": None}

            replies = data.get("replies", [])
            if replies:
                # Ack processed replies over WebSocket (no HTTP request needed)
                last = replies[-1].get("create_time", "")
                if last:
                    try:
                        ws.send(json.dumps({"ack": last}))
                    except Exception:
                        pass
                return {"replies": replies, "takeover": False, "error": None}
    except ConnectionError as e:
        return {"replies": [], "takeover": False, "error": str(e)}
    finally:
        ws.close()


def register_message(worker_url, message_id, chat_id):
    """Register a sent message's chat_id with the worker for reaction routing."""
    import subprocess

    url = f"{worker_url}/register-message"
    payload = json.dumps({"message_id": message_id, "chat_id": chat_id})
    try:
        result = subprocess.run(
            [
                "curl",
                "-s",
                "--noproxy", "*",
                "--max-time",
                "5",
                "-X",
                "POST",
                "-H",
                "Content-Type: application/json",
                *_worker_auth_headers(),
                "-d",
                payload,
                url,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=_worker_curl_env(),
        )
        if result.returncode != 0:
            print(
                f"[handoff] register_message failed (rc={result.returncode}): {result.stderr}",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"[handoff] register_message error: {e}", file=sys.stderr)


def send_takeover(worker_url, chat_id):
    """Signal the worker to notify any polling session of a takeover.

    Sets a flag in the Durable Object. The next poll from
    wait_for_reply.py will see ``takeover: true`` in the response, causing
    it to exit cleanly so a new session can take over.

    Args:
        worker_url: Base URL of the Cloudflare Worker.
        chat_id: Chat ID of the handoff group being taken over.
    """
    import subprocess

    url = f"{worker_url}/takeover/chat:{chat_id}"
    try:
        result = subprocess.run(
            [
                "curl",
                "-s",
                "--noproxy", "*",
                "--max-time",
                "5",
                "-X",
                "POST",
                *_worker_auth_headers(),
                url,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=_worker_curl_env(),
        )
        if result.returncode != 0:
            print(
                f"[handoff] send_takeover failed (rc={result.returncode}): {result.stderr}",
                file=sys.stderr,
            )
    except Exception as e:
        # Best-effort — the old session may already be dead
        print(f"[handoff] send_takeover error: {e}", file=sys.stderr)


def ack_worker_replies(worker_url, chat_id, before, key=None):
    """Acknowledge processed replies, removing them from the Durable Object.

    Removes all replies with create_time <= before. This prevents unbounded
    growth of stored replies during long handoff periods.

    Args:
        worker_url: Base URL of the Cloudflare Worker.
        chat_id: Chat ID whose replies to acknowledge.
        before: Timestamp string (ms) — remove replies at or before this time.
        key: Optional DO routing key. When provided, acks this key instead of
            ``chat:{chat_id}``. Used by permission bridge for nonce-keyed DOs.
    """
    import subprocess

    do_key = key or f"chat:{chat_id}"
    url = f"{worker_url}/replies/{do_key}/ack?before={before}"
    try:
        result = subprocess.run(
            [
                "curl",
                "-s",
                "--noproxy", "*",
                "--max-time",
                "5",
                "-X",
                "POST",
                *_worker_auth_headers(),
                url,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=_worker_curl_env(),
        )
        if result.returncode != 0:
            print(
                f"[handoff] ack_worker_replies failed (rc={result.returncode}): {result.stderr}",
                file=sys.stderr,
            )
    except Exception as e:
        # Non-critical — stale replies just get cleaned up eventually
        print(f"[handoff] ack_worker_replies error: {e}", file=sys.stderr)


def check_do_quota_status(worker_url, chat_id):
    """Check if the DO quota is exhausted for this chat (via KV, no DO access).

    Returns the exhausted_at timestamp string if exhausted, or None.
    """
    import subprocess

    do_key = f"chat:{chat_id}"
    url = f"{worker_url}/status/{do_key}"
    try:
        result = subprocess.run(
            ["curl", "-s", "--noproxy", "*", "--max-time", "5",
             *_worker_auth_headers(), url],
            capture_output=True,
            text=True,
            timeout=10,
            env=_worker_curl_env(),
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        if data.get("do_quota_exhausted"):
            return data.get("exhausted_at")
    except Exception:
        pass
    return None
