#!/usr/bin/env python3
"""SessionStart hook: persist session ID, project dir, and resume active handoff.

When a Claude session starts or resumes, this hook:
1. Writes HANDOFF_SESSION_ID, HANDOFF_SESSION_TOOL, and HANDOFF_PROJECT_DIR
   to CLAUDE_ENV_FILE so scripts know their ID, tool, and project root.
2. If handoff is active AND owned by this session, silences terminal
   notifications and outputs context for Claude to resume the handoff loop.
"""

import json
import os
import shlex
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import handoff_db


_LOG_FILE = os.path.join(
    os.environ.get("HANDOFF_TMP_DIR") or "/tmp/handoff",
    "session-start.log",
)


def _log(msg):
    """Write to persistent log file for post-mortem diagnosis."""
    try:
        os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)
        try:
            if os.path.getsize(_LOG_FILE) > 256 * 1024:
                with open(_LOG_FILE, "rb") as f:
                    f.seek(-128 * 1024, 2)
                    tail = f.read()
                nl = tail.find(b"\n")
                if nl >= 0:
                    tail = tail[nl + 1 :]
                with open(_LOG_FILE, "wb") as f:
                    f.write(tail)
        except Exception:
            pass
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(_LOG_FILE, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def warn(msg):
    print(f"[handoff] {msg}", file=sys.stderr)
    _log(msg)


def _is_agent_mode():
    """Check if running inside Claude Agent SDK (not CLI)."""
    return os.environ.get("HANDOFF_SESSION_TOOL") == "Claude Agent SDK"


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception as e:
        warn(f"invalid SessionStart hook input JSON: {e}")
        hook_input = {}

    session_id = hook_input.get("session_id", "")
    warn(f"hook fired: session_id={session_id!r}, keys={list(hook_input.keys())}")

    # In agent mode, the agent process manages lifecycle (env vars, activation, cards).
    # Skip CLI-specific setup but let the hook run cleanly.
    if _is_agent_mode():
        warn("agent mode detected — skipping CLI-specific setup")
        return

    # Persist session_id and project dir as env vars for subsequent Bash commands
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    _log(f"env_file={env_file!r} project_dir={project_dir!r} session_id={session_id!r}")

    # Log existing env file contents before overwriting
    if env_file and os.path.exists(env_file):
        try:
            with open(env_file) as f:
                old_content = f.read().strip()
            if old_content:
                _log(f"env_file BEFORE overwrite: {old_content!r}")
        except Exception:
            pass

    if env_file and session_id:
        try:
            managed_prefixes = (
                "export HANDOFF_SESSION_ID=",
                "export HANDOFF_SESSION_TOOL=",
                "export HANDOFF_PROJECT_DIR=",
                "export HANDOFF_PROFILE=",
            )
            lines = []
            if os.path.exists(env_file):
                with open(env_file) as f:
                    lines = [
                        l
                        for l in f.readlines()
                        if not l.startswith(managed_prefixes)
                    ]
            lines.append(f"export HANDOFF_SESSION_ID={shlex.quote(session_id)}\n")
            lines.append(f"export HANDOFF_SESSION_TOOL={shlex.quote('Claude Code')}\n")
            if project_dir:
                lines.append(f"export HANDOFF_PROJECT_DIR={shlex.quote(project_dir)}\n")
            # Preserve HANDOFF_PROFILE if the session has a non-default profile
            session = handoff_db.get_session(session_id)
            if session:
                profile = session.get("config_profile", "default")
                if profile and profile != "default":
                    lines.append(f"export HANDOFF_PROFILE={shlex.quote(profile)}\n")
            with open(env_file, "w") as f:
                f.writelines(lines)
            _log(f"env_file AFTER write: wrote HANDOFF_SESSION_ID={session_id}")
        except Exception as e:
            warn(f"failed to persist env vars to env file: {e}")

    # Write per-session cache file as fallback for session_id resolution.
    # CLAUDE_ENV_FILE works on Claude Code ≥2.1.45, but we keep this cache
    # as a fallback for older versions and edge cases. enter_handoff.py can
    # identify the correct session by intersecting ancestor PID chains.
    if session_id:
        try:
            # Use a fixed path, NOT $TMPDIR. Hooks get the system TMPDIR
            # (/var/folders/...) while Bash tool calls get /tmp/claude — they'd
            # write/read different directories. /private/tmp/claude-{uid}/ is in
            # the sandbox write allowlist and works from both contexts.
            sessions_dir = f"/private/tmp/claude-{os.getuid()}/handoff-sessions"
            warn(f"writing session cache to {sessions_dir}")
            os.makedirs(sessions_dir, exist_ok=True)
            # Collect ancestor PIDs (up to 3 levels: us → shell? → Claude Code → ...)
            ancestors = []
            pid = os.getpid()
            for _ in range(3):
                try:
                    ppid = os.getppid() if pid == os.getpid() else int(
                        subprocess.run(
                            ["ps", "-o", "ppid=", "-p", str(pid)],
                            capture_output=True, text=True,
                        ).stdout.strip()
                    )
                    if ppid > 1:
                        ancestors.append(ppid)
                        pid = ppid
                    else:
                        break
                except Exception:
                    break
            cache_file = os.path.join(sessions_dir, f"{session_id}.json")
            with open(cache_file, "w") as f:
                json.dump({
                    "session_id": session_id,
                    "project_dir": project_dir or "",
                    "session_tool": "Claude Code",
                    "written_at": time.time(),
                    "ancestors": ancestors,
                }, f)
            warn(f"wrote session cache: {cache_file} ancestors={ancestors}")
            # Clean up session files older than 24h to avoid accumulation
            cutoff = time.time() - 86400
            for fname in os.listdir(sessions_dir):
                fpath = os.path.join(sessions_dir, fname)
                try:
                    if os.path.getmtime(fpath) < cutoff:
                        os.unlink(fpath)
                except Exception:
                    pass
        except Exception as e:
            warn(f"failed to write session cache: {e}")

    # Clear hooks-pending marker (proves hooks are loaded in this session)
    try:
        marker = f"/private/tmp/claude-{os.getuid()}/handoff-hooks-pending"
        if os.path.exists(marker):
            os.unlink(marker)
            warn("cleared handoff-hooks-pending marker")
    except Exception as e:
        warn(f"failed to clear hooks-pending marker: {e}")

    # Check if this session has an active handoff
    session = handoff_db.get_session(session_id) if session_id else None
    if not session:
        return

    # Handoff is active for this session — silence terminal
    try:
        subprocess.run(
            [sys.executable, os.path.join(SCRIPT_DIR, "iterm2_silence.py"), "on"],
            timeout=5,
        )
    except Exception as e:
        warn(f"failed to silence terminal notifications: {e}")

    # Output full loop instructions so Claude can resume handoff even after
    # context compaction (where SKILL.md content may have been summarized away).
    chat_id = session.get("chat_id", "")
    print(
        f"[Handoff active] This session has an active Lark handoff "
        f"(chat_id: {chat_id}). Resume the handoff main loop NOW.\n"
        f"\n"
        f"CRITICAL RULES:\n"
        f"- ALL Bash commands calling Python scripts with network access MUST use "
        f"dangerouslyDisableSandbox: true (sandbox blocks Lark API)\n"
        f"- NEVER use AskUserQuestion or EnterPlanMode — the user is on Lark, not CLI\n"
        f"- Send ALL responses to Lark via send_to_group.py\n"
        f"\n"
        f"LOOP STEPS:\n"
        f"1. Wait for Lark message:\n"
        f"   python3 .claude/skills/handoff/scripts/wait_for_reply.py --timeout 0\n"
        f"   (dangerouslyDisableSandbox: true, Bash timeout: 600000)\n"
        f"2. If reply is 'handback' → exit handoff (send goodbye, deactivate, restore notifications)\n"
        f"3. Process the user's request (read files, edit code, run commands, etc.)\n"
        f"4. Send response to Lark:\n"
        f"   python3 .claude/skills/handoff/scripts/send_to_group.py '<response>'\n"
        f"   (dangerouslyDisableSandbox: true)\n"
        f"5. Go to step 1\n"
        f"\n"
        f"For full protocol details, read .claude/skills/handoff/SKILL.md"
    )


if __name__ == "__main__":
    main()
