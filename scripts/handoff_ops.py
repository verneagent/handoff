#!/usr/bin/env python3
"""Deterministic handoff operations used by SKILL markdown.

This script replaces inline `python3 -c` snippets with stable, testable
commands that return machine-readable JSON where appropriate.
"""

import argparse
import datetime
import json
import os
import shutil
import sys
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import group_config
import handoff_config
import handoff_db
import handoff_worker
import lark_im
from send_to_group import (
    create_handoff_group,
    find_external_groups,
    find_groups_for_workspace,
    get_worktree_name,
)
from worker_http import poll_worker_urllib  # type: ignore


def _jprint(obj):
    print(json.dumps(obj, ensure_ascii=True))


def _get_session_id(args=None):
    sid = os.environ.get("HANDOFF_SESSION_ID", "").strip()
    return sid


def _require_credentials():
    # Resolve profile from active session or environment — never accept an
    # explicit profile so callers can't accidentally skip resolution.
    sid = _get_session_id()
    profile = None
    if sid:
        session = handoff_db.get_session(sid)
        if session:
            profile = session.get("config_profile", "default")
    if profile is None:
        profile = handoff_config.resolve_profile()
    creds = handoff_config.load_credentials(profile=profile)
    if not creds:
        raise RuntimeError("no_credentials")
    return creds


def _require_token(creds):
    return lark_im.get_tenant_token(creds["app_id"], creds["app_secret"])


def _resolve_cmd_profile(args):
    """Resolve profile for a command: --profile arg → session → resolve_profile()."""
    explicit = getattr(args, "profile", None)
    if explicit:
        return handoff_config.resolve_profile(explicit=explicit)
    # Defense-in-depth: if a session is active, read profile from it
    sid = _get_session_id()
    if sid:
        session = handoff_db.get_session(sid)
        if session:
            return session.get("config_profile", "default")
    return handoff_config.resolve_profile()


def _clean_profile_env():
    """Remove HANDOFF_PROFILE from CLAUDE_ENV_FILE on deactivate."""
    import tempfile

    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file or not os.path.exists(env_file):
        return
    try:
        with open(env_file) as f:
            lines = [l for l in f.readlines()
                     if not l.startswith("export HANDOFF_PROFILE=")]
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(env_file))
        try:
            with os.fdopen(fd, "w") as f:
                f.writelines(lines)
            os.rename(tmp, env_file)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        _warn(f"failed to clean HANDOFF_PROFILE from env file: {e}")


def _require_active_chat_id():
    chat_id, _, _ = handoff_config.resolve_chat_id()
    return chat_id


