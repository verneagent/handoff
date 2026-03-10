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
    creds = handoff_config.load_credentials()
    if not creds:
        raise RuntimeError("no_credentials")
    return creds


def _require_token(creds):
    return lark_im.get_tenant_token(creds["app_id"], creds["app_secret"])


def _require_active_chat_id():
    sid = _get_session_id()
    if not sid:
        raise RuntimeError("missing_session_id")
    session = handoff_db.get_session(sid)
    if not session:
        raise RuntimeError("no_active_handoff")
    chat_id = session.get("chat_id", "")
    if not chat_id:
        raise RuntimeError("missing_chat_id")
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
    """Discover external groups for sidecar mode (groups without workspace tag)."""
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
    model = str(args.session_model).strip()
    if "/" in model:
        model = model.split("/", 1)[1]
    # Resolve operator open_id and bot open_id
    operator_open_id = ""
    bot_open_id = ""
    try:
        creds = handoff_config.load_credentials()
        email = creds.get("email", "") if creds else ""
        if email:
            token = lark_im.get_tenant_token(creds["app_id"], creds["app_secret"])
            operator_open_id = lark_im.lookup_open_id_by_email(token, email) or ""
            bot_info = lark_im.get_bot_info(token)
            bot_open_id = bot_info.get("open_id", "")
    except Exception as exc:
        _warn(f"failed to resolve operator/bot open_id: {exc}")
    sidecar_mode = getattr(args, "sidecar_mode", False)
    handoff_db.activate_handoff(
        sid, args.chat_id, session_model=model,
        operator_open_id=operator_open_id,
        bot_open_id=bot_open_id,
        sidecar_mode=sidecar_mode,
    )
    # Best-effort: update MEMORY.md with handoff-specific instructions
    # so they survive auto-compaction
    try:
        _update_memory_md()
    except Exception as exc:
        _warn(f"_update_memory_md failed: {exc}")
    _jprint({"ok": True, "session_id": sid, "chat_id": args.chat_id})
    return 0


def _drain_takeover_signal(worker_url, chat_id, timeout_s):
    if not worker_url:
        return False
    result = poll_worker_urllib(
        worker_url,
        chat_id,
        since="",
        timeout=max(1, int(timeout_s)),
        api_key=handoff_config.load_api_key() or "",
    )
    return bool(result.get("takeover", False))


def cmd_takeover(args):
    sid = _get_session_id()
    if not sid:
        raise RuntimeError("missing_session_id")

    model = str(args.session_model).strip()
    if "/" in model:
        model = model.split("/", 1)[1]
    # Resolve operator open_id and bot open_id
    operator_open_id = ""
    bot_open_id = ""
    try:
        creds = handoff_config.load_credentials()
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

    worker_url = handoff_config.load_worker_url()
    if worker_url and replaced_owner and replaced_owner != sid:
        handoff_worker.send_takeover(worker_url, args.chat_id)
    drained = False
    if worker_url and replaced_owner and replaced_owner != sid:
        drained = _drain_takeover_signal(worker_url, args.chat_id, args.drain_timeout)
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
    chat_id = handoff_db.deactivate_handoff(sid)
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
    handoff_db.set_message_filter(session["chat_id"], level)
    _jprint({"ok": True, "level": level})
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
    creds = handoff_config.load_credentials()
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
    cfg = handoff_config.CONFIG_FILE
    _jprint({"config_file": cfg, "config_exists": os.path.exists(cfg)})
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


def _known_session_tab_names(chat_id):
    """Return set of tab names that belong to handoff sessions (tool + model names)."""
    names = set()
    try:
        conn = handoff_db._get_db()
        rows = conn.execute(
            "SELECT session_tool, session_model FROM sessions WHERE chat_id = ?",
            (chat_id,),
        ).fetchall()
        for row in rows:
            if row[0]:
                names.add(row[0])
            if row[1]:
                names.add(row[1])
    except Exception:
        pass
    return names


