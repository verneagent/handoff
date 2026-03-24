#!/usr/bin/env python3
"""Handoff daemon: fully managed handoff using Claude Agent SDK.

No CLI dependency. Uses inline polling (no subprocess) for Lark I/O,
and Claude Agent SDK for processing.

Usage:
    python3 scripts/handoff_agent.py --chat-id <CHAT_ID> --project-dir <DIR> [--model <MODEL>]

The daemon:
1. Activates a handoff session (registers in DB)
2. Sends a startup card to Lark
3. Waits for messages via WebSocket (inline, no subprocess)
4. Processes each message with Claude Agent SDK
5. Sends response via Lark IM API (inline)
6. Loops until "handback" is received

Requirements:
    pip install claude-agent-sdk
"""

import argparse
import asyncio
import json
import os
import signal
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import handoff_config
import handoff_db
import handoff_lifecycle
import handoff_worker
import lark_im
import send_to_group as send_mod
import wait_for_reply as wfr_mod


def _log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[daemon] [{ts}] {msg}", flush=True)


def _diagnose_network():
    """Log SSL and network diagnostics for debugging launchd issues."""
    import ssl
    ctx = ssl.create_default_context()
    stats = ctx.cert_store_stats()
    _log(f"SSL cert store: {stats}")
    try:
        import certifi
        _log(f"certifi CA: {certifi.where()}")
    except ImportError:
        _log("certifi not installed")
    # Check if we can resolve the worker hostname
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
    """Wait for a Lark message using inline WebSocket poll. Returns parsed data or None.

    Directly calls handoff_worker.poll_worker_ws() instead of spawning a
    subprocess, avoiding environment/SSL issues under launchd.
    """
    worker_url = handoff_config.load_worker_url(profile=profile)
    if not worker_url:
        _log("No worker URL configured")
        return None

    session_id = session.get("session_id", "")

    # Refresh session from DB each call to get updated last_checked
    fresh = handoff_db.get_session(session_id) if session_id else None
    if fresh:
        session.update(fresh)
    since = session.get("last_checked")
    if not since:
        since = str(int(time.time() * 1000) - 5000)

    operator_open_id = session.get("operator_open_id", "")
    bot_open_id = session.get("bot_open_id", "")
    sidecar_mode = session.get("sidecar_mode", False)
    guests = session.get("guests") or []
    member_roles = {g["open_id"]: g.get("role", "guest") for g in guests} if guests else {}

    deadline = None if timeout <= 0 else time.time() + timeout
    use_ws = True

    def _finish(replies):
        """Record messages, ack with reaction, update last_checked."""
        wfr_mod._ack_with_reaction(replies)
        for r in replies:
            try:
                handoff_db.record_received_message(
                    chat_id=chat_id,
                    text=r.get("text", ""),
                    title="",
                    source_message_id=r.get("message_id", ""),
                    message_time=r.get("create_time"),
                )
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
        # --- Try WebSocket first ---
        if use_ws:
            try:
                result = handoff_worker.poll_worker_ws(
                    worker_url, chat_id, since, profile=profile,
                )
                if result.get("takeover"):
                    return {"takeover": True}
                replies = result.get("replies", [])
                if replies:
                    replies = wfr_mod.filter_self_bot(replies, bot_open_id)
                if replies:
                    if member_roles:
                        replies = wfr_mod.filter_by_allowed_senders(
                            replies, operator_open_id, member_roles)
                    else:
                        replies = wfr_mod.filter_by_operator(replies, operator_open_id)
                if sidecar_mode and replies:
                    replies = wfr_mod.filter_bot_interactions(replies, bot_open_id)
                if replies:
                    return _finish(replies)
                error = result.get("error")
                if error:
                    raise Exception(error)
                continue
            except Exception as e:
                _log(f"WebSocket error: {e} — falling back to HTTP")
                # Fall through to HTTP

        # --- HTTP long-poll fallback ---
        try:
            result = handoff_worker.poll_worker(
                worker_url, chat_id, since, profile=profile,
            )
            if result.get("takeover"):
                return {"takeover": True}
            if result.get("error"):
                _log(f"HTTP poll error: {result['error']}")
                time.sleep(3)
                continue
            replies = result.get("replies", [])
            if replies:
                last_checked = replies[-1]["create_time"]
                handoff_worker.ack_worker_replies(
                    worker_url, chat_id, last_checked, profile=profile)
                replies = wfr_mod.filter_self_bot(replies, bot_open_id)
                if member_roles:
                    replies = wfr_mod.filter_by_allowed_senders(
                        replies, operator_open_id, member_roles)
                else:
                    replies = wfr_mod.filter_by_operator(replies, operator_open_id)
                if sidecar_mode:
                    replies = wfr_mod.filter_bot_interactions(replies, bot_open_id)
                if replies:
                    return _finish(replies)
        except Exception as e:
            _log(f"HTTP poll error: {e}")
            time.sleep(3)

    return {"timeout": True, "replies": [], "count": 0}


