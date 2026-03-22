#!/usr/bin/env python3
"""Handoff daemon: fully managed handoff using Claude Agent SDK.

No CLI dependency. Reuses existing handoff scripts (wait_for_reply.py,
send_to_group.py) for Lark I/O, and Claude Agent SDK for processing.

Usage:
    python3 scripts/daemon.py --chat-id <CHAT_ID> --project-dir <DIR> [--model <MODEL>]

The daemon:
1. Activates a handoff session (registers in DB)
2. Sends a startup card to Lark
3. Waits for messages via wait_for_reply.py (WebSocket)
4. Processes each message with Claude Agent SDK
5. Sends response via send_to_group.py (markdown card)
6. Loops until "handback" is received

Requirements:
    pip install claude-agent-sdk
"""

import argparse
import asyncio
import json
import os
import signal
import subprocess
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


def _run_script(script_name, *args, timeout=600):
    """Run a handoff script and return its stdout."""
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, script_name)] + list(args)
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        env=os.environ,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if stderr:
            _log(f"{script_name} stderr: {stderr[:200]}")
    return result.stdout.strip()


def wait_for_reply(timeout=0):
    """Wait for a Lark message using wait_for_reply.py. Returns parsed JSON or None."""
    args = ["--timeout", str(timeout)]
    try:
        output = _run_script("wait_for_reply.py", *args, timeout=max(timeout + 60, 600))
        if not output:
            return None
        # The script may print multiple lines; the JSON is the last line
        for line in reversed(output.split("\n")):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        return None
    except subprocess.TimeoutExpired:
        return None
    except Exception as e:
        _log(f"wait_for_reply error: {e}")
        return None


def send_to_group(text, card=False):
    """Send a message to Lark using send_to_group.py."""
    args = [text]
    if card:
        args.append("--card")
    try:
        _run_script("send_to_group.py", *args, timeout=30)
    except Exception as e:
        _log(f"send_to_group error: {e}")


async def run_agent(prompt, project_dir, session_id=None, model="claude-opus-4-6"):
    """Run Claude Agent SDK with the given prompt. Returns (result_text, session_id)."""
    from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, SystemMessage

    options = ClaudeAgentOptions(
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        cwd=project_dir,
        model=model,
    )
    if session_id:
        options.resume = session_id

    result_text = None
    new_session_id = None

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, SystemMessage) and message.subtype == "init":
            new_session_id = message.data.get("session_id")
            _log(f"Agent session: {new_session_id}")
        elif isinstance(message, ResultMessage):
            result_text = message.result
            if not new_session_id:
                new_session_id = getattr(message, "session_id", None)
            cost = getattr(message, "total_cost_usd", 0)
            _log(f"Agent done. Cost: ${cost:.4f}")

    return result_text or "(no response)", new_session_id


async def main_loop(chat_id, project_dir, model):
    """Main daemon loop."""
    # Always generate a fresh session_id for the daemon — never inherit from
    # the parent environment to avoid cross-session message leaking
    import uuid
    session_id = str(uuid.uuid4())
    os.environ["HANDOFF_SESSION_ID"] = session_id
    os.environ["HANDOFF_PROJECT_DIR"] = project_dir
    os.environ["HANDOFF_SESSION_TOOL"] = "Daemon"

    # Activate handoff in DB
    credentials = handoff_config.load_credentials()
    if not credentials:
        print("Error: No credentials configured.", file=sys.stderr)
        return 1

    token = lark_im.get_tenant_token(credentials["app_id"], credentials["app_secret"])

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
    )
    _log(f"Activated session {session_id} for chat {chat_id}")

    # Send startup card
    handoff_lifecycle.handoff_start(session_id, model, tool_name="Daemon", silence=False)
    _log(f"Daemon started. Project: {project_dir}")

    agent_session_id = None
    running = True

    def handle_signal(sig, frame):
        nonlocal running
        _log("Signal received. Shutting down...")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while running:
        try:
            # Wait for message
            _log("Waiting for message...")
            data = wait_for_reply(timeout=0)

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

            # Concatenate all reply texts
            texts = [r.get("text", "").strip() for r in replies if r.get("text")]
            if not texts:
                continue
            user_message = "\n".join(texts)

            # Check for handback
            if user_message.lower().strip() in ("handback", "hand back"):
                handoff_lifecycle.handoff_end(
                    session_id, model, tool_name="Daemon",
                    body="Daemon stopped.", silence=False,
                )
                _log("Handback received. Stopped.")
                running = False
                break

            _log(f"Processing: {user_message[:80]}")

            # Run Agent SDK
            try:
                result, new_sid = await run_agent(
                    prompt=user_message,
                    project_dir=project_dir,
                    session_id=agent_session_id,
                    model=model,
                )
                if new_sid:
                    agent_session_id = new_sid

                # Send response
                send_to_group(result)
                _log("Response sent.")

            except Exception as e:
                _log(f"Agent error: {e}")
                send_to_group(f"**Error:**\n```\n{str(e)[:500]}\n```")

        except KeyboardInterrupt:
            running = False
        except Exception as e:
            _log(f"Loop error: {e}")
            await asyncio.sleep(5)

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
    args = parser.parse_args()

    project_dir = os.path.abspath(args.project_dir)
    if not os.path.isdir(project_dir):
        print(f"Error: {project_dir} is not a directory", file=sys.stderr)
        return 1

    return asyncio.run(main_loop(args.chat_id, project_dir, args.model))


if __name__ == "__main__":
    sys.exit(main() or 0)
