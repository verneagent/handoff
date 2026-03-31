#!/usr/bin/env python3
"""PostToolUse / PostToolUseFailure hook: forward tool outputs to Lark.

Sends Edit diffs, Write confirmations, Bash outputs, and tool failures
to the active handoff chat so the user can see what Claude is doing.

Triggered by PostToolUse and PostToolUseFailure hooks for Edit, Write,
and Bash tools only.
"""

import difflib
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import handoff_config
import handoff_db
import lark_im

# Max chars for card body (Lark card content limit is ~4096)
MAX_BODY = 3000

# Bash commands to skip (handoff infrastructure)
SKIP_COMMANDS = (
    "send_to_group.py",
    "send_and_wait.py",
    "start_and_wait.py",
    "end_and_cleanup.py",
    "wait_for_reply.py",
    "handoff_ops.py",
    "on_notification.py",
    "on_post_tool_use.py",
    "on_session_start.py",
    "on_session_end.py",
    "permission_bridge.py",
    "permission_core.py",
    "iterm2_silence.py",
    "preflight.py",
    "lark_im.py",
    "enter_handoff.py",
    "install_hooks.py",
    "run_tests.py",
    "handoff_config.py",
    # Path fragments — catch any command referencing handoff internals
    "skills/handoff/scripts/",
    "skills/handoff/worker/",
    # Env var prefixes used by handoff infrastructure calls
    "HANDOFF_PROJECT_DIR=",
    "HANDOFF_SESSION_ID=",
)


_ANSI_RE = re.compile(
    r"\x1b\[[0-9;]*[A-Za-z]"           # CSI sequences (cursor, SGR, etc.)
    r"|\x1b\][^\x07]*\x07"             # OSC terminated by BEL (hyperlinks, titles)
    r"|\x1b\][^\x1b]*\x1b\\"           # OSC terminated by ST
    r"|\x1b[()][AB012]"                # Character set selection
)

# ANSI color code → Lark <font color> mapping
_ANSI_COLOR_MAP = {
    "31": "red", "91": "red",       # red / bright red
    "32": "green", "92": "green",   # green / bright green
    "33": "orange", "93": "orange", # yellow → orange (closest)
    "34": "blue", "94": "blue",     # blue / bright blue
    "35": "purple", "95": "purple", # magenta → purple
    "36": "blue", "96": "blue",     # cyan → blue
    "2": "grey",                     # dim → grey
}


def _strip_ansi(text):
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