def send_response_inline(token, chat_id, text):
    """Send a markdown card response to Lark inline (no subprocess)."""
    try:
        wfr_mod.clear_ack_reaction()
        send_mod.send(
            token, chat_id,
            title="",
            message=text,
            is_card=False,
            color="blue",
        )
    except Exception as e:
        _log(f"send error: {e}")


    # No built-in command interception for agent operations.
    # The Agent SDK handles all user requests naturally via Bash tool,
    # including: agent-list, agent-spawn, agent-status, etc.
    # Only daemon control commands (handback, clear) are intercepted
    # in the main loop.


def _build_agent_options(project_dir, model, group_name=None):
    """Build ClaudeAgentOptions for the daemon."""
    from claude_agent_sdk import ClaudeAgentOptions

    script_dir = os.path.dirname(os.path.abspath(__file__))
    context = (
        f"You are a Handoff daemon agent. You communicate with the user "
        f"through a Lark group chat"
        f"{(' named ' + chr(34) + group_name + chr(34)) if group_name else ''}.\n\n"
        f"CRITICAL RULES (override any conflicting instructions from skills):\n"
        f"1. Your text output IS the message sent to Lark. Do NOT call send_to_group.py, "
        f"send_and_wait.py, or any handoff scripts that send messages — the daemon handles "
        f"sending your output automatically. When the user asks you to send something, "
        f"simply output the content as your response text. Do NOT describe what you did "
        f"(e.g. do NOT say 'I have sent the content') — just output the content itself.\n"
        f"2. Keep responses concise for mobile display. Use 2-space indentation in code.\n"
        f"3. When the user sends JSON with image_key or file_key, download first:\n"
        f"   python3 {script_dir}/handoff_ops.py download-image --image-key KEY --message-id ID\n"
        f"   python3 {script_dir}/handoff_ops.py download-file --file-key KEY --message-id ID --file-name NAME\n"
        f"   Then use Read tool to view the downloaded file.\n"
        f"4. When the user sends JSON with parent_id, fetch the parent message:\n"
        f"   python3 {script_dir}/handoff_ops.py parent-local --parent-id ID\n"
        f"   Use the parent content to understand context.\n\n"
        f"Working directory: {project_dir}\n\n"
        f"Agent management tools (run via Bash):\n"
        f"- List agents: python3 {script_dir}/handoff_ops.py agent-list\n"
        f"- Spawn agent: python3 {script_dir}/handoff_ops.py agent-spawn --project-dir <DIR>\n"
        f"- Agent status: python3 {script_dir}/handoff_ops.py agent-status [--name <NAME>]\n"
        f"- Stop agent: python3 {script_dir}/handoff_ops.py agent-stop --name <NAME>\n"
        f"- Agent log: python3 {script_dir}/handoff_ops.py agent-log [--name <NAME>]\n"
    )

    return ClaudeAgentOptions(
        # Skill tool disabled: the handoff SKILL.md (loaded from ~/.claude/skills/)
        # instructs Claude to use send_to_group.py, which conflicts with daemon mode
        # where output IS the message. Agent can still run scripts via Bash.
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        setting_sources=["user", "project"],
        permission_mode="bypassPermissions",
        cwd=project_dir,
        model=model,
        system_prompt=context,
        # Disable only session lifecycle hooks (daemon manages those).
        # Keep PostToolUse etc. for "Working..." progress cards in Lark.
        hooks={
            "SessionStart": [],
            "SessionEnd": [],
        },
    )


