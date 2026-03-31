#!/usr/bin/env python3
"""Handoff daemon: background agent using Claude Agent SDK.

Architecture: daemon manages the message wait loop, Agent SDK processes
each message via ClaudeSDKClient. The agent sends responses to Lark
via send_to_group.py (same as CLI handoff). Daemon provides fallback
if the agent doesn't send.

Usage:
    python3 scripts/handoff_agent.py --chat-id <CHAT_ID> --project-dir <DIR> [--model <MODEL>]

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
    sidecar_mode = session.get("sidecar_mode", False)
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
                return {"takeover": True}
            replies = result.get("replies", [])
            if replies:
                replies = wfr_mod.filter_self_bot(replies, bot_open_id)
            if replies:
                replies = (wfr_mod.filter_by_allowed_senders(replies, operator_open_id, member_roles)
                           if member_roles else wfr_mod.filter_by_operator(replies, operator_open_id))
            if sidecar_mode and replies:
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
                if sidecar_mode:
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


def _build_agent_options(project_dir, model):
    """Build ClaudeAgentOptions."""
    from claude_agent_sdk import ClaudeAgentOptions

    # Pass handoff env vars + correct PATH for Bash tool
    handoff_env = {
        "PATH": f"/opt/homebrew/bin:/opt/homebrew/sbin:{os.environ.get('PATH', '/usr/bin:/bin')}",
    }
    for key in ("HANDOFF_SESSION_ID", "HANDOFF_PROJECT_DIR", "HANDOFF_SESSION_TOOL",
                "HANDOFF_TMP_DIR", "http_proxy", "https_proxy", "all_proxy",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        val = os.environ.get(key)
        if val:
            handoff_env[key] = val

    return ClaudeAgentOptions(
        allowed_tools=["Skill", "Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        setting_sources=["user", "project"],
        permission_mode="bypassPermissions",
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": (
                "\n\nYou are a Handoff agent powered by Claude Agent SDK, chatting with "
                "a user via Lark (Feishu) group chat. You are NOT running in a terminal "
                "or CLI — you are a background agent process.\n\n"
                "## Message handling\n"
                "Messages arrive as JSON with fields like text, image_key, file_key, "
                "parent_id, msg_type. When a message has image_key, download each image "
                "with `python3 scripts/handoff_ops.py download-image --image-key '<KEY>' "
                "--message-id '<MSG_ID>'` then Read the file to see it before responding. "
                "When a message has file_key, download with "
                "`python3 scripts/handoff_ops.py download-file --file-key '<KEY>' "
                "--message-id '<MSG_ID>' --file-name '<NAME>'` then Read it. "
                "When a message has parent_id, resolve context with "
                "`python3 scripts/handoff_ops.py parent-local --parent-id '<ID>'`.\n\n"
                "## Responses\n"
                "Send responses via `python3 scripts/send_to_group.py '<message>'`. "
                "Use 2-space indentation in code blocks for mobile readability.\n\n"
                "## Built-in commands (handled by daemon, not you)\n"
                "These commands are intercepted before reaching you: handback, /clear, "
                "/model <name>, /esc. Do not try to handle them yourself."
            ),
        },
        cwd=project_dir,
        model=model,
        env=handoff_env,
    )


async def run_agent_turn(client, prompt):
    """Send a prompt to the persistent SDK client. Returns (result_text, cost)."""
    from claude_agent_sdk import AssistantMessage, TextBlock, ResultMessage

    await client.query(prompt)
    result_text = None
    cost = 0.0
    async for message in client.receive_response():
        if isinstance(message, ResultMessage):
            result_text = message.result
            cost = getattr(message, "total_cost_usd", 0) or 0.0
            _log(f"Agent done. Cost: ${cost:.4f}")
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    result_text = block.text
    return result_text or "(no response)", cost


async def main_loop(chat_id, project_dir, model, profile=None):
    """Main daemon loop."""
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
    try:
        email = credentials.get("email", "")
        if email:
            user_info = lark_im.get_user_by_email(token, email)
            operator_open_id = user_info.get("open_id", "")
    except Exception:
        pass

    # Clean up any stale session for this chat_id before activating
    try:
        existing = handoff_db.get_active_sessions()
        for s in existing:
            if s.get("chat_id") == chat_id:
                _log(f"Cleaning stale session {s['session_id']} for chat {chat_id}")
                handoff_db.deactivate_handoff(s["session_id"])
    except Exception as e:
        _log(f"Stale session cleanup error: {e}")

    handoff_lifecycle.activate(
        session_id, chat_id, model,
        operator_open_id=operator_open_id,
        config_profile=resolved_profile,
    )
    _log(f"Activated session {session_id} for chat {chat_id}")

    handoff_lifecycle.handoff_start(session_id, model, tool_name="Claude Agent SDK", silence=False)
    _log(f"Daemon started. Project: {project_dir}")

    group_name = ""
    try:
        info = lark_im.get_chat_info(token, chat_id)
        group_name = info.get("name", "")
        _log(f"Group: {group_name}")
    except Exception:
        pass

    session = handoff_db.get_session(session_id) or {}
    running = True
    start_time = time.time()
    msg_count = 0
    total_cost = 0.0

    def handle_signal(sig, frame):
        nonlocal running
        _log("Signal received. Shutting down...")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Persistent ClaudeSDKClient — session stays alive across turns
    from claude_agent_sdk import ClaudeSDKClient
    state = {"options": _build_agent_options(project_dir, model)}
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

            # Check handback
            msg_lower = user_message.lower().strip()
            if ("handback" in msg_lower or "hand back" in msg_lower
                    or msg_lower in ("退出", "停止", "结束", "stop", "quit", "exit")):
                dissolve = "dissolve" in msg_lower
                body = "Daemon stopped." if not dissolve else "Daemon stopped. Dissolving group..."
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
                send_response_inline(token, chat_id, "Session cleared. Starting fresh.")
                continue

            # Check /model — switch model dynamically
            if msg_lower.startswith("/model") or msg_lower.startswith("model "):
                parts = user_message.strip().split(None, 1)
                if len(parts) >= 2:
                    new_model = parts[1].strip()
                    old_model = model
                    model = new_model
                    state["options"] = _build_agent_options(project_dir, model)
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
                        state["options"] = _build_agent_options(project_dir, model)
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
            if msg_lower in ("/cost", "cost", "/usage", "usage"):
                token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
                send_response_inline(token, chat_id,
                    f"💰 Usage\n"
                    f"- Total cost: **${total_cost:.4f}**\n"
                    f"- Messages processed: {msg_count}\n"
                    f"- Model: {model}")
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
                    "| `/cost` | Show API usage |\n"
                    "| `/help` | This help |\n"
                    "| `handback` | Stop agent |\n"
                    "| `filter <level>` | Set message filter |\n"
                    "| `autoapprove on/off` | Toggle auto-approve |")
                continue

            _log(f"Processing: {user_message[:80]}")
            msg_count += 1

            # Run Agent SDK — agent sends via send_to_group.py (env vars passed)
            try:
                result, turn_cost = await run_agent_turn(client, user_message)
                total_cost += turn_cost
                _log(f"Agent turn done. Result length: {len(result)}")

                # Always send agent's output as fallback.
                # If agent already sent via send_to_group.py, this is a duplicate
                # that the user can ignore. Better to double-send than not send.
                if result and result.strip():
                    token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])
                    send_response_inline(token, chat_id, result)
                    _log("Response sent.")

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