def _render_ansi(text):
    """Simulate terminal cursor movement and convert ANSI colors to Lark markup.

    Handles carriage return, cursor-up, erase-line, and erase-to-end (progress
    bars, spinners) to get the final visible state, then converts color codes
    to <font color="..."> tags.
    Returns (rendered_text, has_colors).
    """
    # Phase 1: Simulate cursor movement to get final terminal state
    lines = []
    cursor_row = 0
    for raw_line in text.split("\n"):
        # Handle \r (carriage return): simulate line overwrite.
        # Common in progress bars: "old text\rnew text" → overwrites from col 0.
        if "\r" in raw_line:
            parts = raw_line.split("\r")
            buf = list(parts[0])
            for part in parts[1:]:
                for i, ch in enumerate(part):
                    if i < len(buf):
                        buf[i] = ch
                    else:
                        buf.append(ch)
            raw_line = "".join(buf)

        # Process cursor-up sequences: \033[nA moves cursor up n rows
        while True:
            m = re.search(r"\x1b\[(\d*)A", raw_line)
            if not m:
                break
            n = int(m.group(1) or "1")
            cursor_row = max(0, cursor_row - n)
            raw_line = raw_line[:m.start()] + raw_line[m.end():]

        # Process erase-to-end-of-line: \033[K or \033[0K
        # Truncates content after the sequence position.
        raw_line = re.sub(r"\x1b\[0?K.*", "", raw_line)

        # Process erase-line: \033[2K clears entire current line
        raw_line = raw_line.replace("\x1b[2K", "")

        # Place line at cursor position
        while len(lines) <= cursor_row:
            lines.append("")
        lines[cursor_row] = raw_line
        cursor_row += 1

    # Phase 2: Convert ANSI colors to Lark <font> tags
    has_colors = False
    rendered = []
    for line in lines:
        if not re.search(r"\x1b\[", line):
            # No CSI sequences — strip any remaining escapes (OSC, etc.)
            rendered.append(_ANSI_RE.sub("", line))
            continue

        has_colors = True
        result = []
        open_tag = False
        open_bold = False
        pos = 0
        for m in re.finditer(r"\x1b\[([0-9;]*)m", line):
            # Append text before this escape
            result.append(line[pos:m.start()])
            pos = m.end()

            codes = m.group(1).split(";") if m.group(1) else ["0"]
            for code in codes:
                if code == "0":
                    # Full reset — close both font and bold
                    if open_tag:
                        result.append("</font>")
                        open_tag = False
                    if open_bold:
                        result.append("**")
                        open_bold = False
                elif code in ("22",):
                    # Normal intensity — close bold only
                    if open_bold:
                        result.append("**")
                        open_bold = False
                elif code in ("39",):
                    # Default foreground — close color only
                    if open_tag:
                        result.append("</font>")
                        open_tag = False
                elif code in _ANSI_COLOR_MAP:
                    if open_tag:
                        result.append("</font>")
                    result.append(f'<font color="{_ANSI_COLOR_MAP[code]}">')
                    open_tag = True
                elif code == "1":
                    if not open_bold:
                        result.append("**")
                        open_bold = True
                # Ignore other codes (underline, blink, etc.)

        # Remaining text after last escape
        result.append(line[pos:])
        # Close any open tags at end of line
        if open_tag:
            result.append("</font>")
        if open_bold:
            result.append("**")
        # Strip any remaining non-color escapes (OSC, CSI non-SGR, etc.)
        rendered.append(_ANSI_RE.sub("", "".join(result)))

    return "\n".join(rendered), has_colors


def warn(msg):
    print(f"[handoff-post-tool] {msg}", file=sys.stderr)


def _relative_path(abs_path, cwd):
    """Make a path relative to cwd for readability."""
    try:
        return os.path.relpath(abs_path, cwd)
    except ValueError:
        return abs_path


# File extension → code block language for syntax highlighting
_EXT_LANG = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "jsx", ".json": "json", ".yaml": "yaml",
    ".yml": "yaml", ".sh": "bash", ".zsh": "bash", ".rb": "ruby",
    ".rs": "rust", ".go": "go", ".java": "java", ".kt": "kotlin",
    ".swift": "swift", ".css": "css", ".scss": "scss", ".html": "html",
    ".xml": "xml", ".sql": "sql", ".md": "markdown", ".toml": "toml",
    ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
}


def _lang_for_file(file_path):
    """Detect code block language from file extension."""
    _, ext = os.path.splitext(file_path)
    return _EXT_LANG.get(ext.lower(), "")


def _truncate(text, limit=MAX_BODY):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (truncated)"


def _format_edit(tool_input, tool_response, cwd):
    """Format Edit as bold header + colored sections + monospace code blocks."""
    file_path = tool_input.get("file_path", "")
    old_string = tool_input.get("old_string", "")
    new_string = tool_input.get("new_string", "")
    rel_path = _relative_path(file_path, cwd)

    old_lines = old_string.splitlines()
    new_lines = new_string.splitlines()

    removed = []
    added = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, old_lines, new_lines
    ).get_opcodes():
        if tag == "equal":
            continue
        if tag in ("delete", "replace"):
            removed.extend(old_lines[i1:i2])
        if tag in ("insert", "replace"):
            added.extend(new_lines[j1:j2])

    if not removed and not added:
        return None, None

    lang = _lang_for_file(file_path)
    parts = [f"**Edit: {rel_path}**"]
    if removed:
        code = "\n".join(removed)
        parts.append(f'<font color="red">Removed:</font>\n```{lang}\n{code}\n```')
    if added:
        code = "\n".join(added)
        parts.append(f'<font color="green">Added:</font>\n```{lang}\n{code}\n```')

    body = _truncate("\n".join(parts))
    return "", body