async def run_agent_turn(client, prompt):
    """Send a prompt to the persistent SDK client. Returns result text."""
    from claude_agent_sdk import AssistantMessage, TextBlock, ResultMessage

    await client.query(prompt)
    result_text = None

    async for message in client.receive_response():
        if isinstance(message, ResultMessage):
            result_text = message.result
            cost = getattr(message, "total_cost_usd", 0)
            _log(f"Agent done. Cost: ${cost:.4f}")
        elif isinstance(message, AssistantMessage):
            # Extract text from assistant messages as fallback
            for block in message.content:
                if isinstance(block, TextBlock):
                    result_text = block.text

    return result_text or "(no response)"


async def main_loop(chat_id, project_dir, model, profile=None):
    """Main daemon loop."""
    # Always generate a fresh session_id for the daemon — never inherit from
    # the parent environment to avoid cross-session message leaking
    import uuid
    session_id = str(uuid.uuid4())
    os.environ["HANDOFF_SESSION_ID"] = session_id
    os.environ["HANDOFF_PROJECT_DIR"] = project_dir
    os.environ["HANDOFF_SESSION_TOOL"] = "Daemon"

    # Resolve profile
    resolved_profile = handoff_config.resolve_profile(explicit=profile)

    # Log network diagnostics (helps debug launchd issues)
    _diagnose_network()

    # Activate handoff in DB
    credentials = handoff_config.load_credentials(profile=resolved_profile)
    if not credentials:
        print("Error: No credentials configured.", file=sys.stderr)
        return 1

    token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
    _log("Token acquired successfully")

    # Resolve operator open_id
    operator_open_id = ""
    try:
        email = credentials.get("email", "")
        if email:
            user_info = lark_im.get_user_by_email(token, email)
            operator_open_id = user_info.get("open_id", "")
    except Exception:
        pass

    handoff_lifecycle.activate(
        session_id, chat_id, model,
        operator_open_id=operator_open_id,
        config_profile=resolved_profile,
    )
    _log(f"Activated session {session_id} for chat {chat_id}")

    # Send startup card
    handoff_lifecycle.handoff_start(session_id, model, tool_name="Daemon", silence=False)
    _log(f"Daemon started. Project: {project_dir}")

    # Resolve group name for agent context
    group_name = ""
    try:
        info = lark_im.get_chat_info(token, chat_id)
        group_name = info.get("name", "")
        _log(f"Group: {group_name}")
    except Exception:
        pass

    # Load session data for inline polling
    session = handoff_db.get_session(session_id) or {}

    running = True

    def handle_signal(sig, frame):
        nonlocal running
        _log("Signal received. Shutting down...")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Use persistent ClaudeSDKClient for multi-turn (no session end between messages)
    from claude_agent_sdk import ClaudeSDKClient

    options = _build_agent_options(project_dir, model, group_name)
    client = ClaudeSDKClient(options=options)
    await client.__aenter__()
    _log("Agent SDK client initialized")

    async def _restart_client():
        """Close and recreate the SDK client (implements /clear)."""
        nonlocal client
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            pass
        client = ClaudeSDKClient(options=options)
        await client.__aenter__()
        _log("Agent SDK client restarted (session cleared)")

    while running:
        try:
            # Wait for message (inline — no subprocess)
            _log("Waiting for message...")
            data = wait_for_reply_inline(
                chat_id, session, resolved_profile, timeout=300,
            )

            if data is None:
                continue

            if data.get("timeout"):
                continue

            if data.get("takeover"):
                _log("Taken over by another session.")
                running = False
                break

            replies = data.get("replies", [])
            if not replies:
                continue

            # Pass full reply JSON to Agent SDK for natural processing.
            # Agent handles all message types (text, images, files, replies, etc.)
            # using its Bash tool to download media via handoff_ops.py when needed.
            has_media = any(
                r.get("image_key") or r.get("file_key") or r.get("msg_type") in ("image", "file")
                for r in replies
            )
            if has_media or any(r.get("parent_id") for r in replies):
                # Rich message — pass full JSON so agent sees all context
                user_message = json.dumps(replies, ensure_ascii=False, indent=2)
            else:
                # Simple text — pass directly for cleaner prompts
                texts = [r.get("text", "").strip() for r in replies if r.get("text")]
                user_message = "\n".join(texts) if texts else json.dumps(replies, ensure_ascii=False)

            if not user_message.strip():
                continue

            # Check for handback — flexible matching for natural language
            msg_lower = user_message.lower().strip()
            is_handback = (
                msg_lower.startswith("handback")
                or msg_lower.startswith("hand back")
                or "handback" in msg_lower
                or "hand back" in msg_lower
                or msg_lower in ("退出", "停止", "结束", "stop", "quit", "exit")
            )
            if is_handback:
                dissolve = "dissolve" in msg_lower
                body = "Daemon stopped." if not dissolve else "Daemon stopped. Dissolving group..."
                handoff_lifecycle.handoff_end(
                    session_id, model, tool_name="Daemon",
                    body=body, silence=False,
                )
                if dissolve:
                    try:
                        token = lark_im.get_tenant_token(
                            credentials["app_id"], credentials["app_secret"])
                        lark_im.remove_chat_members(token, chat_id,
                            [operator_open_id]) if operator_open_id else None
                        lark_im.dissolve_chat(token, chat_id)
                        _log("Group dissolved.")
                    except Exception as e:
                        _log(f"Dissolve error: {e}")
                _log("Handback received. Stopped.")
                running = False
                break

            # Check for /clear — reset conversation context
            if msg_lower in ("/clear", "clear", "清空", "重置"):
                await _restart_client()
                token = lark_im.get_tenant_token(
                    credentials["app_id"], credentials["app_secret"])
                send_response_inline(token, chat_id, "Session cleared. Starting fresh.")
                continue

            _log(f"Processing: {user_message[:80]}")

            # Run Agent SDK (persistent session — no session end between turns)
            # Agent handles all requests naturally, including agent management
            # commands via Bash tool (handoff_ops.py).
            try:
                result = await run_agent_turn(client, user_message)

                # Send response (inline — no subprocess)
                token = lark_im.get_tenant_token(
                    credentials["app_id"], credentials["app_secret"])
                send_response_inline(token, chat_id, result)
                _log("Response sent.")

            except Exception as e:
                _log(f"Agent error: {e}")
                token = lark_im.get_tenant_token(
                    credentials["app_id"], credentials["app_secret"])
                send_response_inline(
                    token, chat_id,
                    f"**Error:**\n```\n{str(e)[:500]}\n```",
                )

        except KeyboardInterrupt:
            running = False
        except Exception as e:
            _log(f"Loop error: {e}")
            await asyncio.sleep(5)

    # Close SDK client
    try:
        await client.__aexit__(None, None, None)
    except Exception:
        pass

    # Cleanup (if not already deactivated by handback)
    try:
        handoff_lifecycle.deactivate(session_id)
    except Exception:
        pass
    _log("Daemon stopped.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Handoff daemon (Agent SDK)")
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