def _fmt_epoch_seconds(value):
    if value is None:
        return ""
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _fmt_epoch_millis(value):
    if value is None:
        return ""
    try:
        ts_ms = int(value)
    except (TypeError, ValueError):
        return ""
    if ts_ms <= 0:
        return ""
    return _fmt_epoch_seconds(ts_ms // 1000)


def _read_last_lines(path, max_lines):
    if max_lines <= 0:
        max_lines = 1
    try:
        with open(path, "r", errors="replace") as f:
            lines = f.readlines()
        return lines[-max_lines:]
    except FileNotFoundError:
        return None


def _parse_iso_ts(line):
    if not line.startswith("["):
        return None
    end = line.find("]")
    if end <= 1:
        return None
    ts = line[1:end]
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.datetime.fromisoformat(ts)
    except Exception:
        return None


def _filter_by_since_minutes(lines, since_minutes):
    if since_minutes <= 0:
        return lines
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now_utc - datetime.timedelta(minutes=since_minutes)
    out = []
    for line in lines:
        dt = _parse_iso_ts(line)
        if not dt:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        if dt >= cutoff:
            out.append(line)
    return out


def _count_contains(lines, needle):
    return sum(1 for line in lines if needle in line)


def _render_status_pretty(status_obj):
    lines = []
    workspace = status_obj.get("workspace", "")
    db = status_obj.get("database", "")
    db_exists = status_obj.get("db_exists", False)
    groups = status_obj.get("groups", [])

    lines.append(f"Workspace: {workspace}")
    lines.append(f"Database: {db} ({'exists' if db_exists else 'missing'})")
    lines.append(f"Groups: {len(groups)}")

    if not groups:
        lines.append("- (none)")
        return "\n".join(lines)

    for g in groups:
        name = g.get("name", "(unnamed)")
        chat_id = g.get("chat_id", "")
        active = g.get("active", False)
        is_current = g.get("is_current_session", False)
        sess = g.get("session") or {}

        status = "active" if active else "idle"
        current_suffix = " [current]" if is_current else ""
        lines.append(f"- {name}{current_suffix} ({status})")
        lines.append(f"  chat_id: {chat_id}")

        if active:
            lines.append(f"  session_id: {sess.get('session_id', '')}")
            lines.append(f"  session_tool: {sess.get('session_tool', '')}")
            lines.append(f"  session_model: {sess.get('session_model', '')}")
            activated = sess.get("activated_at_human") or sess.get("activated_at") or ""
            last_checked = (
                sess.get("last_checked_human") or sess.get("last_checked") or ""
            )
            lines.append(f"  activated_at: {activated}")
            lines.append(f"  last_checked: {last_checked}")

    return "\n".join(lines)


def cmd_session_check(args):
    sid = _get_session_id()
    session = handoff_db.get_session(sid) if sid else None
    if session:
        _jprint({"already_active": True, "chat_id": session.get("chat_id", "")})
        return 0
    _jprint({"already_active": False})
    return 0


def cmd_discover(args):
    creds = _require_credentials()
    token = _require_token(creds)
    email = creds.get("email", "")
    open_id = lark_im.lookup_open_id_by_email(token, email) if email else ""
    workspace_id = handoff_config.get_workspace_id()
    groups = find_groups_for_workspace(token, workspace_id, open_id or None)
    handoff_db.prune_stale_sessions()
    sessions = handoff_db.get_active_sessions()
    session_by_chat = {s["chat_id"]: s for s in sessions}
    result = []
    for g in groups:
        chat_id = g.get("chat_id", "")
        sess = session_by_chat.get(chat_id)
        row = {
            "chat_id": chat_id,
            "name": g.get("name", ""),
            "active": bool(sess),
            "last_checked": sess.get("last_checked") if sess else None,
            "last_checked_human": _fmt_epoch_millis(sess.get("last_checked"))
            if sess
            else "",
            "activated_at": sess.get("activated_at") if sess else None,
            "activated_at_human": _fmt_epoch_seconds(sess.get("activated_at"))
            if sess
            else "",
            "session_id": sess.get("session_id") if sess else "",
            "session_tool": sess.get("session_tool") if sess else "",
            "session_model": sess.get("session_model") if sess else "",
        }
        result.append(row)

    # Stable ordering for deterministic prompts and comparisons.
    result.sort(key=lambda x: (x.get("name", ""), x.get("chat_id", "")))
    _jprint({"groups": result, "open_id": open_id, "workspace_id": workspace_id})
    return 0


def cmd_discover_bot(args):
    """Discover external groups (groups without workspace tag)."""
    creds = _require_credentials()
    token = _require_token(creds)
    email = creds.get("email", "")
    open_id = lark_im.lookup_open_id_by_email(token, email) if email else ""
    groups = find_external_groups(token, open_id or None)
    # Get bot info for @-mention matching
    bot_info = lark_im.get_bot_info(token)
    handoff_db.prune_stale_sessions()
    sessions = handoff_db.get_active_sessions()
    session_by_chat = {s["chat_id"]: s for s in sessions}
    result = []
    for g in groups:
        chat_id = g.get("chat_id", "")
        sess = session_by_chat.get(chat_id)
        row = {
            "chat_id": chat_id,
            "name": g.get("name", ""),
            "active": bool(sess),
            "last_checked": sess.get("last_checked") if sess else None,
            "last_checked_human": _fmt_epoch_millis(sess.get("last_checked"))
            if sess
            else "",
            "activated_at": sess.get("activated_at") if sess else None,
            "activated_at_human": _fmt_epoch_seconds(sess.get("activated_at"))
            if sess
            else "",
            "session_id": sess.get("session_id") if sess else "",
            "session_tool": sess.get("session_tool") if sess else "",
            "session_model": sess.get("session_model") if sess else "",
        }
        result.append(row)

    result.sort(key=lambda x: (x.get("name", ""), x.get("chat_id", "")))
    _jprint({
        "groups": result,
        "open_id": open_id,
        "bot_open_id": bot_info.get("open_id", ""),
        "bot_name": bot_info.get("app_name", ""),
    })
    return 0


def cmd_create_group(args):
    creds = _require_credentials()
    token = _require_token(creds)
    email = creds.get("email", "")
    if not email:
        raise RuntimeError("email_not_configured")
    open_id = lark_im.lookup_open_id_by_email(token, email)
    if not open_id:
        raise RuntimeError("open_id_not_found")

    workspace_id = handoff_config.get_workspace_id()
    machine = handoff_config._get_machine_name()
    worktree = get_worktree_name()
    existing_names = json.loads(args.existing_names_json or "[]")
    chat_id = create_handoff_group(
        token,
        open_id,
        worktree,
        machine,
        existing_names,
        workspace_id=workspace_id,
    )
    _jprint({"chat_id": chat_id})
    return 0


def _warn(msg):
    print(f"[handoff] {msg}", file=sys.stderr)


def _update_memory_md():
    """Best-effort update of MEMORY.md with handoff sandbox exception and recovery section.

    Ensures that after auto-compaction (which preserves MEMORY.md in the system
    prompt), the model still knows:
    1. Handoff commands require dangerouslyDisableSandbox: true
    2. The essential handoff loop commands to resume
    """
    try:
        project_dir = handoff_config._require_project_dir()
    except RuntimeError:
        return  # Cannot locate memory file without project dir
    # Compute the memory path the same way Claude Code does:
    # ~/.claude/projects/{dir-with-dashes}/memory/MEMORY.md
    project_name = project_dir.replace("/", "-")
    memory_path = os.path.join(
        os.path.expanduser("~/.claude/projects"),
        project_name,
        "memory",
        "MEMORY.md",
    )
    if not os.path.isfile(memory_path):
        return

    try:
        with open(memory_path, "r") as f:
            content = f.read()
    except Exception:
        return

    changed = False

    # --- Sandbox fix: add handoff exception if sandbox policy exists but no exception ---
    sandbox_marker = "Never set `dangerouslyDisableSandbox: true`"
    if sandbox_marker in content and "handoff exception" not in content.lower():
        exception_line = (
            "- **Handoff exception**: During active handoff, Bash commands that "
            "call curl or Python scripts using network (wait_for_reply.py, "
            "send_to_group.py, lark_im.py, permission_bridge.py) MUST use "
            "`dangerouslyDisableSandbox: true` because the sandbox blocks Lark API "
            "and Cloudflare Worker access."
        )
        content = content.replace(
            sandbox_marker,
            sandbox_marker + "\n" + exception_line,
        )
        changed = True

    # --- Recovery section: append if not present ---
    recovery_header = "## Handoff Loop Recovery"
    if recovery_header not in content:
        recovery_section = (
            "\n\n" + recovery_header + "\n"
            "If handoff is active but the loop was lost (e.g. after context compaction), "
            "resume with these steps:\n"
            "1. Read `.claude/skills/handoff/SKILL.md` for the full protocol\n"
            "2. Wait for next Lark message: "
            "`python3 .claude/skills/handoff/scripts/wait_for_reply.py --timeout 0` "
            "(with `dangerouslyDisableSandbox: true`, timeout 600000)\n"
            "3. Process the reply, then send response: "
            "`python3 .claude/skills/handoff/scripts/send_to_group.py '<message>'` "
            "(with `dangerouslyDisableSandbox: true`)\n"
            "4. NEVER use AskUserQuestion during handoff — send questions to Lark instead\n"
            "5. Go back to step 2\n"
        )
        content += recovery_section
        changed = True

    if changed:
        try:
            with open(memory_path, "w") as f:
                f.write(content)
        except Exception as exc:
            _warn(f"failed to write {memory_path}: {exc}")


def cmd_activate(args):
    sid = _get_session_id()
    if not sid:
        raise RuntimeError("missing_session_id")
    profile = _resolve_cmd_profile(args)
    model = str(args.session_model).strip()
    if "/" in model:
        model = model.split("/", 1)[1]
    # Resolve operator open_id and bot open_id
    operator_open_id = ""
    bot_open_id = ""
    _token = None
    try:
        creds = handoff_config.load_credentials(profile=profile)
        email = creds.get("email", "") if creds else ""
        if email:
            _token = lark_im.get_tenant_token(creds["app_id"], creds["app_secret"])
            operator_open_id = lark_im.lookup_open_id_by_email(_token, email) or ""
            bot_info = lark_im.get_bot_info(_token)
            bot_open_id = bot_info.get("open_id", "")
    except Exception as exc:
        _warn(f"failed to resolve operator/bot open_id: {exc}")
    need_mention = False
    try:
        if _token and bot_open_id:
            from handoff_lifecycle import compute_need_mention
            need_mention = compute_need_mention(_token, args.chat_id, bot_open_id)
    except Exception as exc:
        _warn(f"compute_need_mention failed: {exc}")
    handoff_db.activate_handoff(
        sid, args.chat_id, session_model=model,
        operator_open_id=operator_open_id,
        bot_open_id=bot_open_id,
        need_mention=need_mention,
        config_profile=profile,
    )
    # Best-effort: update MEMORY.md with handoff-specific instructions
    # so they survive auto-compaction
    try:
        _update_memory_md()
    except Exception as exc:
        _warn(f"_update_memory_md failed: {exc}")
    _jprint({"ok": True, "session_id": sid, "chat_id": args.chat_id})
    return 0


def _drain_takeover_signal(worker_url, chat_id, timeout_s, profile):
    if not worker_url:
        return False
    result = poll_worker_urllib(
        worker_url,
        chat_id,
        since="",
        timeout=max(1, int(timeout_s)),
        api_key=handoff_config.load_api_key(profile=profile) or "",
    )
    return bool(result.get("takeover", False))


def cmd_takeover(args):
    sid = _get_session_id()
    if not sid:
        raise RuntimeError("missing_session_id")

    profile = _resolve_cmd_profile(args)
    model = str(args.session_model).strip()
    if "/" in model:
        model = model.split("/", 1)[1]
    # Resolve operator open_id and bot open_id
    operator_open_id = ""
    bot_open_id = ""
    try:
        creds = handoff_config.load_credentials(profile=profile)
        email = creds.get("email", "") if creds else ""
        if email:
            token = lark_im.get_tenant_token(creds["app_id"], creds["app_secret"])
            operator_open_id = lark_im.lookup_open_id_by_email(token, email) or ""
            bot_info = lark_im.get_bot_info(token)
            bot_open_id = bot_info.get("open_id", "")
    except Exception as exc:
        _warn(f"failed to resolve operator/bot open_id: {exc}")
    expected_owner = handoff_db.get_chat_owner_session(args.chat_id)
    ok, owner, replaced_owner = handoff_db.takeover_chat(
        sid,
        args.chat_id,
        model,
        expected_owner_session_id=expected_owner,
        operator_open_id=operator_open_id,
        bot_open_id=bot_open_id,
        config_profile=profile,
    )

    if not ok:
        _jprint(
            {
                "ok": False,
                "won": False,
                "chat_id": args.chat_id,
                "current_owner": owner,
                "expected_owner": expected_owner,
            }
        )
        return 1

    worker_url = handoff_config.load_worker_url(profile=profile)
    if worker_url and replaced_owner and replaced_owner != sid:
        handoff_worker.send_takeover(worker_url, args.chat_id, profile=profile)
    drained = False
    if worker_url and replaced_owner and replaced_owner != sid:
        drained = _drain_takeover_signal(worker_url, args.chat_id, args.drain_timeout, profile=profile)
    _jprint(
        {
            "ok": True,
            "won": True,
            "session_id": sid,
            "chat_id": args.chat_id,
            "takeover_sent": bool(worker_url),
            "drained": drained,
            "expected_owner": expected_owner,
            "replaced_owner": replaced_owner,
        }
    )
    return 0


def cmd_deactivate(args):
    sid = _get_session_id()
    if not sid:
        _jprint({"ok": True, "deactivated": False})
        return 0
    handoff_db.clear_working_message(sid)
    handoff_db.clear_autoapprove_message(sid)
    chat_id = handoff_db.deactivate_handoff(sid)
    _clean_profile_env()
    _jprint({"ok": True, "deactivated": True, "chat_id": chat_id or ""})
    return 0


def cmd_set_filter(args):
    sid = _get_session_id()
    if not sid:
        _jprint({"ok": False, "error": "no active session"})
        return 1
    session = handoff_db.get_session(sid)
    if not session:
        _jprint({"ok": False, "error": "session not found"})
        return 1
    level = args.level
    if level not in handoff_db.MESSAGE_FILTER_LEVELS:
        _jprint({"ok": False, "error": f"invalid level: {level}"})
        return 1
    chat_id = session["chat_id"]
    # Write to Lark pinned card (source of truth)
    try:
        token = _require_token(_require_credentials())
        group_config.set_filter(token, chat_id, level)
    except Exception as e:
        print(f"[handoff] group_config sync failed: {e}", file=sys.stderr)
    # Write to local DB (session-local cache for readers)
    handoff_db.set_message_filter(chat_id, level)
    _jprint({"ok": True, "level": level})
    return 0


def cmd_set_autoapprove(args):
    sid = _get_session_id()
    if not sid:
        _jprint({"ok": False, "error": "no active session"})
        return 1
    session = handoff_db.get_session(sid)
    if not session:
        _jprint({"ok": False, "error": "session not found"})
        return 1
    enabled = args.enabled.lower() in ("on", "true", "1", "yes")
    chat_id = session["chat_id"]
    # Write to Lark pinned card (source of truth)
    try:
        token = _require_token(_require_credentials())
        group_config.set_autoapprove(token, chat_id, enabled)
    except Exception as e:
        print(f"[handoff] group_config sync failed: {e}", file=sys.stderr)
    # Write to local DB (session-local cache for readers)
    handoff_db.set_autoapprove(chat_id, enabled)
    _jprint({"ok": True, "autoapprove": enabled})
    return 0


def cmd_parent_local(args):
    local = handoff_db.lookup_parent_message(args.parent_id)
    if not local:
        _jprint({"found": False})
        return 0
    _jprint(
        {
            "found": True,
            "source": "local",
            "text": local.get("text", ""),
            "title": local.get("title", ""),
        }
    )
    return 0


def cmd_parent_api(args):
    creds = _require_credentials()
    token = _require_token(creds)
    msg = lark_im.get_message(token, args.parent_id)
    text, msg_type = lark_im.extract_message_text(msg)
    _jprint({"source": "api", "msg_type": msg_type, "text": text})
    return 0


def cmd_send_image(args):
    """Upload a local image and send it to the active handoff chat."""
    creds = _require_credentials()
    token = _require_token(creds)
    chat_id = _require_active_chat_id()
    image_key = lark_im.upload_image(token, args.path)
    payload = {
        "receive_id": chat_id,
        "msg_type": "image",
        "content": json.dumps({"image_key": image_key}),
    }
    req = urllib.request.Request(
        f"{lark_im.BASE_URL}/im/v1/messages?receive_id_type=chat_id",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    msg_id = data.get("data", {}).get("message_id", "")
    handoff_db.record_sent_message(msg_id, text=args.path, title="[image]", chat_id=chat_id)
    _jprint({"ok": True, "image_key": image_key, "message_id": msg_id})
    return 0


def cmd_send_file(args):
    """Upload a local file and send it to the active handoff chat."""
    creds = _require_credentials()
    token = _require_token(creds)
    chat_id = _require_active_chat_id()
    file_key = lark_im.upload_file(token, args.path, file_type=args.file_type)
    payload = {
        "receive_id": chat_id,
        "msg_type": "file",
        "content": json.dumps({"file_key": file_key}),
    }
    req = urllib.request.Request(
        f"{lark_im.BASE_URL}/im/v1/messages?receive_id_type=chat_id",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    msg_id = data.get("data", {}).get("message_id", "")
    handoff_db.record_sent_message(msg_id, text=args.path, title="[file]", chat_id=chat_id)
    _jprint({"ok": True, "file_key": file_key, "message_id": msg_id})
    return 0


def cmd_download_image(args):
    creds = _require_credentials()
    token = _require_token(creds)
    path = lark_im.download_image(token, args.image_key, args.message_id)
    _jprint({"path": path})
    return 0


def cmd_download_file(args):
    creds = _require_credentials()
    token = _require_token(creds)
    path = lark_im.download_file(token, args.file_key, args.message_id, args.file_name)
    _jprint({"path": path})
    return 0


def cmd_merge_forward(args):
    creds = _require_credentials()
    token = _require_token(creds)
    items = lark_im.list_merge_forward_messages(token, args.message_id)
    for item in items:
        sender = item.get("sender", {})
        sender_type = sender.get("sender_type", "unknown")
        sender_id = (sender.get("sender_id") or {}).get("open_id", "")
        text, msg_type = lark_im.extract_message_text(item)
        _jprint(
            {
                "sender_type": sender_type,
                "sender_id": sender_id,
                "msg_type": msg_type,
                "text": text,
                "message_id": item.get("message_id", ""),
                "create_time": item.get("create_time", ""),
            }
        )
    return 0


def cmd_list_groups(args):
    creds = _require_credentials()
    token = _require_token(creds)
    email = creds.get("email", "")
    open_id = lark_im.lookup_open_id_by_email(token, email) if email else ""
    chats = lark_im.list_bot_chats(token)
    results = []
    for c in chats:
        cid = c.get("chat_id", "")
        name = c.get("name", "(unnamed)")
        try:
            info = lark_im.get_chat_info(token, cid)
        except Exception:
            continue
        if info.get("owner_id"):
            continue
        desc = info.get("description") or ""
        if args.scope == "all":
            members = []
            try:
                members = lark_im.list_chat_members(token, cid)
            except Exception:
                pass
            humans = [m for m in members if m.get("member_id_type") != "app"]
            human_names = (
                ", ".join(m.get("name", m.get("member_id", "?")) for m in humans)
                or "(none)"
            )
            results.append(
                {
                    "name": name,
                    "chat_id": cid,
                    "description": desc,
                    "members": human_names,
                }
            )
        else:
            is_member = False
            try:
                members = lark_im.list_chat_members(token, cid)
                is_member = any(m.get("member_id") == open_id for m in members)
            except Exception:
                pass
            if is_member or (open_id and f"user:{open_id}" in desc):
                results.append({"name": name, "chat_id": cid, "description": desc})
    _jprint({"scope": args.scope, "groups": results, "total": len(results)})
    return 0


def cmd_status(args):
    workspace_id = handoff_config.get_workspace_id()
    db_path = handoff_db._db_path()
    profile = _resolve_cmd_profile(args)
    creds = handoff_config.load_credentials(profile=profile)
    out = {
        "workspace": workspace_id,
        "database": db_path,
        "db_exists": os.path.exists(db_path),
        "groups": [],
    }
    if not creds:
        if args.format == "pretty":
            print(_render_status_pretty(out))
        else:
            _jprint(out)
        return 0
    token = _require_token(creds)
    open_id = None
    if creds.get("email"):
        try:
            open_id = lark_im.lookup_open_id_by_email(token, creds["email"])
        except Exception:
            open_id = None
    groups = find_groups_for_workspace(token, workspace_id, open_id) if open_id else []
    session_by_chat = {s["chat_id"]: s for s in handoff_db.get_active_sessions()}
    my_sid = _get_session_id()
    my_session = handoff_db.get_session(my_sid) if my_sid else None
    my_chat_id = my_session["chat_id"] if my_session else None
    for g in groups:
        cid = g.get("chat_id", "")
        sess = session_by_chat.get(cid)
        session_out = None
        if sess:
            session_out = dict(sess)
            session_out["activated_at_human"] = _fmt_epoch_seconds(
                session_out.get("activated_at")
            )
            session_out["last_checked_human"] = _fmt_epoch_millis(
                session_out.get("last_checked")
            )
        out["groups"].append(
            {
                "name": g.get("name", "(unnamed)"),
                "chat_id": cid,
                "is_current_session": cid == my_chat_id,
                "active": bool(sess),
                "session": session_out,
            }
        )
    if args.format == "pretty":
        print(_render_status_pretty(out))
    else:
        _jprint(out)
    return 0


def cmd_config_current(args):
    profile = _resolve_cmd_profile(args)
    cfg = handoff_config.config_path(profile)
    _jprint({
        "config_file": cfg,
        "config_exists": os.path.exists(cfg),
        "profile": profile,
    })
    return 0



def _session_tool_model(args):
    chat_id = _require_active_chat_id()
    tool = handoff_db._normalize_session_tool()  # from HANDOFF_SESSION_TOOL env
    model = str(args.session_model).strip()
    if "/" in model:
        model = model.split("/", 1)[1]
    return chat_id, tool, model


def _find_tab_by_name(tabs, name):
    for tab in tabs:
        if tab.get("tab_type") != "url":
            continue
        if tab.get("tab_name") == name and tab.get("tab_id"):
            return tab
    return None


_HANDOFF_TAB_URLS = {"https://github.com/verneagent", "https://example.com"}


def _is_handoff_tab(tab):
    """Check if a URL tab was created by handoff (identified by its URL)."""
    if tab.get("tab_type") != "url":
        return False
    url = (tab.get("tab_content") or {}).get("url", "")
    return url in _HANDOFF_TAB_URLS


def cmd_tabs_start(args):
    creds = _require_credentials()
    token = _require_token(creds)
    chat_id, _tool, model = _session_tool_model(args)
    if not chat_id:
        raise RuntimeError("missing_chat_id")

    tab_url = str(args.tab_url or "https://github.com/verneagent").strip()
    tabs = lark_im.list_chat_tabs(token, chat_id)

    # Clean up stale handoff tabs (identified by URL) that don't match current model
    stale_ids = [
        tab["tab_id"] for tab in tabs
        if _is_handoff_tab(tab)
        and tab.get("tab_name") != model
        and tab.get("tab_id")
    ]
    if stale_ids:
        lark_im.delete_chat_tabs(token, chat_id, stale_ids)
        tabs = lark_im.list_chat_tabs(token, chat_id)

    # Only create a model tab (no tool tab)
    model_tab = _find_tab_by_name(tabs, model)
    if not model_tab:
        lark_im.create_chat_tabs(
            token, chat_id,
            [{"tab_name": model, "tab_type": "url", "tab_content": {"url": tab_url}}],
        )
        tabs = lark_im.list_chat_tabs(token, chat_id)
        model_tab = _find_tab_by_name(tabs, model)
    elif (model_tab.get("tab_content") or {}).get("url") != tab_url:
        lark_im.update_chat_tabs(token, chat_id, [{
            "tab_id": model_tab["tab_id"],
            "tab_name": model,
            "tab_type": "url",
            "tab_content": {"url": tab_url},
        }])
        tabs = lark_im.list_chat_tabs(token, chat_id)
        model_tab = _find_tab_by_name(tabs, model)

    # Order: message tab first, then model tab, then everything else
    message_id = ""
    ordered = []
    for tab in tabs:
        if tab.get("tab_type") == "message":
            message_id = tab.get("tab_id", "")
            break
    if message_id:
        ordered.append(message_id)
    if model_tab and model_tab.get("tab_id") and model_tab["tab_id"] not in ordered:
        ordered.append(model_tab["tab_id"])
    for tab in tabs:
        tid = tab.get("tab_id", "")
        if tid and tid not in ordered:
            ordered.append(tid)

    sorted_tabs = lark_im.sort_chat_tabs(token, chat_id, ordered)
    _jprint(
        {
            "ok": True,
            "chat_id": chat_id,
            "model_tab_id": (model_tab or {}).get("tab_id", ""),
            "tab_url": tab_url,
            "chat_tabs": sorted_tabs,
        }
    )
    return 0


def cmd_tabs_end(args):
    creds = _require_credentials()
    token = _require_token(creds)
    chat_id, _tool, _model = _session_tool_model(args)
    if not chat_id:
        raise RuntimeError("missing_chat_id")

    tabs = lark_im.list_chat_tabs(token, chat_id)
    # Remove all handoff-created tabs (identified by URL)
    remove_ids = [
        tab["tab_id"] for tab in tabs
        if _is_handoff_tab(tab) and tab.get("tab_id")
    ]

    if remove_ids:
        tabs = lark_im.delete_chat_tabs(token, chat_id, remove_ids)

    _jprint(
        {
            "ok": True,
            "chat_id": chat_id,
            "removed_tab_ids": remove_ids,
            "chat_tabs": tabs,
        }
    )
    return 0


def cmd_tabs_add(args):
    """Add a custom URL tab to the chat group."""
    creds = _require_credentials()
    token = _require_token(creds)
    chat_id = _require_active_chat_id()

    tab_name = args.tab_name.strip()
    tab_url = args.tab_url.strip()
    if not tab_name or not tab_url:
        raise RuntimeError("--tab-name and --tab-url are required")

    tabs = lark_im.create_chat_tabs(token, chat_id, [
        {"tab_name": tab_name, "tab_type": "url", "tab_content": {"url": tab_url}}
    ])
    _jprint({"ok": True, "chat_id": chat_id, "chat_tabs": tabs})
    return 0


def cmd_tabs_remove(args):
    """Remove a tab by name from the chat group."""
    creds = _require_credentials()
    token = _require_token(creds)
    chat_id = _require_active_chat_id()

    tab_name = args.tab_name.strip()
    tabs = lark_im.list_chat_tabs(token, chat_id)
    remove_ids = [
        tab["tab_id"]
        for tab in tabs
        if tab.get("tab_name") == tab_name and tab.get("tab_id")
    ]
    if not remove_ids:
        _jprint({"ok": False, "error": f"No tab named '{tab_name}' found"})
        return 1

    tabs = lark_im.delete_chat_tabs(token, chat_id, remove_ids)
    _jprint({"ok": True, "chat_id": chat_id, "removed": remove_ids, "chat_tabs": tabs})
    return 0


def cmd_tabs_list(args):
    """List all tabs in the chat group."""
    creds = _require_credentials()
    token = _require_token(creds)
    chat_id = _require_active_chat_id()

    tabs = lark_im.list_chat_tabs(token, chat_id)
    _jprint({"ok": True, "chat_id": chat_id, "chat_tabs": tabs})
    return 0


def cmd_send_status_card(args):
    """Send a handoff start or handback card with auto-resolved tool/model."""
    import handoff_lifecycle

    chat_id, tool, model = _session_tool_model(args)
    if not chat_id:
        raise RuntimeError("missing_chat_id")

    session_id = os.environ.get("HANDOFF_SESSION_ID", "")
    card_type = args.card_type  # "start" or "end"
    if card_type == "start":
        msg_id = handoff_lifecycle.send_start_card(session_id, model, tool_name=tool)
        title = f"Handoff from {tool} ({model})"
    elif card_type == "end":
        body_text = args.body or ""
        msg_id = handoff_lifecycle.send_end_card(session_id, model, tool_name=tool, body=body_text)
        title = f"Hand back to {tool}"
    else:
        raise RuntimeError(f"unknown card_type: {card_type}")

    _jprint(
        {"ok": True, "chat_id": chat_id, "title": title, "tool": tool, "model": model}
    )
    return 0


def cmd_remove_user(args):
    creds = _require_credentials()
    token = _require_token(creds)
    email = creds.get("email", "")
    if not email:
        raise RuntimeError("email_not_configured")
    open_id = lark_im.lookup_open_id_by_email(token, email)
    if not open_id:
        raise RuntimeError("open_id_not_found")
    lark_im.remove_chat_members(token, args.chat_id, [open_id])
    _jprint({"ok": True, "chat_id": args.chat_id, "open_id": open_id})
    return 0


def cmd_dissolve_chat(args):
    creds = _require_credentials()
    token = _require_token(creds)
    lark_im.dissolve_chat(token, args.chat_id)
    _jprint({"ok": True, "chat_id": args.chat_id})
    return 0


def cmd_cleanup_sessions(args):
    deleted = []
    targets = set(args.chat_id)
    for s in handoff_db.get_active_sessions():
        if s.get("chat_id") in targets:
            handoff_db.unregister_session(s["session_id"])
            deleted.append(s["session_id"])
    _jprint({"ok": True, "removed_sessions": deleted})
    return 0


def cmd_find_empty_groups(args):
    creds = _require_credentials()
    token = _require_token(creds)
    chats = lark_im.list_bot_chats(token)
    empty = []
    for c in chats:
        cid = c.get("chat_id", "")
        name = c.get("name", "(unnamed)")
        try:
            members = lark_im.list_chat_members(token, cid)
        except Exception:
            continue
        humans = [m for m in members if m.get("member_id_type") != "app"]
        if not humans:
            empty.append({"name": name, "chat_id": cid})
    _jprint({"groups": empty, "total": len(empty)})
    return 0


def cmd_clear_project(args):
    workspace_id = handoff_config.get_workspace_id()
    tag = f"workspace:{workspace_id}"
    dissolved = []
    dissolve_errors = []

    try:
        creds = _require_credentials()
        token = _require_token(creds)
        groups = find_groups_for_workspace(token, workspace_id)
        for g in groups:
            cid = g.get("chat_id", "")
            if not cid:
                continue
            try:
                lark_im.dissolve_chat(token, cid)
                dissolved.append(cid)
            except Exception as e:
                dissolve_errors.append({"chat_id": cid, "error": str(e)})
    except Exception as e:
        dissolve_errors.append({"chat_id": "", "error": f"group_discovery_failed: {e}"})

    sessions = handoff_db.get_active_sessions()
    removed_sessions = []
    for s in sessions:
        sid = s.get("session_id", "")
        if not sid:
            continue
        handoff_db.unregister_session(sid)
        removed_sessions.append(sid)

    project_dir = handoff_config._require_project_dir()
    project_name = project_dir.replace("/", "-")
    project_db_dir = os.path.join(
        os.path.expanduser("~/.handoff/projects"), project_name
    )
    deleted_project_db_dir = False
    if os.path.isdir(project_db_dir):
        shutil.rmtree(project_db_dir)
        deleted_project_db_dir = True

    _jprint(
        {
            "ok": True,
            "workspace_id": workspace_id,
            "workspace_tag": tag,
            "dissolved": dissolved,
            "dissolve_errors": dissolve_errors,
            "removed_sessions": removed_sessions,
            "project_db_dir": project_db_dir,
            "deleted_project_db_dir": deleted_project_db_dir,
        }
    )
    return 0


def cmd_deinit_config(args):
    profile = _resolve_cmd_profile(args)
    cfg = handoff_config.config_path(profile)
    removed = []
    missing = []

    if os.path.exists(cfg):
        os.remove(cfg)
        removed.append(cfg)
    else:
        missing.append(cfg)

    _jprint(
        {
            "ok": True,
            "removed": removed,
            "missing": missing,
            "active_config": cfg,
            "profile": profile,
        }
    )
    return 0


def cmd_send_form_select(args):
    creds = _require_credentials()
    token = _require_token(creds)
    chat_id = _require_active_chat_id()
    options = json.loads(args.options_json)
    checkers = None
    if args.checkers_json:
        checkers = [(c[0], c[1], c[2]) for c in json.loads(args.checkers_json)]
    # Unescape literal \n from bash single-quoted strings
    body = args.body.replace("\\n", "\n")
    card = lark_im.build_form_card(
        title=args.title,
        body=body,
        color=args.color,
        selects=[(args.field_name, args.placeholder, options)],
        checkers=checkers,
        cancel_label=args.cancel_label,
        chat_id=chat_id,
    )
    msg_id = lark_im.send_message(token, chat_id, card)
    handoff_db.record_sent_message(msg_id, text=body, title=args.title, chat_id=chat_id)
    _jprint({"ok": True, "chat_id": chat_id, "message_id": msg_id})
    return 0


def cmd_send_form_input(args):
    creds = _require_credentials()
    token = _require_token(creds)
    chat_id = _require_active_chat_id()
    # Unescape literal \n from bash single-quoted strings
    body = args.body.replace("\\n", "\n")
    card = lark_im.build_form_card(
        title=args.title,
        body=body,
        color=args.color,
        inputs=[(args.field_name, args.placeholder)],
        cancel_label=args.cancel_label,
        chat_id=chat_id,
    )
    msg_id = lark_im.send_message(token, chat_id, card)
    handoff_db.record_sent_message(msg_id, text=body, title=args.title, chat_id=chat_id)
    _jprint({"ok": True, "chat_id": chat_id, "message_id": msg_id})
    return 0


def cmd_send_form(args):
    creds = _require_credentials()
    token = _require_token(creds)
    chat_id = _require_active_chat_id()
    selects = None
    if args.selects_json:
        selects = []
        for s in json.loads(args.selects_json):
            opts = [(o[0], o[1]) for o in s[2]]
            entry = [s[0], s[1], opts]
            if len(s) > 3:
                entry.append(s[3])
            if len(s) > 4:
                entry.append(s[4])
            selects.append(tuple(entry))
    checkers = None
    if args.checkers_json:
        checkers = [(c[0], c[1], c[2]) for c in json.loads(args.checkers_json)]
    inputs = None
    if args.inputs_json:
        inputs = [(i[0], i[1]) for i in json.loads(args.inputs_json)]
    body = (args.body or "").replace("\\n", "\n")
    card = lark_im.build_form_card(
        title=args.title,
        body=body,
        color=args.color,
        selects=selects,
        checkers=checkers,
        inputs=inputs,
        submit_label=args.submit_label,
        cancel_label=args.cancel_label,
        chat_id=chat_id,
    )
    msg_id = lark_im.send_message(token, chat_id, card)
    handoff_db.record_sent_message(msg_id, text=body, title=args.title, chat_id=chat_id)
    _jprint({"ok": True, "chat_id": chat_id, "message_id": msg_id})
    return 0


def cmd_log_check(args):
    base = args.log_dir
    plugin_path = os.path.join(base, "handoff-plugin.log")
    bridge_path = os.path.join(base, "permission-bridge-cc.log")

    plugin_lines = _read_last_lines(plugin_path, args.lines)
    bridge_lines = _read_last_lines(bridge_path, args.lines)

    plugin_scope = "tail"
    bridge_scope = "tail"
    if plugin_lines is not None and args.since_minutes > 0:
        plugin_lines = _filter_by_since_minutes(plugin_lines, args.since_minutes)
        plugin_scope = f"last_{args.since_minutes}m"
    if bridge_lines is not None and args.since_minutes > 0:
        bridge_lines = _filter_by_since_minutes(bridge_lines, args.since_minutes)
        bridge_scope = f"last_{args.since_minutes}m"

    out = {
        "log_dir": base,
        "plugin_log": {
            "path": plugin_path,
            "exists": plugin_lines is not None,
            "scope": plugin_scope,
        },
        "permission_bridge_log": {
            "path": bridge_path,
            "exists": bridge_lines is not None,
            "scope": bridge_scope,
        },
        "issues": [],
        "warnings": [],
        "ok": True,
    }

    if plugin_lines is not None:
        plugin_stats = {
            "session_error": _count_contains(plugin_lines, "event: session.error"),
            "prompt_failed": _count_contains(plugin_lines, "prompt failed"),
            "handoff_not_active_skip": _count_contains(
                plugin_lines, "handoff not active, skipping bridge"
            ),
            "deactivate_state_table_error": _count_contains(
                plugin_lines, "no such table: state"
            ),
        }
        out["plugin_log"]["stats"] = plugin_stats
        if plugin_stats["deactivate_state_table_error"] > 0:
            out["warnings"].append(
                "plugin log contains legacy state-table cleanup errors; these may be old entries"
            )
    else:
        out["warnings"].append("handoff-plugin.log not found")

    if bridge_lines is not None:
        bridge_stats = {
            "http_403": _count_contains(bridge_lines, "HTTP 403"),
            "curl_timeout": _count_contains(bridge_lines, "curl failed (exit 28)"),
            "ack_failed": _count_contains(bridge_lines, "ack failed"),
            "poll_loop_error": _count_contains(bridge_lines, "poll loop error"),
        }
        out["permission_bridge_log"]["stats"] = bridge_stats
        if bridge_stats["http_403"] > 0:
            out["issues"].append(
                "permission bridge polling has HTTP 403 errors (likely worker auth mismatch)"
            )
        if bridge_stats["curl_timeout"] > 0:
            out["warnings"].append("permission bridge saw poll timeouts (curl exit 28)")
    else:
        out["warnings"].append("permission-bridge-cc.log not found")

    out["ok"] = len(out["issues"]) == 0
    _jprint(out)
    return 0


def cmd_diag(args):
    """Diagnose permission bridge: test card action → poll round-trip."""
    from permission_core import build_permission_body, permission_buttons

    chat_id = args.chat_id
    if chat_id and not handoff_config.is_valid_chat_id(chat_id):
        _jprint({"ok": False, "error": f"invalid chat_id format: {chat_id!r}"})
        return 1
    mode = args.mode  # "ws", "http", or "both"
    timeout = args.timeout
    steps = []

    # 1. Credentials & token
    diag_profile = _resolve_cmd_profile(args)
    try:
        creds = handoff_config.load_credentials(profile=diag_profile)
        if not creds:
            raise RuntimeError("no_credentials")
        token = _require_token(creds)
        steps.append({"step": "credentials", "ok": True})
    except Exception as e:
        _jprint(
            {
                "ok": False,
                "steps": [{"step": "credentials", "ok": False, "error": str(e)}],
            }
        )
        return 1

    # 2. Worker connectivity
    worker_url = handoff_config.load_worker_url(profile=diag_profile)
    if not worker_url:
        _jprint(
            {
                "ok": False,
                "steps": steps
                + [{"step": "worker_url", "ok": False, "error": "missing"}],
            }
        )
        return 1
    steps.append({"step": "worker_url", "ok": True, "value": worker_url})

    # 3. Resolve chat_id if not provided
    if not chat_id:
        session_id = _get_session_id()
        session = handoff_db.get_session(session_id) if session_id else None
        if session:
            chat_id = session.get("chat_id", "")
        if not chat_id:
            # Try to discover groups and pick the first one
            workspace_id = handoff_config.get_workspace_id()
            groups = find_groups_for_workspace(token, workspace_id)
            if groups:
                chat_id = groups[0]["chat_id"]
        if not chat_id:
            _jprint(
                {
                    "ok": False,
                    "steps": steps
                    + [
                        {
                            "step": "chat_id",
                            "ok": False,
                            "error": "no chat_id found — provide --chat-id or activate a session",
                        }
                    ],
                }
            )
            return 1
    steps.append({"step": "chat_id", "ok": True, "value": chat_id})

    # 4. Ack all stale replies
    handoff_worker.ack_worker_replies(worker_url, chat_id, "9999999999999", profile=diag_profile)
    steps.append({"step": "ack_stale", "ok": True})

    # 5. Send test card with buttons
    body = build_permission_body(
        "DiagnosticTool",
        "Click a button to test if card actions reach the poll.\n"
        f"Mode: **{mode}** | Timeout: **{timeout}s**",
    )
    card = lark_im.build_card(
        "Permission Bridge Diagnostic",
        body=body,
        color="orange",
        buttons=permission_buttons(),
        chat_id=chat_id,
    )
    try:
        msg_id = lark_im.send_message(token, chat_id, card)
        steps.append({"step": "send_card", "ok": True, "message_id": msg_id})
    except Exception as e:
        _jprint(
            {
                "ok": False,
                "steps": steps + [{"step": "send_card", "ok": False, "error": str(e)}],
            }
        )
        return 1

    # 6. Poll for response
    start = time.time()
    poll_result = None

    if mode in ("ws", "both"):
        # WebSocket poll — run in a thread with timeout so --timeout is respected
        import threading

        ws_result_box = {}

        def _ws_poll():
            try:
                ws_result_box["result"] = handoff_worker.poll_worker_ws(
                    worker_url, chat_id, since="0", profile=diag_profile
                )
            except Exception as exc:
                ws_result_box["error"] = str(exc)

        ws_thread = threading.Thread(target=_ws_poll, daemon=True)
        ws_thread.start()
        ws_thread.join(timeout=timeout)
        elapsed = round(time.time() - start, 2)

        if ws_thread.is_alive():
            poll_result = {
                "step": "poll_ws",
                "ok": False,
                "elapsed_s": elapsed,
                "error": f"timeout after {timeout}s",
            }
        elif "error" in ws_result_box:
            poll_result = {
                "step": "poll_ws",
                "ok": False,
                "elapsed_s": elapsed,
                "error": ws_result_box["error"],
            }
        else:
            result = ws_result_box.get("result", {})
            replies = result.get("replies", [])
            if replies:
                poll_result = {
                    "step": "poll_ws",
                    "ok": True,
                    "elapsed_s": elapsed,
                    "replies": [
                        {"msg_type": r.get("msg_type"), "text": r.get("text")}
                        for r in replies
                    ],
                }
            elif result.get("error"):
                poll_result = {
                    "step": "poll_ws",
                    "ok": False,
                    "elapsed_s": elapsed,
                    "error": result["error"],
                }

    if mode == "http" or (
        mode == "both" and (not poll_result or not poll_result.get("ok"))
    ):
        # HTTP long-poll (loop until timeout)
        http_start = time.time()
        http_deadline = http_start + timeout
        while time.time() < http_deadline:
            result = handoff_worker.poll_worker(worker_url, chat_id, since="0", profile=diag_profile)
            if result.get("error"):
                continue
            replies = result.get("replies", [])
            if replies:
                elapsed = round(time.time() - start, 2)
                poll_result = {
                    "step": "poll_http",
                    "ok": True,
                    "elapsed_s": elapsed,
                    "replies": [
                        {"msg_type": r.get("msg_type"), "text": r.get("text")}
                        for r in replies
                    ],
                }
                break
        if not poll_result or not poll_result.get("ok"):
            elapsed = round(time.time() - start, 2)
            if poll_result:
                poll_result["note"] = "ws failed, http also timed out"
            else:
                poll_result = {
                    "step": "poll_http",
                    "ok": False,
                    "elapsed_s": elapsed,
                    "error": "timeout",
                }

    steps.append(poll_result)

    # Clean up
    handoff_worker.ack_worker_replies(worker_url, chat_id, "9999999999999", profile=diag_profile)

    ok = all(s.get("ok") for s in steps)
    _jprint({"ok": ok, "steps": steps})
    return 0 if ok else 1


def cmd_guest_add(args):
    """Add members to the guest/coowner whitelist.

    --guests-json: JSON array of [{"open_id": "ou_xxx", "name": "Alice"}, ...]
    --role: Role to assign — "guest" (default) or "coowner".
    """
    session_id = os.environ.get("HANDOFF_SESSION_ID", "")
    if not session_id:
        _jprint({"ok": False, "error": "HANDOFF_SESSION_ID not set"})
        return 1
    session = handoff_db.get_session(session_id)
    if not session:
        _jprint({"ok": False, "error": "session not found"})
        return 1
    try:
        new_guests = json.loads(args.guests_json)
    except json.JSONDecodeError as e:
        _jprint({"ok": False, "error": f"invalid JSON: {e}"})
        return 1
    if not new_guests:
        _jprint({"ok": False, "error": "no guests provided"})
        return 1
    role = getattr(args, "role", "guest") or "guest"
    for g in new_guests:
        g["role"] = role
    # Write to Lark pinned card (source of truth)
    chat_id = session["chat_id"]
    try:
        token = _require_token(_require_credentials())
        added, current = group_config.add_guests(token, chat_id, new_guests)
    except Exception as e:
        print(f"[handoff] group_config sync failed: {e}", file=sys.stderr)
        added, current = handoff_db.add_guests(session_id, new_guests)
    else:
        # Sync to local DB for session readers
        handoff_db.set_guests(session_id, current)
    _jprint({
        "ok": True,
        "added": added,
        "current": current,
    })


def cmd_guest_remove(args):
    """Remove members from the whitelist.

    --open-ids-json: JSON array of open_id strings to remove.
    """
    session_id = os.environ.get("HANDOFF_SESSION_ID", "")
    if not session_id:
        _jprint({"ok": False, "error": "HANDOFF_SESSION_ID not set"})
        return 1
    session = handoff_db.get_session(session_id)
    if not session:
        _jprint({"ok": False, "error": "session not found"})
        return 1
    try:
        open_ids = json.loads(args.open_ids_json)
    except json.JSONDecodeError as e:
        _jprint({"ok": False, "error": f"invalid JSON: {e}"})
        return 1
    if not open_ids:
        _jprint({"ok": False, "error": "no open_ids provided"})
        return 1
    # Write to Lark pinned card (source of truth)
    chat_id = session["chat_id"]
    try:
        token = _require_token(_require_credentials())
        removed, current = group_config.remove_guests(token, chat_id, open_ids)
    except Exception as e:
        print(f"[handoff] group_config sync failed: {e}", file=sys.stderr)
        removed, current = handoff_db.remove_guests(session_id, open_ids)
    else:
        # Sync to local DB for session readers
        handoff_db.set_guests(session_id, current)
    _jprint({
        "ok": True,
        "removed": removed,
        "current": current,
    })


def cmd_guest_list(args):
    """List the current guest/coowner whitelist."""
    session_id = os.environ.get("HANDOFF_SESSION_ID", "")
    if not session_id:
        _jprint({"ok": False, "error": "HANDOFF_SESSION_ID not set"})
        return 1
    guests = handoff_db.get_guests(session_id)
    _jprint({
        "ok": True,
        "guests": guests,
        "count": len(guests),
    })


def cmd_set_rules(args):
    """Set group rules (per-group CLAUDE.md equivalent)."""
    sid = _get_session_id()
    if not sid:
        _jprint({"ok": False, "error": "no active session"})
        return 1
    session = handoff_db.get_session(sid)
    if not session:
        _jprint({"ok": False, "error": "session not found"})
        return 1
    chat_id = session["chat_id"]
    rules = args.rules
    try:
        token = _require_token(_require_credentials())
        group_config.set_rules(token, chat_id, rules)
    except Exception as e:
        _jprint({"ok": False, "error": f"failed to save rules: {e}"})
        return 1
    _jprint({"ok": True, "rules": rules})
    return 0


def cmd_get_rules(args):
    """Get group rules."""
    sid = _get_session_id()
    if not sid:
        _jprint({"ok": False, "error": "no active session"})
        return 1
    session = handoff_db.get_session(sid)
    if not session:
        _jprint({"ok": False, "error": "session not found"})
        return 1
    chat_id = session["chat_id"]
    try:
        token = _require_token(_require_credentials())
        rules = group_config.get_rules(token, chat_id)
    except Exception as e:
        _jprint({"ok": False, "error": f"failed to get rules: {e}"})
        return 1
    _jprint({"ok": True, "rules": rules})
    return 0


def cmd_profile_list(args):
    """List all available config profiles."""
    profiles = handoff_config.list_profiles()
    default = handoff_config.get_default_profile() or "default"
    current = handoff_config.resolve_profile()
    _jprint({
        "ok": True,
        "profiles": profiles,
        "default_profile": default,
        "current_profile": current,
    })
    return 0


def cmd_profile_show(args):
    """Show details of a specific profile."""
    profile = _resolve_cmd_profile(args)
    path = handoff_config.config_path(profile)
    exists = os.path.exists(path)
    _jprint({
        "ok": True,
        "profile": profile,
        "config_path": path,
        "config_exists": exists,
    })
    return 0


def cmd_profile_set_default(args):
    """Set the default profile."""
    name = args.name
    handoff_config.validate_profile_name(name)
    handoff_config.set_default_profile(name)
    _jprint({"ok": True, "default_profile": name})
    return 0


def cmd_relay(args):
    """Send a relay message to another handoff chat via the Worker."""
    import urllib.request

    session_id = os.environ.get("HANDOFF_SESSION_ID", "")
    if not session_id:
        _jprint({"ok": False, "error": "no active session"})
        return 1

    session = handoff_db.get_session(session_id)
    if not session:
        _jprint({"ok": False, "error": "session not found"})
        return 1

    profile = session.get("config_profile", "default")
    worker_url = handoff_config.load_worker_url(profile=profile)
    api_key = handoff_config.load_api_key(profile=profile)
    if not worker_url or not api_key:
        _jprint({"ok": False, "error": "worker_url or api_key not configured"})
        return 1

    from_chat_id = session.get("chat_id", "")

    # Resolve from_chat_name from lark_im group info
    from_chat_name = ""
    from_workspace = ""
    try:
        credentials = handoff_config.load_credentials(profile=profile)
        if credentials:
            token = lark_im.get_tenant_token(
                credentials["app_id"], credentials["app_secret"]
            )
            info = lark_im.get_chat_info(token, from_chat_id)
            from_chat_name = info.get("name", "")
        from_workspace = lark_im.get_workspace_id()
    except Exception:
        pass

    # Build relay payload
    payload = json.dumps({
        "to_chat_id": args.target_chat_id,
        "message": args.message,
        "from_chat_id": from_chat_id,
        "from_chat_name": from_chat_name or f"session:{session_id[:8]}",
        "from_workspace": from_workspace,
    }).encode()

    req = urllib.request.Request(
        f"{worker_url}/relay",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "curl/8.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if result.get("ok"):
            # Also send a Lark card to the target group for visibility
            try:
                credentials = handoff_config.load_credentials(profile=profile)
                if credentials:
                    token = lark_im.get_tenant_token(
                        credentials["app_id"], credentials["app_secret"]
                    )
                    source_label = from_chat_name or from_workspace or "another session"
                    card = lark_im.build_card(
                        f"📨 Relay from 💬{source_label}",
                        body=args.message,
                        color="blue",
                    )
                    lark_im.send_message(token, args.target_chat_id, card)
            except Exception:
                pass  # Lark card is best-effort
            _jprint({"ok": True, "to_chat_id": args.target_chat_id})
        else:
            _jprint({"ok": False, "error": result.get("error", "unknown")})
            return 1
    except Exception as e:
        _jprint({"ok": False, "error": str(e)})
        return 1


def _chat_id_type(value):
    """Argparse type that validates chat_id format."""
    if not handoff_config.is_valid_chat_id(value):
        raise argparse.ArgumentTypeError(
            f"invalid chat_id format: {value!r} — "
            "must be 1-128 chars, alphanumeric/dash/underscore/dot/colon/@"
        )
    return value


# ---------------------------------------------------------------------------
# Agent management — macOS launchd only
# ---------------------------------------------------------------------------

_AGENT_PLIST_PREFIX = "com.handoff.agent"
_AGENT_PLIST_DIR = os.path.expanduser("~/Library/LaunchAgents")
_AGENT_LOG_DIR = "/tmp/handoff"


def _agent_slug(name):
    """Convert a human-readable name to a filesystem-safe slug."""
    import re
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "default"


def _discover_agents():
    """Scan plist files and return list of installed agent dicts."""
    import glob
    import plistlib

    agents = []
    pattern = os.path.join(_AGENT_PLIST_DIR, f"{_AGENT_PLIST_PREFIX}*.plist")
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, "rb") as f:
                plist = plistlib.load(f)
        except Exception:
            continue
        label = plist.get("Label", "")
        prog_args = plist.get("ProgramArguments", [])
        # Extract --chat-id, --project-dir, --model, --profile from ProgramArguments
        info = {"label": label, "plist": path}
        for i, arg in enumerate(prog_args):
            if arg == "--chat-id" and i + 1 < len(prog_args):
                info["chat_id"] = prog_args[i + 1]
            elif arg == "--project-dir" and i + 1 < len(prog_args):
                info["project_dir"] = prog_args[i + 1]
            elif arg == "--model" and i + 1 < len(prog_args):
                info["model"] = prog_args[i + 1]
            elif arg == "--profile" and i + 1 < len(prog_args):
                info["profile"] = prog_args[i + 1]
        # Derive name from label
        if label == _AGENT_PLIST_PREFIX:
            info["name"] = "(legacy)"
        elif label.startswith(_AGENT_PLIST_PREFIX + "."):
            info["name"] = label[len(_AGENT_PLIST_PREFIX) + 1:]
        else:
            info["name"] = label
        # Check running status
        import subprocess
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True,
        )
        info["running"] = result.returncode == 0
        # Log paths
        info["log"] = plist.get("StandardOutPath", "")
        info["err_log"] = plist.get("StandardErrorPath", "")
        agents.append(info)
    return agents


def _resolve_agent(name, agents=None):
    """Resolve agent by name. Returns agent dict or None."""
    if agents is None:
        agents = _discover_agents()
    if not agents:
        return None
    if not name:
        return agents[0] if len(agents) == 1 else None
    for a in agents:
        if a["name"] == name or a["label"] == name:
            return a
    # Fuzzy match
    for a in agents:
        if name.lower() in a["name"].lower():
            return a
    return None


def cmd_agent_list(args):
    """List all installed launchd agents."""
    if sys.platform != "darwin":
        _jprint({"error": "Agent management is macOS only"})
        return 1
    agents = _discover_agents()
    _jprint({"agents": agents, "count": len(agents)})


def cmd_agent_install(args):
    """Install a new launchd agent for a given chat group."""
    if sys.platform != "darwin":
        _jprint({"error": "Agent management is macOS only"})
        return 1
    import plistlib

    slug = _agent_slug(args.name)
    label = f"{_AGENT_PLIST_PREFIX}.{slug}"
    plist_path = os.path.join(_AGENT_PLIST_DIR, f"{label}.plist")

    # Check for duplicate
    if os.path.exists(plist_path):
        _jprint({"error": f"Agent '{slug}' already exists", "plist": plist_path})
        return 1

    # Check for duplicate chat_id
    for a in _discover_agents():
        if a.get("chat_id") == args.chat_id:
            _jprint({"error": f"Chat {args.chat_id} already managed by agent '{a['name']}'"})
            return 1

    script_dir = os.path.dirname(os.path.abspath(__file__))
    agent_script = os.path.join(script_dir, "handoff_agent.py")
    python_path = sys.executable
    project_dir = os.path.abspath(args.project_dir or os.getcwd())
    log_path = os.path.join(_AGENT_LOG_DIR, f"handoff-agent-{slug}.log")
    err_path = os.path.join(_AGENT_LOG_DIR, f"handoff-agent-{slug}.err")

    os.makedirs(_AGENT_LOG_DIR, exist_ok=True)
    os.makedirs(_AGENT_PLIST_DIR, exist_ok=True)

    prog_args = [
        python_path, agent_script,
        "--chat-id", args.chat_id,
        "--project-dir", project_dir,
        "--model", args.model,
    ]
    if args.profile:
        prog_args += ["--profile", args.profile]

    # Build environment
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:/opt/homebrew/sbin",
        "HOME": os.path.expanduser("~"),
        "LANG": "en_US.UTF-8",
    }
    # API key
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    # SSL cert
    try:
        import certifi
        env["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        import ssl
        ca = ssl.get_default_verify_paths().cafile
        if ca:
            env["SSL_CERT_FILE"] = ca
    # Proxy (crucial for GFW regions)
    for var in ("http_proxy", "https_proxy", "all_proxy",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        val = os.environ.get(var)
        if val:
            env[var] = val

    plist_data = {
        "Label": label,
        "ProgramArguments": prog_args,
        "WorkingDirectory": project_dir,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "StandardOutPath": log_path,
        "StandardErrorPath": err_path,
        "EnvironmentVariables": env,
    }

    with open(plist_path, "wb") as f:
        plistlib.dump(plist_data, f)

    # Load
    import subprocess
    subprocess.run(["launchctl", "load", plist_path], check=True)

    _jprint({
        "ok": True, "name": slug, "label": label,
        "chat_id": args.chat_id, "project_dir": project_dir,
        "model": args.model, "log": log_path, "plist": plist_path,
    })


def cmd_agent_status(args):
    """Show detailed status for one agent."""
    if sys.platform != "darwin":
        _jprint({"error": "Agent management is macOS only"})
        return 1
    agents = _discover_agents()
    agent = _resolve_agent(getattr(args, "name", None), agents)
    if not agent:
        names = [a["name"] for a in agents]
        _jprint({"error": "Agent not found", "available": names})
        return 1
    # Read recent log
    log_lines = []
    if agent.get("log") and os.path.isfile(agent["log"]):
        try:
            with open(agent["log"]) as f:
                log_lines = f.readlines()[-20:]
        except Exception:
            pass
    agent["recent_log"] = [l.rstrip() for l in log_lines]
    _jprint(agent)


def cmd_agent_stop(args):
    """Stop a running agent."""
    if sys.platform != "darwin":
        _jprint({"error": "Agent management is macOS only"})
        return 1
    agent = _resolve_agent(args.name)
    if not agent:
        _jprint({"error": f"Agent '{args.name}' not found"})
        return 1
    import subprocess
    subprocess.run(
        ["launchctl", "unload", agent["plist"]], capture_output=True)
    _jprint({"ok": True, "name": agent["name"], "stopped": True})


def cmd_agent_start(args):
    """Start a stopped agent."""
    if sys.platform != "darwin":
        _jprint({"error": "Agent management is macOS only"})
        return 1
    agent = _resolve_agent(args.name)
    if not agent:
        _jprint({"error": f"Agent '{args.name}' not found"})
        return 1
    import subprocess
    subprocess.run(
        ["launchctl", "load", agent["plist"]], capture_output=True)
    _jprint({"ok": True, "name": agent["name"], "started": True})


def cmd_agent_uninstall(args):
    """Stop and remove an agent."""
    if sys.platform != "darwin":
        _jprint({"error": "Agent management is macOS only"})
        return 1
    agent = _resolve_agent(args.name)
    if not agent:
        _jprint({"error": f"Agent '{args.name}' not found"})
        return 1
    import subprocess
    subprocess.run(
        ["launchctl", "unload", agent["plist"]], capture_output=True)
    if os.path.isfile(agent["plist"]):
        os.unlink(agent["plist"])
    _jprint({"ok": True, "name": agent["name"], "removed": True})


def cmd_agent_log(args):
    """Print recent log lines for an agent."""
    if sys.platform != "darwin":
        _jprint({"error": "Agent management is macOS only"})
        return 1
    agent = _resolve_agent(getattr(args, "name", None))
    if not agent:
        _jprint({"error": "Agent not found"})
        return 1
    lines = getattr(args, "lines", 50)
    log_path = agent.get("log", "")
    if not log_path or not os.path.isfile(log_path):
        _jprint({"error": "No log file", "path": log_path})
        return 1
    with open(log_path) as f:
        all_lines = f.readlines()
    output = [l.rstrip() for l in all_lines[-lines:]]
    _jprint({"name": agent["name"], "log": log_path, "lines": output})


def cmd_agent_spawn(args):
    """Spawn a temporary agent (no launchd, background process).

    Discovers or creates a group for the workspace, then runs handoff_agent.py
    as a nohup background process. Stops on handback.
    """
    import subprocess

    project_dir = os.path.abspath(args.project_dir or os.getcwd())
    if not os.path.isdir(project_dir):
        _jprint({"error": f"Not a directory: {project_dir}"})
        return 1

    profile = _resolve_cmd_profile(args)
    creds = handoff_config.load_credentials(profile=profile)
    if not creds:
        _jprint({"error": "No credentials configured"})
        return 1

    token = lark_im.get_tenant_token(creds["app_id"], creds["app_secret"])
    email = creds.get("email", "")
    open_id = lark_im.lookup_open_id_by_email(token, email) if email else ""

    chat_id = getattr(args, "chat_id", None)
    group_name_arg = getattr(args, "group_name", None)
    group_name = None

    if chat_id:
        # Explicit chat_id — takeover unconditionally
        try:
            info = lark_im.get_chat_info(token, chat_id)
            group_name = info.get("name", "")
        except Exception:
            pass
    elif group_name_arg:
        # Search by group name — takeover if found
        from send_to_group import find_group_by_name
        matches = find_group_by_name(token, group_name_arg, open_id or None)
        if not matches:
            _jprint({"error": "group_not_found", "group_name": group_name_arg})
            return 1
        if len(matches) > 1:
            _jprint({"error": "multiple_groups_found", "group_name": group_name_arg,
                     "matches": [{"chat_id": m["chat_id"], "name": m["name"]} for m in matches]})
            return 1
        chat_id = matches[0]["chat_id"]
        group_name = matches[0].get("name", "")
    else:
        # Discover workspace groups, pick an idle one or create new
        machine = handoff_config._get_machine_name()
        folder = project_dir.replace("/", "-").strip("-")
        workspace_id = f"{machine}-{folder}"
        groups = find_groups_for_workspace(token, workspace_id, open_id or None)

        active_chat_ids = set()
        try:
            old_env = os.environ.get("HANDOFF_PROJECT_DIR")
            os.environ["HANDOFF_PROJECT_DIR"] = project_dir
            for s in handoff_db.get_active_sessions():
                active_chat_ids.add(s.get("chat_id", ""))
            if old_env:
                os.environ["HANDOFF_PROJECT_DIR"] = old_env
            elif "HANDOFF_PROJECT_DIR" in os.environ:
                del os.environ["HANDOFF_PROJECT_DIR"]
        except Exception:
            pass

        for g in groups:
            if g["chat_id"] not in active_chat_ids:
                chat_id = g["chat_id"]
                group_name = g["name"]
                break

        if not chat_id:
            worktree = os.path.basename(project_dir)
            existing_names = [g["name"] for g in groups]
            chat_id = create_handoff_group(
                token, open_id, worktree, machine, existing_names,
                workspace_id=workspace_id,
            )
            group_name = f"{worktree}@{machine}"

    # Spawn handoff_agent.py as background process
    script_dir = os.path.dirname(os.path.abspath(__file__))
    agent_script = os.path.join(script_dir, "handoff_agent.py")
    log_dir = _AGENT_LOG_DIR
    os.makedirs(log_dir, exist_ok=True)
    slug = _agent_slug(group_name or "temp")
    # Include chat_id suffix to avoid log collisions when multiple groups
    # have the same name (e.g. same project on different machines).
    chat_suffix = chat_id[-8:] if chat_id else ""
    log_path = os.path.join(log_dir, f"handoff-agent-{slug}-{chat_suffix}.log")

    # Find a Python with claude_agent_sdk installed. sys.executable might be
    # Xcode Python (3.9) when called from Agent SDK Bash tool, which lacks
    # the SDK. Try candidates in order: current exe, then common paths.
    import subprocess as _sp
    python_path = None
    candidates = [
        sys.executable,
        "/opt/homebrew/bin/python3",
        "/opt/homebrew/bin/python3.14",
        "/usr/local/bin/python3",
    ]
    for candidate in candidates:
        try:
            r = _sp.run([candidate, "-c", "import claude_agent_sdk"],
                        capture_output=True, timeout=5)
            if r.returncode == 0:
                python_path = candidate
                break
        except Exception:
            continue
    if not python_path:
        _jprint({"error": "No Python with claude_agent_sdk found",
                 "tried": candidates})
        return 1

    cmd = [
        python_path, agent_script,
        "--chat-id", chat_id,
        "--project-dir", project_dir,
        "--model", args.model,
    ]
    if profile != "default":
        cmd += ["--profile", profile]

    with open(log_path, "a") as log_f:
        proc = subprocess.Popen(
            cmd,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    _jprint({
        "ok": True,
        "pid": proc.pid,
        "chat_id": chat_id,
        "group_name": group_name,
        "project_dir": project_dir,
        "model": args.model,
        "log": log_path,
    })


def build_parser():
    p = argparse.ArgumentParser(description="Deterministic handoff operations")
    p.add_argument("--profile", default=None, help="Config profile name")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("session-check")

    s.set_defaults(func=cmd_session_check)

    s = sub.add_parser("discover")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("discover-bot")
    s.set_defaults(func=cmd_discover_bot)

    s = sub.add_parser("create-group")
    s.add_argument("--existing-names-json", default="[]")
    s.set_defaults(func=cmd_create_group)

    s = sub.add_parser("activate")
    s.add_argument("--chat-id", required=True, type=_chat_id_type)
    s.add_argument("--session-model", required=True)
    s.set_defaults(func=cmd_activate)

    s = sub.add_parser("takeover")
    s.add_argument("--chat-id", required=True, type=_chat_id_type)
    s.add_argument("--session-model", required=True)
    s.add_argument("--drain-timeout", type=int, default=3)
    s.set_defaults(func=cmd_takeover)

    s = sub.add_parser("deactivate")
    s.set_defaults(func=cmd_deactivate)

    s = sub.add_parser("set-filter")
    s.add_argument("level", choices=["verbose", "important", "concise"])
    s.set_defaults(func=cmd_set_filter)

    s = sub.add_parser("set-autoapprove")
    s.add_argument("enabled", choices=["on", "off"])
    s.set_defaults(func=cmd_set_autoapprove)

    s = sub.add_parser("parent-local")
    s.add_argument("--parent-id", required=True)
    s.set_defaults(func=cmd_parent_local)

    s = sub.add_parser("parent-api")
    s.add_argument("--parent-id", required=True)
    s.set_defaults(func=cmd_parent_api)

    s = sub.add_parser("send-image")
    s.add_argument("path", help="Local path to image file")
    s.set_defaults(func=cmd_send_image)

    s = sub.add_parser("send-file")
    s.add_argument("path", help="Local path to file")
    s.add_argument("--file-type", default="stream",
                   choices=["opus", "mp4", "pdf", "doc", "xls", "ppt", "stream"])
    s.set_defaults(func=cmd_send_file)

    s = sub.add_parser("download-image")
    s.add_argument("--image-key", required=True)
    s.add_argument("--message-id", required=True)
    s.set_defaults(func=cmd_download_image)

    s = sub.add_parser("download-file")
    s.add_argument("--file-key", required=True)
    s.add_argument("--message-id", required=True)
    s.add_argument("--file-name", required=True)
    s.set_defaults(func=cmd_download_file)

    s = sub.add_parser("merge-forward")
    s.add_argument("--message-id", required=True)
    s.set_defaults(func=cmd_merge_forward)

    s = sub.add_parser("list-groups")
    s.add_argument("--scope", choices=["user", "all"], default="user")
    s.set_defaults(func=cmd_list_groups)

    s = sub.add_parser("status")

    s.add_argument("--format", choices=["pretty", "json"], default="pretty")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("config-current")
    s.set_defaults(func=cmd_config_current)

    s = sub.add_parser("remove-user")
    s.add_argument("--chat-id", required=True, type=_chat_id_type)
    s.set_defaults(func=cmd_remove_user)

    s = sub.add_parser("dissolve-chat")
    s.add_argument("--chat-id", required=True, type=_chat_id_type)
    s.set_defaults(func=cmd_dissolve_chat)

    s = sub.add_parser("cleanup-sessions")
    s.add_argument("--chat-id", action="append", required=True, type=_chat_id_type)
    s.set_defaults(func=cmd_cleanup_sessions)

    s = sub.add_parser("find-empty-groups")
    s.set_defaults(func=cmd_find_empty_groups)

    s = sub.add_parser("clear-project")
    s.set_defaults(func=cmd_clear_project)

    s = sub.add_parser("deinit-config")
    s.set_defaults(func=cmd_deinit_config)

    s = sub.add_parser("send-form-select")
    s.add_argument("--title", required=True)
    s.add_argument("--body", required=True)
    s.add_argument("--field-name", required=True)
    s.add_argument("--placeholder", default="Select...")
    s.add_argument("--options-json", required=True)
    s.add_argument("--color", default="blue")
    s.add_argument("--cancel-label", default=None)
    s.add_argument("--checkers-json", default=None)

    s.set_defaults(func=cmd_send_form_select)

    s = sub.add_parser("send-form-input")
    s.add_argument("--title", required=True)
    s.add_argument("--body", required=True)
    s.add_argument("--field-name", required=True)
    s.add_argument("--placeholder", required=True)
    s.add_argument("--color", default="blue")
    s.add_argument("--cancel-label", default=None)

    s.set_defaults(func=cmd_send_form_input)

    s = sub.add_parser("send-form")
    s.add_argument("--title", required=True)
    s.add_argument("--body", default="")
    s.add_argument("--color", default="blue")
    s.add_argument("--selects-json", default=None)
    s.add_argument("--checkers-json", default=None)
    s.add_argument("--inputs-json", default=None)
    s.add_argument("--submit-label", default="Submit")
    s.add_argument("--cancel-label", default=None)

    s.set_defaults(func=cmd_send_form)

    s = sub.add_parser("tabs-start")

    s.add_argument("--session-model", required=True)
    s.add_argument("--tab-url", default=None)
    s.set_defaults(func=cmd_tabs_start)

    s = sub.add_parser("tabs-end")

    s.add_argument("--session-model", required=True)
    s.set_defaults(func=cmd_tabs_end)

    s = sub.add_parser("tabs-add")
    s.add_argument("--tab-name", required=True)
    s.add_argument("--tab-url", required=True)
    s.set_defaults(func=cmd_tabs_add)

    s = sub.add_parser("tabs-remove")
    s.add_argument("--tab-name", required=True)
    s.set_defaults(func=cmd_tabs_remove)

    s = sub.add_parser("tabs-list")
    s.set_defaults(func=cmd_tabs_list)

    s = sub.add_parser("send-status-card")
    s.add_argument("card_type", choices=["start", "end"])

    s.add_argument("--session-model", required=True)
    s.add_argument("--body", default="")
    s.set_defaults(func=cmd_send_status_card)

    s = sub.add_parser("log-check")
    s.add_argument("--log-dir", default=handoff_config.handoff_tmp_dir())
    s.add_argument("--lines", type=int, default=2000)
    s.add_argument("--since-minutes", type=int, default=0)
    s.set_defaults(func=cmd_log_check)

    s = sub.add_parser("diag")
    s.add_argument("--chat-id", default="")
    s.add_argument("--mode", choices=["ws", "http", "both"], default="ws")
    s.add_argument("--timeout", type=int, default=60)
    s.set_defaults(func=cmd_diag)

    s = sub.add_parser("guest-add")
    s.add_argument("--guests-json", required=True)
    s.add_argument("--role", choices=["guest", "coowner"], default="guest")
    s.set_defaults(func=cmd_guest_add)

    s = sub.add_parser("guest-remove")
    s.add_argument("--open-ids-json", required=True)
    s.set_defaults(func=cmd_guest_remove)

    s = sub.add_parser("guest-list")
    s.set_defaults(func=cmd_guest_list)

    s = sub.add_parser("set-rules")
    s.add_argument("--rules", required=True, help="Group rules text")
    s.set_defaults(func=cmd_set_rules)

    s = sub.add_parser("get-rules")
    s.set_defaults(func=cmd_get_rules)

    s = sub.add_parser("profile-list")
    s.set_defaults(func=cmd_profile_list)

    s = sub.add_parser("profile-show")
    s.set_defaults(func=cmd_profile_show)

    s = sub.add_parser("profile-set-default")
    s.add_argument("name", help="Profile name to set as default")
    s.set_defaults(func=cmd_profile_set_default)

    s = sub.add_parser("relay")
    s.add_argument("--target-chat-id", required=True, type=_chat_id_type)
    s.add_argument("--message", required=True)
    s.set_defaults(func=cmd_relay)

    # Agent management
    s = sub.add_parser("agent-list")
    s.set_defaults(func=cmd_agent_list)

    s = sub.add_parser("agent-install")
    s.add_argument("--chat-id", required=True, type=_chat_id_type)
    s.add_argument("--name", required=True, help="Agent slug name")
    s.add_argument("--project-dir", default=None)
    s.add_argument("--model", default="claude-opus-4-6")
    s.set_defaults(func=cmd_agent_install)

    s = sub.add_parser("agent-status")
    s.add_argument("--name", default=None)
    s.set_defaults(func=cmd_agent_status)

    s = sub.add_parser("agent-stop")
    s.add_argument("--name", required=True)
    s.set_defaults(func=cmd_agent_stop)

    s = sub.add_parser("agent-start")
    s.add_argument("--name", required=True)
    s.set_defaults(func=cmd_agent_start)

    s = sub.add_parser("agent-uninstall")
    s.add_argument("--name", required=True)
    s.set_defaults(func=cmd_agent_uninstall)

    s = sub.add_parser("agent-log")
    s.add_argument("--name", default=None)
    s.add_argument("--lines", type=int, default=50)
    s.set_defaults(func=cmd_agent_log)

    s = sub.add_parser("agent-spawn")
    s.add_argument("--project-dir", default=None)
    s.add_argument("-c", "--chat-id", default=None, help="Target group by ID (takeover if active)")
    s.add_argument("-g", "--group-name", default=None, help="Target group by name (takeover if active)")
    s.add_argument("--model", default="claude-opus-4-6")
    s.set_defaults(func=cmd_agent_spawn)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args) or 0)
    except Exception as e:
        _jprint({"error": str(e), "cmd": args.cmd})
        return 1


if __name__ == "__main__":
    sys.exit(main())