def _format_write(tool_input, tool_response, cwd):
    """Format Write tool output."""
    file_path = tool_input.get("file_path", "")
    content = tool_input.get("content", "")
    rel_path = _relative_path(file_path, cwd)
    line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

    body = f"**Write: {rel_path}**\nCreated file ({line_count} lines)"
    return "", body


def _format_diff_output(output):
    """Format unified diff output with colored removed/added sections per file."""
    files = []
    current_file = None
    removed = []
    added = []

    def _flush():
        if current_file and (removed or added):
            files.append((current_file, list(removed), list(added)))
        removed.clear()
        added.clear()

    for line in output.splitlines():
        if line.startswith("diff --git"):
            _flush()
            # Extract file path: diff --git a/path b/path
            m = re.search(r" b/(.+)$", line)
            current_file = m.group(1) if m else line
        elif line.startswith("---") or line.startswith("+++"):
            continue  # Skip --- a/file / +++ b/file headers
        elif line.startswith("@@"):
            continue  # Skip hunk headers
        elif line.startswith("index "):
            continue  # Skip index metadata
        elif line.startswith("-"):
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])

    _flush()

    if not files:
        return None

    parts = []
    for filepath, rem, add in files:
        lang = _lang_for_file(filepath)
        parts.append(f"**{filepath}**")
        if rem:
            code = "\n".join(rem)
            parts.append(
                f'<font color="red">Removed:</font>\n```{lang}\n{code}\n```'
            )
        if add:
            code = "\n".join(add)
            parts.append(
                f'<font color="green">Added:</font>\n```{lang}\n{code}\n```'
            )
    return "\n".join(parts)


def _format_bash(tool_input, tool_response, cwd):
    """Format Bash tool output."""
    command = tool_input.get("command", "")
    description = tool_input.get("description", "")

    # Skip handoff infrastructure commands
    for skip in SKIP_COMMANDS:
        if skip in command:
            return None, None

    stdout = tool_response.get("stdout", "")
    stderr = tool_response.get("stderr", "")
    exit_code = tool_response.get("exitCode", 0)

    label = f"$ {description or command}"
    # Truncate long labels
    if len(label) > 80:
        label = label[:77] + "..."

    output = stdout
    if stderr:
        output = f"{stdout}\n{stderr}".strip() if stdout else stderr

    if not output and exit_code == 0:
        return None, None  # No output, no error — skip card

    # Render ANSI: simulate cursor movement + convert colors
    rendered, has_colors = _render_ansi(output)
    # Strip blank lines produced by cursor simulation
    rendered = "\n".join(l for l in rendered.splitlines() if l.strip())

    if exit_code != 0:
        out_block = f"\n```\n{_truncate(rendered)}\n```" if rendered else ""
        body = f"**{label}**{out_block}\nExit code: {exit_code}"
    else:
        # Detect unified diff output and format with colors
        diff_body = None
        clean = _strip_ansi(output)
        if clean.lstrip().startswith("diff --git"):
            diff_body = _format_diff_output(clean)
        if diff_body:
            body = f"**{label}**\n{_truncate(diff_body)}"
        elif has_colors:
            # Use plain text (not code block) to preserve <font> tags
            body = f"**{label}**\n{_truncate(rendered)}"
        else:
            body = f"**{label}**\n```\n{_truncate(rendered)}\n```"

    return "", body


def _format_team_create(tool_input, tool_response, cwd):
    """Format TeamCreate event — team spawned."""
    team_name = tool_response.get("teamName", "")
    members = tool_response.get("members", [])
    if not members:
        return None, None

    member_lines = []
    for m in members:
        name = m.get("name", "?")
        agent_type = m.get("agentType", "")
        member_lines.append(f"  • **{name}**" + (f" ({agent_type})" if agent_type else ""))

    body = f"**Team created: {team_name}**\n" + "\n".join(member_lines)
    return "", body