def cmd_tabs_start(args):
    creds = _require_credentials()
    token = _require_token(creds)
    chat_id, tool, model = _session_tool_model(args)
    if not chat_id:
        raise RuntimeError("missing_chat_id")

    tab_url = str(args.tab_url or "https://example.com").strip()
    tabs = lark_im.list_chat_tabs(token, chat_id)

    # Clean up stale URL tabs from previous sessions — only remove tabs
    # whose names match known session tool/model names (not user-created tabs)
    known_session_names = _known_session_tab_names(chat_id)
    keep_names = {tool, model}
    stale_ids = [
        tab["tab_id"]
        for tab in tabs
        if tab.get("tab_type") == "url"
        and tab.get("tab_name") in known_session_names
        and tab.get("tab_name") not in keep_names
        and tab.get("tab_id")
    ]
    if stale_ids:
        lark_im.delete_chat_tabs(token, chat_id, stale_ids)
        tabs = lark_im.list_chat_tabs(token, chat_id)

    tool_tab = _find_tab_by_name(tabs, tool)
    model_tab = _find_tab_by_name(tabs, model)

    missing = []
    if not tool_tab:
        missing.append(
            {"tab_name": tool, "tab_type": "url", "tab_content": {"url": tab_url}}
        )
    if not model_tab:
        missing.append(
            {"tab_name": model, "tab_type": "url", "tab_content": {"url": tab_url}}
        )
    if missing:
        lark_im.create_chat_tabs(token, chat_id, missing)
        tabs = lark_im.list_chat_tabs(token, chat_id)
        tool_tab = _find_tab_by_name(tabs, tool)
        model_tab = _find_tab_by_name(tabs, model)

    updates = []
    if tool_tab and tool_tab.get("tab_id") and (tool_tab.get("tab_content") or {}).get("url") != tab_url:
        updates.append(
            {
                "tab_id": tool_tab["tab_id"],
                "tab_name": tool,
                "tab_type": "url",
                "tab_content": {"url": tab_url},
            }
        )
    if model_tab and model_tab.get("tab_id") and (model_tab.get("tab_content") or {}).get("url") != tab_url:
        updates.append(
            {
                "tab_id": model_tab["tab_id"],
                "tab_name": model,
                "tab_type": "url",
                "tab_content": {"url": tab_url},
            }
        )
    if updates:
        lark_im.update_chat_tabs(token, chat_id, updates)
        tabs = lark_im.list_chat_tabs(token, chat_id)
        tool_tab = _find_tab_by_name(tabs, tool)
        model_tab = _find_tab_by_name(tabs, model)

    message_id = ""
    ordered = []
    for tab in tabs:
        if tab.get("tab_type") == "message":
            message_id = tab.get("tab_id", "")
            break
    if message_id:
        ordered.append(message_id)
    for tab in (tool_tab, model_tab):
        if tab and tab.get("tab_id") and tab.get("tab_id") not in ordered:
            ordered.append(tab.get("tab_id"))
    for tab in tabs:
        tid = tab.get("tab_id", "")
        if tid and tid not in ordered:
            ordered.append(tid)

    sorted_tabs = lark_im.sort_chat_tabs(token, chat_id, ordered)
    _jprint(
        {
            "ok": True,
            "chat_id": chat_id,
            "tool_tab_id": (tool_tab or {}).get("tab_id", ""),
            "model_tab_id": (model_tab or {}).get("tab_id", ""),
            "tab_url": tab_url,
            "chat_tabs": sorted_tabs,
        }
    )
    return 0


def cmd_tabs_end(args):
    creds = _require_credentials()
    token = _require_token(creds)
    chat_id, tool, model = _session_tool_model(args)
    if not chat_id:
        raise RuntimeError("missing_chat_id")

    tabs = lark_im.list_chat_tabs(token, chat_id)
    remove_ids = []
    for tab in tabs:
        if tab.get("tab_type") != "url":
            continue
        if tab.get("tab_name") in {tool, model}:
            tid = tab.get("tab_id", "")
            if tid:
                remove_ids.append(tid)

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
    creds = _require_credentials()
    token = _require_token(creds)
    chat_id, tool, model = _session_tool_model(args)
    if not chat_id:
        raise RuntimeError("missing_chat_id")

    card_type = args.card_type  # "start" or "end"
    if card_type == "start":
        title = f"Handoff from {tool} ({model})"
        body = (
            "Handed off to Lark. Reply here to continue working with "
            f"{tool.lower()}. Send **handback** to return to CLI."
        )
        color = "green"
    elif card_type == "end":
        body_text = args.body or "Handing back to CLI."
        title = f"Hand back to {tool}"
        body = body_text
        color = "green"
    else:
        raise RuntimeError(f"unknown card_type: {card_type}")

    card = lark_im.build_card(title, body=body, color=color)
    msg_id = lark_im.send_message(token, chat_id, card)
    try:
        handoff_db.record_sent_message(msg_id, text=body, title=title, chat_id=chat_id)
    except Exception:
        pass

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
    cfg = handoff_config.CONFIG_FILE
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
    try:
        creds = _require_credentials()
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
    worker_url = handoff_config.load_worker_url()
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
    handoff_worker.ack_worker_replies(worker_url, chat_id, "9999999999999")
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
                    worker_url, chat_id, since="0"
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
            result = handoff_worker.poll_worker(worker_url, chat_id, since="0")
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
    handoff_worker.ack_worker_replies(worker_url, chat_id, "9999999999999")

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
    added, current = handoff_db.add_guests(session_id, new_guests)
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
    try:
        open_ids = json.loads(args.open_ids_json)
    except json.JSONDecodeError as e:
        _jprint({"ok": False, "error": f"invalid JSON: {e}"})
        return 1
    if not open_ids:
        _jprint({"ok": False, "error": "no open_ids provided"})
        return 1
    removed, current = handoff_db.remove_guests(session_id, open_ids)
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


def _chat_id_type(value):
    """Argparse type that validates chat_id format."""
    if not handoff_config.is_valid_chat_id(value):
        raise argparse.ArgumentTypeError(
            f"invalid chat_id format: {value!r} — "
            "must be 1-128 chars, alphanumeric/dash/underscore/dot/colon/@"
        )
    return value


def build_parser():
    p = argparse.ArgumentParser(description="Deterministic handoff operations")
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
    s.add_argument("--sidecar", dest="sidecar_mode", action="store_true")
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
    s.add_argument("--tab-url", default="https://example.com")
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
