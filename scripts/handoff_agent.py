#!/usr/bin/env python3
"""Handoff daemon: thin supervisor that runs Agent SDK in handoff loop mode.

The daemon activates a handoff session, then gives the Agent SDK a prompt
that triggers the handoff skill's Main Loop. The agent handles all Lark I/O
(wait_for_reply, send_to_group, etc.) just like normal CLI handoff.

The daemon only manages:
- Session activation/deactivation
- Agent restart on /clear
- Signal handling (SIGTERM/SIGINT)
- Crash recovery (auto-restart)

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
import lark_im


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


def _build_agent_options(project_dir, model, session_model):
    """Build ClaudeAgentOptions for the agent."""
    from claude_agent_sdk import ClaudeAgentOptions

    # Pass handoff env vars + correct PATH so agent's Bash tool works
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
        cwd=project_dir,
        model=model,
        env=handoff_env,
        # Disable session lifecycle hooks — daemon manages those.
        # Keep all other hooks (PostToolUse → Working cards, etc.)
        hooks={
            "SessionStart": [],
            "SessionEnd": [],
        },
    )


# The prompt that triggers the agent to enter handoff mode.
# It invokes the handoff skill which reads SKILL.md and enters the Main Loop.
HANDOFF_LOOP_PROMPT = (
    "You are now in handoff daemon mode. The handoff session is already activated "
    "and the start card has been sent. Enter the Main Loop from SKILL.md:\n\n"
    "1. Call wait_for_reply.py to wait for the first user message\n"
    "2. Process the message (Step 3)\n"
    "3. Send your response via send_to_group.py (NOT send_and_wait.py)\n"
    "4. After sending, call wait_for_reply.py again for the next message\n"
    "5. Repeat until the user says handback\n\n"
    "IMPORTANT:\n"
    "- Use send_to_group.py (not send_and_wait.py) for responses.\n"
    "- The session_model is '{session_model}'.\n"
    "- When user asks to 'start/open/spawn a new agent' in a directory, use:\n"
    "  python3 {script_dir}/handoff_ops.py agent-spawn --project-dir '<DIR>'\n"
    "  Do NOT use /wksp or open iTerm2 — use agent-spawn for daemon agents.\n\n"
    "Start by calling wait_for_reply.py now."
)


async def run_agent_loop(project_dir, model, session_model):
    """Run the agent in handoff loop mode. Returns exit reason string."""
    from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, SystemMessage

    options = _build_agent_options(project_dir, model, session_model)
    prompt = (HANDOFF_LOOP_PROMPT
              .replace("{session_model}", session_model)
              .replace("{script_dir}", SCRIPT_DIR))

    _log("Starting agent in handoff loop mode...")

    result_text = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, SystemMessage) and message.subtype == "init":
            sid = message.data.get("session_id")
            _log(f"Agent session: {sid}")
        elif isinstance(message, ResultMessage):
            result_text = message.result
            cost = getattr(message, "total_cost_usd", 0)
            _log(f"Agent exited. Cost: ${cost:.4f}")

    return result_text or ""


async def main_loop(chat_id, project_dir, model, profile=None):
    """Main daemon loop — supervisor that restarts agent on /clear."""
    import uuid
    session_id = str(uuid.uuid4())
    os.environ["HANDOFF_SESSION_ID"] = session_id
    os.environ["HANDOFF_PROJECT_DIR"] = project_dir
    os.environ["HANDOFF_SESSION_TOOL"] = "Daemon"

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

    handoff_lifecycle.activate(
        session_id, chat_id, model,
        operator_open_id=operator_open_id,
        config_profile=resolved_profile,
    )
    _log(f"Activated session {session_id} for chat {chat_id}")

    handoff_lifecycle.handoff_start(session_id, model, tool_name="Daemon", silence=False)
    _log(f"Daemon started. Project: {project_dir}")

    session_model = model
    running = True

    def handle_signal(sig, frame):
        nonlocal running
        _log("Signal received. Shutting down...")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while running:
        try:
            # Run agent — it enters the handoff Main Loop and handles all I/O
            result = await run_agent_loop(project_dir, model, session_model)
            _log(f"Agent loop exited. Result: {result[:100] if result else '(empty)'}")

            # Check exit reason
            result_lower = result.lower() if result else ""
            if "handback" in result_lower or "hand back" in result_lower:
                _log("Handback detected. Stopping daemon.")
                running = False
            elif any(kw in result_lower for kw in ("/clear", "clear", "清空", "重置")):
                _log("Clear detected. Restarting agent with fresh session...")
                # Agent exited due to /clear — restart with new session
                continue
            else:
                # Agent exited unexpectedly — restart after delay
                _log("Agent exited unexpectedly. Restarting in 5s...")
                await asyncio.sleep(5)

        except KeyboardInterrupt:
            running = False
        except Exception as e:
            _log(f"Agent error: {e}")
            await asyncio.sleep(5)

    # Cleanup
    try:
        handoff_lifecycle.handoff_end(
            session_id, model, tool_name="Daemon",
            body="Daemon stopped.", silence=False,
        )
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