def _format_send_message(tool_input, tool_response, cwd):
    """Format SendMessage event — inter-agent communication."""
    msg_type = tool_input.get("type", "message")
    to = tool_input.get("to", "")
    summary = tool_input.get("summary", "")

    if msg_type == "shutdown_request":
        body = f"**Shutting down** teammate: {to}"
    elif msg_type == "broadcast":
        body = f"**Broadcast:** {summary}" if summary else "**Broadcast sent**"
    elif msg_type == "message" and to:
        body = f"**→ {to}:** {summary}" if summary else f"**Message to {to}**"
    else:
        return None, None

    return "", _truncate(body, 500)


def _format_team_delete(tool_input, tool_response, cwd):
    """Format TeamDelete event — team dissolved."""
    return "", "**Team dissolved**"


FORMATTERS = {
    "Edit": _format_edit,
    "Write": _format_write,
    "Bash": _format_bash,
    "TeamCreate": _format_team_create,
    "SendMessage": _format_send_message,
    "TeamDelete": _format_team_delete,
}


def _format_failure(tool_name, tool_input, error, cwd):
    """Format a tool failure message."""
    command = tool_input.get("command", "")
    # Skip handoff infrastructure failures too
    for skip in SKIP_COMMANDS:
        if skip in command:
            return None, None, None

    file_path = tool_input.get("file_path", "")
    description = tool_input.get("description", "")

    if tool_name == "Bash":
        label = description or command
        if len(label) > 70:
            label = label[:67] + "..."
        title = f"$ {label}"
    elif file_path:
        rel_path = _relative_path(file_path, cwd)
        title = f"{tool_name}: {rel_path}"
    else:
        title = f"{tool_name} failed"

    error = _strip_ansi(error)
    body = f'<font color="red">Error:</font>\n```\n{_truncate(error)}\n```'
    return title, body, "red"


def _get_token(session):
    """Get tenant token, returning (token, chat_id) or (None, None)."""
    profile = session.get("config_profile", "default")
    credentials = handoff_config.load_credentials(profile=profile)
    if not credentials:
        return None, None
    try:
        token = lark_im.get_tenant_token(
            credentials["app_id"],
            credentials["app_secret"],
        )
        return token, session["chat_id"]
    except Exception as e:
        warn(f"failed to get tenant token: {e}")
        return None, None


def _send_card(session, title, body, color="grey"):
    """Send a formatted card to the handoff chat."""
    token, chat_id = _get_token(session)
    if not token:
        return

    card = lark_im.build_markdown_card(body, title=title, color=color)

    try:
        msg_id = lark_im.send_message(token, chat_id, card)
        try:
            handoff_db.record_sent_message(
                msg_id, text=body, title=title, chat_id=chat_id
            )
        except Exception:
            pass
    except Exception as e:
        warn(f"failed to send tool output to chat {chat_id}: {e}")


def _tool_summary(tool_name, tool_input):
    """Build a brief one-line summary of a tool action."""
    if tool_name == "Bash":
        desc = tool_input.get("description", "")
        cmd = tool_input.get("command", "")
        label = desc or cmd
        if len(label) > 60:
            label = label[:57] + "..."
        return f"`$ {label}`"
    if tool_name in ("Edit", "Write"):
        file_path = tool_input.get("file_path", "")
        name = os.path.basename(file_path) if file_path else "file"
        return f"`{tool_name}: {name}`"
    return f"`{tool_name}`"


_WORKING_TITLES = [
    (0, "Working..."),
    (20, "Working hard..."),
    (40, "Working really hard..."),
    (60, "Working super hard..."),
    (90, "Working incredibly hard..."),
    (120, "Working unreasonably hard..."),
    (180, "Working absurdly hard..."),
    (240, "Working impossibly hard..."),
    (300, "Working ridiculously hard..."),
    (420, "Working cosmically hard..."),
    (600, "Working transcendently hard..."),
    (900, "Working beyond comprehension..."),
]


def _working_title(elapsed_seconds):
    """Return an escalating title based on elapsed time since card creation."""
    title = _WORKING_TITLES[0][1]
    for threshold, t in _WORKING_TITLES:
        if elapsed_seconds >= threshold:
            title = t
    return title


