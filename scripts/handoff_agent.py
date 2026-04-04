#!/usr/bin/env python3
"""Handoff agent: background agent process using Claude Agent SDK.

Architecture: the agent process manages the message wait loop and passes
each message to ClaudeSDKClient. The SDK client sends responses to Lark
via send_to_group.py (same as CLI handoff). The agent process provides
a fallback send if the SDK client doesn't.

Usage:
    python3 scripts/handoff_agent.py --chat-id <CHAT_ID> --project-dir <DIR> [--model <MODEL>]

Requirements:
    pip install claude-agent-sdk
"""

import os
import sys

# Ensure we're running on a Python with claude_agent_sdk.
# On some machines (e.g. macOS with Xcode), `python3` resolves to a system
# Python that lacks the SDK. Try to find a better interpreter and re-exec.
def _ensure_sdk():
    try:
        import claude_agent_sdk  # noqa: F401
        return  # All good
    except ImportError:
        pass
    # Search PATH for any python3.X that has the SDK (highest version first)
    import glob
    import subprocess
    candidates = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        candidates.extend(sorted(glob.glob(os.path.join(d, "python3.[0-9]*")), reverse=True))
    # Also check well-known locations
    for fallback in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3"]:
        if fallback not in candidates:
            candidates.append(fallback)
    for candidate in candidates:
        if not candidate or candidate == sys.executable:
            continue
        if not os.path.isfile(candidate):
            continue
        try:
            rc = subprocess.call(
                [candidate, "-c", "import claude_agent_sdk"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if rc == 0:
                os.execv(candidate, [candidate] + sys.argv)
        except Exception:
            continue
    print("Error: claude_agent_sdk not found in any Python interpreter.", file=sys.stderr)
    sys.exit(1)

_ensure_sdk()

import argparse
import asyncio
import json
import re
import signal
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import group_config
import handoff_config
import handoff_db
import handoff_lifecycle
import handoff_worker
import lark_im
import send_to_group as send_mod
import wait_for_reply as wfr_mod


def _log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[agent] [{ts}] {msg}", flush=True)


def _diagnose_network():
    """Log SSL and network diagnostics for debugging launchd issues."""
    import ssl
    ctx = ssl.create_default_context()
    _log(f"SSL cert store: {ctx.cert_store_stats()}")
    try:
        import certifi
        _log(f"certifi CA: {certifi.where()}")
    except ImportError:
        _log("certifi not installed")
    import socket
    try:
        worker_url = handoff_config.load_worker_url(profile="default")
        if worker_url:
            import urllib.parse
            host = urllib.parse.urlparse(worker_url).hostname
            addr = socket.getaddrinfo(host, 443, socket.AF_INET)
            _log(f"DNS resolve {host}: {addr[0][4] if addr else 'FAILED'}")
    except Exception as e:
        _log(f"DNS resolve failed: {e}")


def wait_for_reply_inline(chat_id, session, profile, timeout=300):
    """Wait for a Lark message using inline WebSocket poll."""
    worker_url = handoff_config.load_worker_url(profile=profile)
    if not worker_url:
        return None

    session_id = session.get("session_id", "")
    fresh = handoff_db.get_session(session_id) if session_id else None
    if fresh:
        session.update(fresh)
    since = session.get("last_checked") or str(int(time.time() * 1000) - 5000)

    operator_open_id = session.get("operator_open_id", "")
    bot_open_id = session.get("bot_open_id", "")
    need_mention = session.get("need_mention", False)
    guests = session.get("guests") or []
    member_roles = {g["open_id"]: g.get("role", "guest") for g in guests} if guests else {}

    deadline = None if timeout <= 0 else time.time() + timeout

    def _finish(replies):
        wfr_mod._ack_with_reaction(replies)
        for r in replies:
            try:
                handoff_db.record_received_message(
                    chat_id=chat_id, text=r.get("text", ""), title="",
                    source_message_id=r.get("message_id", ""),
                    message_time=r.get("create_time"))
            except Exception:
                pass
        last_checked = replies[-1]["create_time"]
        if session_id:
            try:
                handoff_db.set_session_last_checked(session_id, last_checked)
            except Exception:
                pass
        return {"replies": replies, "count": len(replies)}

    while deadline is None or time.time() < deadline:
        try:
            result = handoff_worker.poll_worker_ws(worker_url, chat_id, since, profile=profile)
            if result.get("takeover"):
                # Verify our session is still active — if we sent the takeover
                # ourselves (to kick a stale session), ignore it.
                sid = session.get("session_id", "")
                if sid and handoff_db.get_session(sid):
                    _log("Ignoring takeover (own session still active)")
                    continue
                return {"takeover": True}
            replies = result.get("replies", [])
            if replies:
                replies = wfr_mod.filter_self_bot(replies, bot_open_id)
            if replies:
                replies = (wfr_mod.filter_by_allowed_senders(replies, operator_open_id, member_roles)
                           if member_roles else wfr_mod.filter_by_operator(replies, operator_open_id))
            if need_mention and replies:
                replies = wfr_mod.filter_bot_interactions(replies, bot_open_id)
            if replies:
                return _finish(replies)
            if result.get("error"):
                raise Exception(result["error"])
            continue
        except Exception as e:
            _log(f"WebSocket error: {e} — falling back to HTTP")
        try:
            result = handoff_worker.poll_worker(worker_url, chat_id, since, profile=profile)
            if result.get("takeover"):
                sid = session.get("session_id", "")
                if sid and handoff_db.get_session(sid):
                    _log("Ignoring takeover (own session still active)")
                    continue
                return {"takeover": True}
            if result.get("error"):
                time.sleep(3)
                continue
            replies = result.get("replies", [])
            if replies:
                handoff_worker.ack_worker_replies(worker_url, chat_id, replies[-1]["create_time"], profile=profile)
                replies = wfr_mod.filter_self_bot(replies, bot_open_id)
                replies = (wfr_mod.filter_by_allowed_senders(replies, operator_open_id, member_roles)
                           if member_roles else wfr_mod.filter_by_operator(replies, operator_open_id))
                if need_mention:
                    replies = wfr_mod.filter_bot_interactions(replies, bot_open_id)
                if replies:
                    return _finish(replies)
        except Exception as e:
            _log(f"HTTP poll error: {e}")
            time.sleep(3)

    return {"timeout": True, "replies": [], "count": 0}


def send_response_inline(token, chat_id, text):
    """Send a markdown card response to Lark (fallback for when agent doesn't send)."""
    try:
        wfr_mod.clear_ack_reaction()
        send_mod.send(token, chat_id, title="", message=text, is_card=False, color="blue")
    except Exception as e:
        _log(f"send error: {e}")


def _build_agent_append_prompt():
    """Load SKILL-agent.md — the concise Lark I/O guide for the SDK client."""
    skill_agent_path = os.path.join(SCRIPT_DIR, "..", "SKILL-agent.md")
    try:
        with open(skill_agent_path) as f:
            return "\n\n" + f.read()
    except FileNotFoundError:
        _log(f"Warning: SKILL-agent.md not found at {skill_agent_path}")
        return ""


def _build_permission_handler(credentials, chat_id, session_id_ref):
    """Build an async can_use_tool callback for Lark-based permission bridging.

    session_id_ref: mutable list [session_id] so the callback sees the latest.
    """
    from permission_core import (
        build_permission_body,
        prepare_permission_request,
        run_permission_poll_loop,
        update_permission_card,
    )

    def _is_handoff_cmd(tool_name, tool_input):
        if tool_name != "Bash":
            return False
        cmd = tool_input.get("command", "")
        internal = ["handoff_ops.py", "send_to_group.py", "wait_for_reply.py",
                     "send_and_wait.py", "start_and_wait.py", "end_and_cleanup.py",
                     "iterm2_silence.py", "enter_handoff.py", "preflight.py",
                     "team_status.py"]
        if "$SKILL_SCRIPTS/" in cmd or "/handoff/scripts/" in cmd:
            return any(s in cmd for s in internal)
        return False

    def _poll_permission_sync(tool_name, tool_input, sid):
        """Synchronous permission poll — runs in executor to avoid blocking event loop."""
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        session = handoff_db.get_session(sid) if sid else None

        # Autoapprove mode
        if session and session.get("autoapprove"):
            _log(f"[perm] autoapprove: {tool_name}")
            return PermissionResultAllow()

        try:
            token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
            import handoff_config
            perm_profile = session.get("config_profile", "default") if session else "default"
            worker_url = handoff_config.load_worker_url(perm_profile)

            operator_open_id = session.get("operator_open_id", "") if session else ""
            approver_ids = {operator_open_id} if operator_open_id else set()
            if session:
                for g in session.get("guests") or []:
                    if g.get("role") == "coowner":
                        approver_ids.add(g["open_id"])

            description = _format_tool_for_permission(tool_name, tool_input)
            perm_body = build_permission_body(tool_name, description)

            nonce, perm_msg_id = prepare_permission_request(
                lark_im, token, chat_id, tool_name, description,
                ack_fn=lambda key, before: handoff_worker.ack_worker_replies(
                    worker_url, chat_id, before, key=key, profile=perm_profile),
                log_fn=lambda m: _log(f"[perm] {m}"),
                approver_ids=approver_ids,
            )

            decision, _ = run_permission_poll_loop(
                poll_fn=lambda chat_id, since: handoff_worker.poll_worker(
                    worker_url, chat_id, since, key=nonce, profile=perm_profile),
                ack_fn=lambda chat_id, before: handoff_worker.ack_worker_replies(
                    worker_url, chat_id, before, key=nonce, profile=perm_profile),
                record_received_fn=handoff_db.record_received_message,
                set_last_checked_fn=handoff_db.set_session_last_checked,
                on_deny_fn=lambda: update_permission_card(
                    lark_im, token, perm_msg_id, "deny", tool_name, perm_body),
                chat_id=chat_id,
                session_id=sid,
                since="0",
                timeout_seconds=0,
                log_fn=lambda m: _log(f"[perm] {m}"),
                approver_ids=approver_ids,
            )

            if decision in ("allow", "always"):
                update_permission_card(lark_im, token, perm_msg_id, decision, tool_name, perm_body)
                return PermissionResultAllow()
            else:
                return PermissionResultDeny(message=f"Permission to use {tool_name} was denied via Lark.")

        except Exception as e:
            _log(f"[perm] error: {e}")
            return PermissionResultDeny(message=f"Permission check failed: {e}")

    async def can_use_tool(tool_name, tool_input, _context):
        from claude_agent_sdk import PermissionResultAllow

        # Auto-approve handoff internal commands (fast, no I/O)
        if _is_handoff_cmd(tool_name, tool_input):
            return PermissionResultAllow()

        # Run synchronous poll in executor to avoid blocking the event loop
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _poll_permission_sync, tool_name, tool_input, session_id_ref[0]
        )

    return can_use_tool


def _format_tool_for_permission(tool_name, tool_input):
    """Format a tool call description for permission card display."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        desc = tool_input.get("description", "")
        parts = []
        if desc:
            parts.append(desc)
        if cmd:
            display = cmd if len(cmd) <= 200 else cmd[:200] + "..."
            parts.append(f"`{display}`")
        return "\n".join(parts) if parts else "Run a command"
    if tool_name in ("Write", "Edit"):
        path = tool_input.get("file_path", "")
        return f"File: `{path}`" if path else f"{tool_name} a file"
    return f"Use {tool_name}"


def _build_agent_options(project_dir, model, credentials=None, chat_id=None,
                         session_id_ref=None):
    """Build ClaudeAgentOptions.

    credentials, chat_id, session_id_ref: passed to build the can_use_tool
    permission handler. If not provided, permission falls through to default.
    """
    from claude_agent_sdk import ClaudeAgentOptions

    skill_dir = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
    handoff_env = {
        "PATH": f"/opt/homebrew/bin:/opt/homebrew/sbin:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": os.environ.get("HOME", ""),
        "USER": os.environ.get("USER", ""),
        "HANDOFF_SKILL_DIR": skill_dir,
    }
    for key in ("HANDOFF_SESSION_ID", "HANDOFF_PROJECT_DIR", "HANDOFF_SESSION_TOOL",
                "HANDOFF_TMP_DIR", "http_proxy", "https_proxy", "all_proxy",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        val = os.environ.get(key)
        if val:
            handoff_env[key] = val

    def _stderr_handler(line):
        _log(f"[sdk-stderr] {line.rstrip()}")

    # Build permission handler if we have credentials
    perm_handler = None
    if credentials and chat_id and session_id_ref:
        perm_handler = _build_permission_handler(credentials, chat_id, session_id_ref)

    return ClaudeAgentOptions(
        allowed_tools=["Skill", "Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        setting_sources=["user", "project"],
        permission_mode="default",
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": _build_agent_append_prompt(),
        },
        cwd=project_dir,
        model=model,
        env=handoff_env,
        stderr=_stderr_handler,
        can_use_tool=perm_handler,
        hooks={},  # Disable all CLI hooks — agent handles everything
    )




async def run_agent_turn(client, prompt, send_fn=None, working_fn=None):
    """Send a prompt to the persistent SDK client. Returns cost (float).

    Each non-empty AssistantMessage is sent immediately via send_fn.
    Waits for all background tasks to complete after ResultMessage, with a
    timeout to avoid blocking indefinitely.

    send_fn: callable(text, task_id=None, pending_tasks=0) — sends text to
    Lark with optional task context. If None, messages are collected and the
    last one is returned as (text, cost) for backward compat.
    working_fn: callable(action, description="") — manages Working card.
      action: "start" (create/show), "progress" (update description),
              "done" (mark completed).
    """
    from claude_agent_sdk import (
        AssistantMessage, TextBlock, ResultMessage,
        TaskStartedMessage, TaskNotificationMessage,
        TaskProgressMessage,
    )

    _log(f"[turn] query() sending prompt ({len(prompt)} chars)")
    await client.query(prompt)
    _log("[turn] query() sent, starting receive_messages()")
    cost = 0.0
    msg_count = 0
    has_assistant_msg = False
    pending_tasks = set()
    last_text = None  # fallback if no send_fn
    working_started = False

    async for message in client.receive_messages():
        msg_count += 1
        msg_type = type(message).__name__

        if isinstance(message, AssistantMessage):
            has_assistant_msg = True
            text_parts = []
            for block in message.content:
                if isinstance(block, TextBlock) and block.text:
                    text_parts.append(block.text)
            text = "\n".join(text_parts)
            text_len = len(text)
            _log(f"[turn] msg#{msg_count} AssistantMessage text_len={text_len} blocks={len(message.content)}")
            # Tool use (no text, has blocks) → start Working card
            if text_len == 0 and len(message.content) > 0 and not working_started and working_fn:
                working_fn("start")
                working_started = True
            # Send non-empty text immediately
            if text_len > 0:
                # Determine which task this message belongs to (if any)
                current_task_id = None
                parent = getattr(message, "parent_tool_use_id", None)
                if parent:
                    # Message from a subagent/task
                    for tid in pending_tasks:
                        current_task_id = tid  # best guess: latest pending
                        break
                if send_fn:
                    send_fn(text, task_id=current_task_id,
                            pending_tasks=len(pending_tasks))
                    _log(f"[turn] sent {text_len} chars to Lark task_id={current_task_id}")
                last_text = text

        elif isinstance(message, TaskStartedMessage):
            pending_tasks.add(message.task_id)
            _log(f"[turn] msg#{msg_count} TaskStartedMessage task_id={message.task_id} pending={len(pending_tasks)}")

        elif isinstance(message, TaskProgressMessage):
            desc = getattr(message, "description", "") or ""
            _log(f"[turn] msg#{msg_count} TaskProgressMessage task_id={message.task_id} desc={desc[:60]}")
            if working_fn and desc:
                working_fn("progress", desc)

        elif isinstance(message, TaskNotificationMessage):
            pending_tasks.discard(message.task_id)
            _log(f"[turn] msg#{msg_count} TaskNotificationMessage task_id={message.task_id} status={message.status} pending={len(pending_tasks)}")

        elif isinstance(message, ResultMessage):
            cost = getattr(message, "total_cost_usd", 0) or 0.0
            is_error = getattr(message, "is_error", False)
            session_id = getattr(message, "session_id", "")
            stop_reason = getattr(message, "stop_reason", "")
            result_len = len(getattr(message, "result", "") or "")
            _log(f"[turn] msg#{msg_count} ResultMessage cost=${cost:.4f} error={is_error} stop={stop_reason} session={session_id[:12]} has_assistant={has_assistant_msg} result_len={result_len} pending={len(pending_tasks)}")

            if pending_tasks:
                # Model decided the turn is done but background tasks are
                # still running. Cancel them to prevent their notifications
                # from leaking into the next turn as "后台任务已完成" chatter.
                for tid in list(pending_tasks):
                    try:
                        await client.stop_task(tid)
                        _log(f"[turn] cancelled pending task {tid}")
                    except Exception as e:
                        _log(f"[turn] cancel task {tid} failed: {e}")
                pending_tasks.clear()
            break

        else:
            _log(f"[turn] msg#{msg_count} {msg_type}: {str(message)[:200]}")

    _log(f"[turn] receive loop done. msgs={msg_count} has_assistant={has_assistant_msg} pending={len(pending_tasks)}")
    if working_fn and working_started:
        working_fn("done")
    if send_fn:
        return cost
    return (last_text, cost)


def _is_esc_command(text, mentions=None):
    """Check if a message text is an /esc command (after stripping mentions)."""
    t = text.strip().lower()
    for m in (mentions or []):
        key = m.get("key", "")
        if key:
            t = t.replace(key.lower(), "").strip()
    t = re.sub(r"\s+", " ", t).strip()
    return t in ("/esc", "esc", "cancel", "取消")


def _is_authorized_sender(reply, operator_open_id, member_roles):
    """Check if a reply is from an authorized sender (operator or coowner).

    If no operator info is available, allows all senders (graceful fallback).
    """
    if not operator_open_id:
        return True  # No session info — allow all
    sid = reply.get("sender_id", "")
    if not sid:
        return False
    if sid == operator_open_id:
        return True
    if member_roles and sid in member_roles:
        role = member_roles[sid]
        return role == "coowner"
    return False


def _is_handback_command(text, mentions=None):
    """Check if a message text is a handback command (after stripping mentions)."""
    t = text.strip().lower()
    for m in (mentions or []):
        key = m.get("key", "")
        if key:
            t = t.replace(key.lower(), "").strip()
    t = re.sub(r"\s+", " ", t).strip()
    return t in ("handback", "hand back", "handback dissolve", "hand back dissolve")


def _message_monitor_sync(worker_url, chat_id, since, profile, stop_event,
                          session=None):
    """Pure signal detector — watches for interrupt signals during SDK turns.

    Unlike a message relay, this monitor does NOT ack or buffer regular
    messages.  They stay in the worker DO and are picked up by
    wait_for_reply_inline after the turn ends.  This eliminates the
    duplicate-message and pending_messages complexity.

    Detected signals:
    - /esc, cancel     → "esc"
    - stop_signal      → "esc"  (card Stop button)
    - handback         → "handback"
    - takeover         → "takeover"

    Returns the signal string, or None if stop_event was set or WS
    disconnected without detecting a signal.
    """
    import socket as _socket

    # Extract session info for sender/mention filtering
    operator_open_id = (session or {}).get("operator_open_id", "")
    bot_open_id = (session or {}).get("bot_open_id", "")
    need_mention = (session or {}).get("need_mention", False)
    guests = (session or {}).get("guests") or []
    member_roles = {g["open_id"]: g.get("role", "guest") for g in guests} if guests else {}

    do_key = f"chat:{chat_id}"
    ws_url = worker_url.replace("https://", "wss://").replace("http://", "ws://")
    ws_url += f"/ws/{do_key}"
    if since:
        ws_url += f"?since={since}"

    api_key = handoff_config.load_api_key(profile)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    ws = handoff_worker._WebSocket(ws_url, headers=headers)
    try:
        ws.connect(timeout=10)
    except Exception as e:
        _log(f"Monitor WS connect failed: {e}")
        return None

    last_ping = time.time()
    try:
        while not stop_event.is_set():
            try:
                msg = ws.recv(timeout=5)
            except _socket.timeout:
                if time.time() - last_ping > 25:
                    try:
                        ws.send(json.dumps({"ping": True}))
                        last_ping = time.time()
                    except Exception:
                        break
                continue

            if msg is None:
                break

            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue

            if data.get("pong"):
                continue

            if data.get("takeover"):
                return "takeover"

            replies = data.get("replies", [])
            for r in replies:
                # Stop signal from card button (already authorized by worker)
                if r.get("msg_type") == "stop_signal":
                    _log("Stop signal received (card button)")
                    return "esc"

                # /esc command — check sender authorization + need_mention
                if _is_esc_command(r.get("text", ""), r.get("mentions")):
                    if not _is_authorized_sender(r, operator_open_id, member_roles):
                        continue
                    if need_mention:
                        mentions = r.get("mentions") or []
                        is_mentioned = bot_open_id and any(
                            m.get("id") == bot_open_id for m in mentions
                        )
                        parent_id = r.get("parent_id", "")
                        is_reply_to_bot = bool(parent_id) and handoff_db.is_bot_sent_message(parent_id)
                        if not is_mentioned and not is_reply_to_bot:
                            continue
                    return "esc"

                # handback command
                if _is_handback_command(r.get("text", ""), r.get("mentions")):
                    if _is_authorized_sender(r, operator_open_id, member_roles):
                        return "handback"

                # autoapprove is handled by the main loop command detection
                # after the turn ends. Not in the monitor — would cause
                # duplicate cards since the message stays in worker DO.

            # Do NOT ack regular messages — leave them in the DO for
            # wait_for_reply_inline to pick up after the turn.
    except Exception as e:
        _log(f"Monitor error: {e}")
    finally:
        ws.close()

    return None


async def main_loop(chat_id, project_dir, model, profile=None):
    """Main agent loop."""
    import uuid
    session_id = str(uuid.uuid4())
    os.environ["HANDOFF_SESSION_ID"] = session_id
    os.environ["HANDOFF_PROJECT_DIR"] = project_dir
    os.environ["HANDOFF_SESSION_TOOL"] = "Claude Agent SDK"

    resolved_profile = handoff_config.resolve_profile(explicit=profile)
    _diagnose_network()

    credentials = handoff_config.load_credentials(profile=resolved_profile)
    if not credentials:
        print("Error: No credentials configured.", file=sys.stderr)
        return 1

    token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
    _log("Token acquired successfully")

    operator_open_id = ""
    bot_open_id = ""
    try:
        email = credentials.get("email", "")
        if email:
            operator_open_id = lark_im.lookup_open_id_by_email(token, email) or ""
            _log(f"Operator: {email} -> {operator_open_id[:16]}...")
        bot_info = lark_im.get_bot_info(token)
        bot_open_id = bot_info.get("open_id", "")
        if bot_open_id:
            _log(f"Bot: {bot_open_id[:16]}...")
    except Exception as e:
        _log(f"Operator/bot lookup failed: {e}")

    # Clean up any session holding this chat_id before activating.
    # Send takeover HERE (same process) so we can reliably drain it.
    worker_url = handoff_config.load_worker_url(profile=resolved_profile)
    had_stale = False
    try:
        owner = handoff_db.get_chat_owner_session(chat_id)
        if owner:
            _log(f"Cleaning stale session {owner} for chat {chat_id}")
            # Reset any leftover Working card from the old session
            try:
                handoff_lifecycle.reset_working_card(owner)
            except Exception:
                pass
            handoff_db.deactivate_handoff(owner)
            had_stale = True
    except Exception as e:
        _log(f"Stale session cleanup error: {e}")

    # Send takeover + drain in same process to avoid race conditions.
    # The old agent's WS will receive takeover and exit; we wait and
    # then drain any residual flag before opening our own WS.
    if had_stale and worker_url:
        try:
            handoff_worker.send_takeover(worker_url, chat_id, profile=resolved_profile)
            _log("Sent takeover signal")
            time.sleep(2)  # Wait for CF Workers to propagate
        except Exception as e:
            _log(f"send_takeover error: {e}")

    # Auto-detect need_mention before activating
    need_mention = handoff_lifecycle.compute_need_mention(token, chat_id, bot_open_id)

    handoff_lifecycle.activate(
        session_id, chat_id, model,
        operator_open_id=operator_open_id,
        bot_open_id=bot_open_id,
        need_mention=need_mention,
        config_profile=resolved_profile,
    )
    _log(f"Activated session {session_id} for chat {chat_id}"
         + (f" (need_mention)" if need_mention else ""))

    # Sync group config from Lark pinned card → local DB
    try:
        gc = group_config.load_config(token, chat_id, force=True)
        guests = gc.get("guests", [])
        if guests:
            handoff_db.set_guests(session_id, guests)
        msg_filter = gc.get("filter")
        if msg_filter:
            handoff_db.set_message_filter(chat_id, msg_filter)
        autoapprove = gc.get("autoapprove", False)
        handoff_db.set_autoapprove(chat_id, autoapprove)
        rules = gc.get("rules", {})
        _log(f"Group config synced: guests={len(guests)} filter={msg_filter} autoapprove={autoapprove} rules={len(rules)}")
    except Exception as e:
        _log(f"Group config sync failed (non-fatal): {e}")

    try:
        handoff_lifecycle.handoff_start(session_id, model, tool_name="Claude Agent SDK", silence=False)
    except Exception as e:
        _log(f"handoff_start failed (non-fatal): {e}")
    _log(f"Agent started. Project: {project_dir}")

    group_name = ""
    try:
        info = lark_im.get_chat_info(token, chat_id)
        group_name = info.get("name", "")
        _log(f"Group: {group_name}")
    except Exception:
        pass

    # Drain any residual takeover flag before entering the WS poll loop.
    if worker_url:
        import urllib.request
        api_key = handoff_config.load_api_key(resolved_profile)
        try:
            req = urllib.request.Request(f"{worker_url}/replies/chat:{chat_id}")
            if api_key:
                req.add_header("Authorization", f"Bearer {api_key}")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            if data.get("takeover"):
                _log("Drained residual takeover flag")
        except Exception:
            pass

    session = handoff_db.get_session(session_id) or {}
    running = True
    start_time = time.time()
    msg_count = 0
    total_cost = 0.0
    _prev_monitor = None   # Track previous monitor task to ensure WS cleanup

    def handle_signal(sig, frame):
        nonlocal running
        _log("Signal received. Shutting down...")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Persistent ClaudeSDKClient — session stays alive across turns
    from claude_agent_sdk import ClaudeSDKClient
    session_id_ref = [session_id]  # mutable ref for permission handler
    state = {"options": _build_agent_options(
        project_dir, model, credentials=credentials, chat_id=chat_id,
        session_id_ref=session_id_ref)}
    client = ClaudeSDKClient(options=state["options"])
    await client.__aenter__()
    _log("Agent SDK client initialized")

    async def _restart_client():
        nonlocal client
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            pass
        client = ClaudeSDKClient(options=state["options"])
        await client.__aenter__()
        _log("Agent SDK client restarted (session cleared)")

    while running:
        try:
            # Ensure previous monitor is fully stopped before opening new WS.
            # Without this, two WS connections overlap and worker broadcasts
            # messages to both, causing duplicate command responses.
            if _prev_monitor is not None:
                try:
                    await asyncio.wait_for(_prev_monitor, timeout=10)
                except Exception:
                    pass
                _prev_monitor = None

            _log("Waiting for message...")
            data = wait_for_reply_inline(chat_id, session, resolved_profile, timeout=300)

            if data is None or data.get("timeout"):
                continue
            if data.get("takeover"):
                _log("Taken over by another session.")
                running = False
                break

            replies = data.get("replies", [])
            if not replies:
                continue

            # Build prompt from replies
            has_media = any(
                r.get("image_key") or r.get("file_key") or r.get("msg_type") in ("image", "file")
                for r in replies
            )
            if has_media or any(r.get("parent_id") for r in replies):
                user_message = json.dumps(replies, ensure_ascii=False, indent=2)
            else:
                texts = [r.get("text", "").strip() for r in replies if r.get("text")]
                user_message = "\n".join(texts) if texts else json.dumps(replies, ensure_ascii=False)

            if not user_message.strip():
                continue

            # Strip @-mention markers so commands like "@Bot /clear" work
            if not has_media:
                for r in replies:
                    for m in (r.get("mentions") or []):
                        key = m.get("key", "")
                        if key:
                            user_message = user_message.replace(key, "")
                user_message = re.sub(r"\s+", " ", user_message).strip()

            # Check handback (exact match only to avoid false triggers)
            msg_lower = user_message.lower().strip()
            if msg_lower in ("handback", "hand back",
                             "handback dissolve", "hand back dissolve"):
                dissolve = "dissolve" in msg_lower
                body = "Agent stopped." if not dissolve else "Agent stopped. Dissolving group..."
                handoff_lifecycle.handoff_end(session_id, model, tool_name="Claude Agent SDK", body=body, silence=False)
                if dissolve:
                    try:
                        token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
                        if operator_open_id:
                            lark_im.remove_chat_members(token, chat_id, [operator_open_id])
                        lark_im.dissolve_chat(token, chat_id)
                    except Exception as e:
                        _log(f"Dissolve error: {e}")
                _log("Handback received. Stopped.")
                running = False
                break

            # Check /clear
            if msg_lower in ("/clear", "clear", "清空", "重置"):
                await _restart_client()
                token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
                import datetime
                local_time = datetime.datetime.now().strftime("%H:%M")
                card = lark_im.build_card(
                    "🔄 Session Cleared",
                    body=f"Context reset at {local_time}. Starting fresh.",
                    color="orange")
                lark_im.send_message(token, chat_id, card)
                continue

            # Check /model — switch model dynamically
            if msg_lower.startswith("/model") or msg_lower.startswith("model "):
                parts = user_message.strip().split(None, 1)
                if len(parts) >= 2:
                    new_model = parts[1].strip()
                    old_model = model
                    model = new_model
                    state["options"] = _build_agent_options(
                        project_dir, model, credentials=credentials,
                        chat_id=chat_id, session_id_ref=session_id_ref)
                    await _restart_client()
                    # Update tabs to reflect new model name
                    try:
                        handoff_lifecycle._run_tabs("end", session_id, old_model)
                        handoff_lifecycle._run_tabs("start", session_id, model)
                    except Exception:
                        pass
                    token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
                    send_response_inline(token, chat_id, f"Model switched to **{model}**. Session cleared.")
                else:
                    token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
                    send_response_inline(token, chat_id, f"Current model: **{model}**\n\nUsage: `/model claude-sonnet-4-6`")
                continue

            # Check /esc — cancel current operation (no-op if idle)
            if msg_lower in ("/esc", "esc", "cancel", "取消"):
                await _restart_client()
                token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
                send_response_inline(token, chat_id, "Operation cancelled. Ready for next message.")
                continue

            # Check /cd — change working directory
            if msg_lower.startswith("/cd ") or msg_lower.startswith("cd "):
                parts = user_message.strip().split(None, 1)
                if len(parts) >= 2:
                    new_dir = os.path.expanduser(parts[1].strip())
                    if os.path.isdir(new_dir):
                        project_dir = os.path.abspath(new_dir)
                        os.environ["HANDOFF_PROJECT_DIR"] = project_dir
                        state["options"] = _build_agent_options(
                            project_dir, model, credentials=credentials,
                            chat_id=chat_id, session_id_ref=session_id_ref)
                        await _restart_client()
                        token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
                        send_response_inline(token, chat_id, f"Working directory: **{project_dir}**\nSession cleared.")
                    else:
                        token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
                        send_response_inline(token, chat_id, f"Directory not found: `{new_dir}`")
                continue

            # Check /ping
            if msg_lower in ("/ping", "ping"):
                uptime = int(time.time() - start_time)
                h, m = divmod(uptime, 3600)
                mins, secs = divmod(m, 60)
                token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
                send_response_inline(token, chat_id,
                    f"🏓 Pong!\n"
                    f"- Model: **{model}**\n"
                    f"- Uptime: {h}h {mins}m {secs}s\n"
                    f"- Messages: {msg_count}\n"
                    f"- Cost: ${total_cost:.4f}\n"
                    f"- CWD: `{project_dir}`")
                continue

            # Check /cost
            if msg_lower in ("/cost", "cost"):
                token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
                send_response_inline(token, chat_id,
                    f"💰 Session Cost\n"
                    f"- Total: **${total_cost:.4f}**\n"
                    f"- Messages: {msg_count}\n"
                    f"- Model: {model}")
                continue

            # Check /usage
            if msg_lower in ("/usage", "usage"):
                token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
                send_response_inline(token, chat_id,
                    "Plan usage: [claude.ai/settings/usage](https://claude.ai/settings/usage)")
                continue

            # Check /help
            if msg_lower in ("/help", "help", "帮助"):
                token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
                send_response_inline(token, chat_id,
                    "**Agent Commands**\n\n"
                    "| Command | Description |\n"
                    "|---|---|\n"
                    "| `/model` | Show current model |\n"
                    "| `/model <name>` | Switch model |\n"
                    "| `/clear` | Reset conversation |\n"
                    "| `/cd <dir>` | Change working directory |\n"
                    "| `/esc` | Cancel current operation |\n"
                    "| `/ping` | Status check |\n"
                    "| `/cost` | Session API cost |\n"
                    "| `/usage` | Plan usage limits |\n"
                    "| `/help` | This help |\n"
                    "| `handback` | Stop agent |\n"
                    "| `filter <level>` | Set message filter |\n"
                    "| `autoapprove on/off` | Toggle auto-approve |")
                continue

            # Check autoapprove
            if re.match(r"(auto[\s\-]*approve|autoapprove)\s+(on|off)", msg_lower):
                enabled = "on" in msg_lower.split()[-1]
                try:
                    token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
                    group_config.set_autoapprove(token, chat_id, enabled)
                    handoff_db.set_autoapprove(chat_id, enabled)
                    send_response_inline(token, chat_id,
                        f"Auto-approve **{'enabled' if enabled else 'disabled'}**.")
                except Exception as e:
                    _log(f"autoapprove error: {e}")
                continue

            # Check filter
            if re.match(r"filter\s+(verbose|important|concise)", msg_lower):
                level = msg_lower.split()[-1]
                try:
                    import subprocess
                    subprocess.run(
                        [sys.executable, os.path.join(SCRIPT_DIR, "handoff_ops.py"),
                         "set-filter", level],
                        check=True, capture_output=True,
                    )
                    token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
                    send_response_inline(token, chat_id, f"Message filter set to **{level}**.")
                except Exception as e:
                    _log(f"filter error: {e}")
                continue

            _log(f"Processing: {user_message[:80]}")
            msg_count += 1

            # Run Agent SDK with concurrent signal monitor
            try:
                import threading
                monitor_since = session.get("last_checked") or str(int(time.time() * 1000))
                stop_event = threading.Event()

                monitor_task = asyncio.get_running_loop().run_in_executor(
                    None, _message_monitor_sync,
                    worker_url, chat_id, monitor_since, resolved_profile, stop_event,
                    session,
                )
                # Accumulate turn messages into one card (send first, PATCH subsequent)
                _turn_card_id = [None]  # mutable ref for closure
                _turn_parts = []       # collected text parts

                def _send_lark(text, task_id=None, pending_tasks=0):
                    """Send or update the turn's response card on Lark."""
                    try:
                        t = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
                        _turn_parts.append(text)
                        merged = "\n\n---\n\n".join(_turn_parts)

                        if _turn_card_id[0] is None:
                            # First message — send new card
                            send_response_inline(t, chat_id, merged)
                            # Grab the msg_id from the latest sent message
                            try:
                                latest = handoff_db.get_latest_sent_message(session_id)
                                if latest:
                                    _turn_card_id[0] = latest["message_id"]
                            except Exception:
                                pass
                            _log(f"Turn card created ({len(text)} chars, card={_turn_card_id[0]})")
                        else:
                            # Subsequent messages — PATCH update the same card
                            card = lark_im.build_markdown_card(merged)
                            try:
                                lark_im.update_card_message(t, _turn_card_id[0], card)
                                _log(f"Turn card updated ({len(text)} chars appended, total={len(merged)})")
                            except Exception as e:
                                # PATCH failed — fall back to sending a new card
                                _log(f"Turn card update failed ({e}), sending new card")
                                send_response_inline(t, chat_id, text)
                    except Exception as e:
                        _log(f"send error: {e}")

                # Working card state for this turn
                _working_msg_id = [None]
                _working_start_time = [0]

                _WORKING_TITLES = [
                    (0, "Working..."), (20, "Working hard..."),
                    (40, "Working really hard..."), (60, "Working super hard..."),
                    (90, "Working incredibly hard..."), (120, "Working unreasonably hard..."),
                ]

                def _working_fn(action, description=""):
                    """Manage Working card: start/progress/done."""
                    try:
                        t = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
                        if action == "start":
                            _working_start_time[0] = int(time.time())
                            card = lark_im.build_card(
                                "Working...", body=description or "`...`", color="grey",
                                buttons=[("Stop", "__stop__", "default")],
                                chat_id=chat_id)
                            _working_msg_id[0] = lark_im.send_message(t, chat_id, card)
                            handoff_db.set_working_message(session_id, _working_msg_id[0])
                            _log(f"Working card created: {_working_msg_id[0]}")
                        elif action == "progress" and _working_msg_id[0]:
                            elapsed = int(time.time()) - _working_start_time[0]
                            title = "Working..."
                            for threshold, t_str in _WORKING_TITLES:
                                if elapsed >= threshold:
                                    title = t_str
                            card = lark_im.build_card(
                                title, body=f"`{description}`" if description else "", color="grey",
                                buttons=[("Stop", "__stop__", "default")],
                                chat_id=chat_id)
                            try:
                                lark_im.update_card_message(t, _working_msg_id[0], card)
                            except Exception:
                                pass  # PATCH fail is non-fatal for progress
                        elif action == "done" and _working_msg_id[0]:
                            elapsed = int(time.time()) - _working_start_time[0]
                            if elapsed < 60:
                                body = f"Completed in {elapsed}s"
                            else:
                                body = f"Completed in {elapsed // 60}m {elapsed % 60}s"
                            card = lark_im.build_card("Done ✓", body=body, color="green")
                            try:
                                lark_im.update_card_message(t, _working_msg_id[0], card)
                            except Exception:
                                pass
                            handoff_db.clear_working_message(session_id)
                            _working_msg_id[0] = None
                            _log(f"Working card done ({body})")
                    except Exception as e:
                        _log(f"working_fn error: {e}")

                sdk_task = asyncio.create_task(
                    run_agent_turn(client, user_message, send_fn=_send_lark,
                                   working_fn=_working_fn))

                done, _ = await asyncio.wait(
                    {sdk_task, monitor_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if monitor_task in done:
                    monitor_signal = monitor_task.result()

                    if monitor_signal == "esc":
                        _log("Esc received — interrupting SDK")
                        try:
                            await client.interrupt()
                        except Exception as e:
                            _log(f"interrupt error: {e}")
                        try:
                            await asyncio.wait_for(sdk_task, timeout=30)
                        except asyncio.TimeoutError:
                            _log("SDK did not stop after interrupt, restarting client")
                            sdk_task.cancel()
                            await _restart_client()
                        token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
                        send_response_inline(token, chat_id, "Operation cancelled.")

                    elif monitor_signal == "handback":
                        _log("Handback received from monitor — stopping")
                        try:
                            await client.interrupt()
                        except Exception:
                            pass
                        try:
                            await asyncio.wait_for(sdk_task, timeout=10)
                        except asyncio.TimeoutError:
                            sdk_task.cancel()
                        handoff_lifecycle.handoff_end(session_id, model, tool_name="Claude Agent SDK", body="Agent stopped.", silence=False)
                        running = False
                        break

                    elif monitor_signal == "takeover":
                        _log("Taken over by another session (from monitor).")
                        try:
                            await client.interrupt()
                        except Exception:
                            pass
                        try:
                            await asyncio.wait_for(sdk_task, timeout=10)
                        except asyncio.TimeoutError:
                            sdk_task.cancel()
                        running = False
                        break

                    else:
                        # Monitor exited without signal (WS disconnect).
                        # Wait for SDK to finish normally.
                        _log("Monitor exited (WS disconnect) — waiting for SDK to finish")
                        turn_cost = await sdk_task
                        total_cost += turn_cost
                        _log("Agent turn done (WS reconnect path).")
                else:
                    # SDK finished first — stop monitor
                    stop_event.set()
                    turn_cost = sdk_task.result()
                    total_cost += turn_cost
                    _log("Agent turn done.")

                    # Ensure monitor is cleaned up
                    try:
                        await asyncio.wait_for(monitor_task, timeout=5)
                    except asyncio.TimeoutError:
                        _prev_monitor = monitor_task
                    except Exception:
                        pass

                # Pending tasks are cancelled in run_agent_turn via
                # client.stop_task(). No timeout/restart needed.

            except Exception as e:
                _log(f"Agent error: {e}")
                try:
                    token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
                    send_response_inline(token, chat_id, f"**Error:**\n```\n{str(e)[:500]}\n```")
                except Exception:
                    pass

        except KeyboardInterrupt:
            running = False
        except Exception as e:
            _log(f"Loop error: {e}")
            await asyncio.sleep(5)

    try:
        await client.__aexit__(None, None, None)
    except Exception:
        pass
    try:
        handoff_lifecycle.deactivate(session_id)
    except Exception:
        pass
    _log("Agent stopped.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Handoff agent (Agent SDK)")
    parser.add_argument("--chat-id", required=True, help="Lark chat ID")
    parser.add_argument("--project-dir", required=True, help="Project directory")
    parser.add_argument("--model", default="claude-opus-4-6", help="Model to use")
    parser.add_argument("--profile", default=None, help="Config profile name")
    args = parser.parse_args()

    project_dir = os.path.abspath(args.project_dir)
    if not os.path.isdir(project_dir):
        print(f"Error: {project_dir} is not a directory", file=sys.stderr)
        return 1

    return asyncio.run(main_loop(args.chat_id, project_dir, args.model, args.profile))


if __name__ == "__main__":
    sys.exit(main() or 0)