def _send_or_update_working(session_id, session, tool_name, tool_input):
    """Send or update the 'Working...' card for filtered messages.

    Uses a file lock to prevent parallel hooks from each sending a new card.
    Title escalates with elapsed time since card creation. Includes a Stop
    button so the user can interrupt execution from Lark.
    """
    import fcntl
    import time as _time

    token, chat_id = _get_token(session)
    if not token:
        return

    summary = _tool_summary(tool_name, tool_input)

    lock_dir = os.environ.get("HANDOFF_TMP_DIR", "/tmp/handoff")
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, f"working-{session_id}.lock")

    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            existing_msg_id, created_at, _counter = handoff_db.get_working_state(session_id)
            elapsed = int(_time.time()) - created_at if created_at else 0
            title = _working_title(elapsed)
            # Use V1 card format (build_card) instead of V2 (build_working_card /
            # build_markdown_card).  During Lark Card V2 outages (error 230099),
            # send_message falls back to V1, but update_card_message has no such
            # fallback — it fails silently, causing each PostToolUse to create a
            # new card instead of updating the existing one.  V1 cards support
            # title, markdown body, and buttons — everything the working card
            # needs — and send/update work reliably regardless of V2 status.
            # Build approvers list: owner + coowners can stop
            approvers = []
            op_id = session.get("operator_open_id", "")
            if op_id:
                approvers.append(op_id)
            for g in session.get("guests") or []:
                if g.get("role") == "coowner" and g.get("open_id"):
                    approvers.append(g["open_id"])
            card = lark_im.build_card(
                title, body=summary, color="grey",
                buttons=[("Stop", "__stop__", "default")],
                chat_id=chat_id,
                extra_value={"approvers": approvers} if approvers else None,
            )

            # Reuse the existing card if it's still the latest bot message
            # (visible to user). If other messages were sent after it (card
            # scrolled up), retire it to "Done ✓" and create a new one.
            _WORKING_CARD_MAX_AGE = 60
            if existing_msg_id:
                # Check if the working card is still the latest by seeing if
                # any newer messages exist in the DB since card creation
                is_latest = True
                if elapsed > _WORKING_CARD_MAX_AGE:
                    try:
                        latest = handoff_db.get_latest_sent_message(session_id)
                        if latest and latest.get("message_id") != existing_msg_id:
                            is_latest = False
                    except Exception:
                        is_latest = False

                if is_latest:
                    try:
                        lark_im.update_card_message(token, existing_msg_id, card)
                        handoff_db.set_working_message(session_id, existing_msg_id)
                        return
                    except Exception as e:
                        warn(f"failed to update working card: {e}")
                else:
                    # Retire old card to "Done ✓" before creating a new one
                    try:
                        done_card = lark_im.build_card("Done ✓", body="", color="green")
                        lark_im.update_card_message(token, existing_msg_id, done_card)
                    except Exception:
                        pass
                    handoff_db.clear_working_message(session_id)

            try:
                msg_id = lark_im.send_message(token, chat_id, card)
                handoff_db.set_working_message(session_id, msg_id)
                # Clear the THINKING reaction — user can now see work is happening
                try:
                    import wait_for_reply
                    wait_for_reply.clear_ack_reaction()
                except Exception:
                    pass
            except Exception as e:
                warn(f"failed to send working card: {e}")
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception as e:
        warn(f"invalid hook input JSON: {e}")
        return

    session_id = hook_input.get("session_id", "")
    if not session_id:
        return

    # Check for active handoff (resolve_session handles post-compaction session_id changes)
    session = handoff_db.resolve_session(session_id)
    if not session:
        return

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})
    cwd = hook_input.get("cwd", os.getcwd())
    event = hook_input.get("hook_event_name", "PostToolUse")
    is_failure = event == "PostToolUseFailure"

    # Skip handoff infrastructure commands before filter check
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        description = tool_input.get("description", "")
        combined = f"{command} {description}"
        for skip in SKIP_COMMANDS:
            if skip in combined:
                return
        # Skip polling/monitoring commands from the handoff loop.
        # Claude uses various descriptions like "Check for reply",
        # "Check reply after 5 min", "Check latest line", etc.
        desc_lower = description.lower()
        if desc_lower and ("check" in desc_lower or "poll" in desc_lower) and (
            "reply" in desc_lower or "latest" in desc_lower
            or "output" in desc_lower or "status" in desc_lower
            or "log" in desc_lower or "agent" in desc_lower
        ):
            return
        # Skip commands referencing Claude's background task output directory
        # (e.g. "sleep 600 && cat /private/tmp/claude-501/.../tasks/xxx.output")
        if "/tasks/" in command and ".output" in command:
            return

    # Agent Teams events always bypass message filter
    ALWAYS_FORWARD = {"TeamCreate", "SendMessage", "TeamDelete"}

    # Message filter: concise = no forwarding, important = edit/write only
    msg_filter = session.get("message_filter", "concise")
    should_filter = False
    if tool_name not in ALWAYS_FORWARD:
        if msg_filter == "concise":
            should_filter = True
        elif msg_filter == "important" and tool_name == "Bash":
            should_filter = True

    if should_filter:
        # Check local stop flag first — if set, don't overwrite the card
        if not os.path.exists(_stop_flag_path(session_id)):
            _send_or_update_working(session_id, session, tool_name, tool_input)
            _check_stop_signal(session_id, session)
        return

    if is_failure:
        error = hook_input.get("error", "Unknown error")
        if hook_input.get("is_interrupt"):
            return  # User interrupts don't need Lark notification
        result = _format_failure(tool_name, tool_input, error, cwd)
        if result[0] is None:
            return
        title, body, color = result
    else:
        formatter = FORMATTERS.get(tool_name)
        if not formatter:
            return
        tool_response = hook_input.get("tool_response", {})
        try:
            title, body = formatter(tool_input, tool_response, cwd)
        except Exception as e:
            warn(f"format error for {tool_name}: {e}")
            return
        if body is None:
            return
        color = "grey"

    # Don't send new cards if stop flag is set
    if not os.path.exists(_stop_flag_path(session_id)):
        _send_card(session, title, body, color)
        _check_stop_signal(session_id, session)


def _stop_flag_path(session_id):
    """Return the path to the stop flag file for a session."""
    tmp_dir = os.environ.get("HANDOFF_TMP_DIR", "/tmp/handoff")
    return os.path.join(tmp_dir, f"stop-{session_id}.flag")


def _check_stop_signal(session_id, session):
    """Check worker for a __stop__ signal and write a local flag file if found.

    Does a quick non-blocking poll (timeout=0) to the worker. If a __stop__
    reply is waiting, writes a flag file that PreToolUse hooks will check.
    """
    try:
        import handoff_worker

        chat_id = session.get("chat_id", "")
        if not chat_id:
            return

        profile = session.get("config_profile", "default")
        worker_url = handoff_config.load_worker_url(profile=profile)
        if not worker_url:
            return

        api_key = handoff_config.load_api_key(profile=profile)
        if not api_key:
            return

        # Quick non-blocking check via the /stop/ endpoint
        stop_url = f"{worker_url}/stop/chat:{chat_id}"
        import urllib.request
        req = urllib.request.Request(
            stop_url,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        if data.get("stop"):
            flag_path = _stop_flag_path(session_id)
            os.makedirs(os.path.dirname(flag_path), exist_ok=True)
            with open(flag_path, "w") as f:
                f.write("1")
            # Update the working card to "Stopped"
            token, card_chat_id = _get_token(session)
            if token:
                msg_id = handoff_db.get_working_message(session_id)
                if msg_id:
                    stopped_card = lark_im.build_card(
                        "Stopped", body="Stopped by user.", color="red",
                    )
                    try:
                        lark_im.update_card_message(token, msg_id, stopped_card)
                    except Exception:
                        pass
    except Exception as e:
        warn(f"stop signal check failed: {e}")


if __name__ == "__main__":
    main()
