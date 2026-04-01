#!/usr/bin/env python3
"""Single-shot entry point for entering handoff mode.

Runs Steps A→B→C(auto)→D and returns one of:
  {"status": "ready",          "chat_id": "...", "session_id": "...", "project_dir": "..."}
  {"status": "already_active", "chat_id": "...", "session_id": "..."}
  {"status": "choose",         "groups": [...],  "reason": "all_occupied" | "multiple_inactive"}
  {"status": "restart_required", "missing": [...]}

"ready" means activate completed — caller should run start_and_wait.py.
"choose" means Claude must ask the user which group to use, then call
  handoff_ops.py activate --chat-id <...> --session-model <...>
  followed by start_and_wait.py.
"already_active" means this session already has a live handoff.
"restart_required" means session ID could not be resolved; user must restart.

Env var resolution (in order):
  HANDOFF_PROJECT_DIR  — env var, then CLAUDE_PROJECT_DIR, then cwd
  HANDOFF_SESSION_TOOL — env var, then always "Claude Code"
  HANDOFF_SESSION_ID   — env var, then ~/.handoff/sessions/<id>.json matched by ancestor PIDs
"""

import argparse
import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import handoff_config
import handoff_db
import lark_im
from send_to_group import (
    create_handoff_group,
    find_group_by_name,
    find_groups_for_workspace,
    get_worktree_name,
)


def _jprint(obj):
    print(json.dumps(obj, ensure_ascii=True))


_RESOLVE_LOG = os.path.join(
    os.environ.get("HANDOFF_TMP_DIR") or "/tmp/handoff",
    "enter-handoff.log",
)


def _log_resolve(msg):
    """Write to persistent log file for debugging session_id resolution."""
    try:
        os.makedirs(os.path.dirname(_RESOLVE_LOG), exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(_RESOLVE_LOG, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _resolve_env():
    """Resolve required env vars, using fallbacks for each.

    Fallback chain:
      HANDOFF_SESSION_TOOL — always "Claude Code" (enter_handoff is Claude Code only)
      HANDOFF_PROJECT_DIR  — CLAUDE_PROJECT_DIR env var, then os.getcwd()
      HANDOFF_SESSION_ID   — ~/.handoff/sessions/<id>.json matched by ancestor PIDs.
                             Both the hook and this script share Claude Code as an ancestor.
                             The hook stores its ancestor chain; we compute ours and intersect.
                             A non-empty intersection means same Claude Code session.
    Returns error dict {"status": "restart_required", "missing": [...]} if session_id
    cannot be resolved.
    """
    # HANDOFF_SESSION_TOOL — always "Claude Code" for this script
    if not os.environ.get("HANDOFF_SESSION_TOOL"):
        os.environ["HANDOFF_SESSION_TOOL"] = "Claude Code"

    # HANDOFF_PROJECT_DIR — fall back to CLAUDE_PROJECT_DIR then cwd
    if not os.environ.get("HANDOFF_PROJECT_DIR"):
        fallback = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        os.environ["HANDOFF_PROJECT_DIR"] = fallback

    # HANDOFF_SESSION_ID — must come from CLAUDE_ENV_FILE (set by on_session_start hook).
    # The ancestor PID fallback was removed because it is unreliable: after context
    # compaction Claude Code may assign a new session_id without re-firing SessionStart,
    # causing the PID-based lookup to match the wrong (stale) cache entry.
    sid = os.environ.get("HANDOFF_SESSION_ID", "")
    if sid:
        _log_resolve(f"HANDOFF_SESSION_ID from env: {sid}")
    else:
        _log_resolve("HANDOFF_SESSION_ID NOT in env — CLAUDE_ENV_FILE may not have been sourced")

    missing = [v for v in ("HANDOFF_PROJECT_DIR", "HANDOFF_SESSION_ID", "HANDOFF_SESSION_TOOL")
               if not os.environ.get(v)]
    if missing:
        return {"status": "restart_required", "missing": missing}
    return None


def _persist_profile_env(profile):
    """Persist HANDOFF_PROFILE to CLAUDE_ENV_FILE if non-default.

    Uses atomic write (tempfile + rename) so concurrent readers never see
    a partial file.
    """
    import shlex
    import tempfile

    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file:
        return
    try:
        lines = []
        if os.path.exists(env_file):
            with open(env_file) as f:
                lines = [l for l in f.readlines()
                         if not l.startswith("export HANDOFF_PROFILE=")]
        if profile and profile != "default":
            lines.append(f"export HANDOFF_PROFILE={shlex.quote(profile)}\n")
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
        _log_resolve(f"failed to persist HANDOFF_PROFILE: {e}")


def _pick_inactive(groups):
    """Return the most recently active inactive group, or first if no timestamps."""
    inactive = [g for g in groups if not g.get("active")]
    if not inactive:
        return None
    # Prefer most recent last_checked, fall back to activated_at, then first
    def sort_key(g):
        lc = g.get("last_checked") or 0
        aa = g.get("activated_at") or 0
        return (lc, aa)
    return max(inactive, key=sort_key)


def main():
    p = argparse.ArgumentParser(description="Enter handoff mode (Steps A-D)")
    p.add_argument("--session-model", required=True, help="Model name for status card")
    p.add_argument(
        "--mode",
        choices=["default", "no-ask", "new"],
        default="default",
        help="Group selection mode",
    )
    p.add_argument(
        "--group-name",
        default=None,
        help="Join a specific group by name (auto-detects need_mention)",
    )
    p.add_argument(
        "--profile",
        default=None,
        help="Config profile name (default: resolve from env/file/default)",
    )
    args = p.parse_args()

    # Resolve profile early — used for all credential loading and DB storage
    profile = handoff_config.resolve_profile(explicit=args.profile)

    # Verify required env vars are present (proves SessionStart hook ran)
    marker = f"/private/tmp/claude-{os.getuid()}/handoff-hooks-pending"
    err = _resolve_env()
    if err:
        # Env vars missing — differentiate between "hooks just installed" and
        # "hooks installed but not loading" using the pending marker.
        if os.path.exists(marker):
            _jprint({
                "status": "hooks_pending",
                "message": "Hooks were just installed. Exit and restart Claude Code to activate them, then run /handoff.",
            })
        else:
            _jprint(err)
        return 1
    # Env vars present — SessionStart hook ran. Clear stale marker if present.
    if os.path.exists(marker):
        try:
            os.unlink(marker)
        except OSError:
            pass
    project_dir = os.environ["HANDOFF_PROJECT_DIR"]
    session_id = os.environ["HANDOFF_SESSION_ID"]

    # ── Step A: session-check ──────────────────────────────────────────────
    session = handoff_db.get_session(session_id)
    if session:
        _jprint({
            "status": "already_active",
            "chat_id": session.get("chat_id", ""),
            "session_id": session_id,
            "project_dir": project_dir,
        })
        return 0

    # ── Credentials (shared by all paths) ────────────────────────────────
    creds = handoff_config.load_credentials(profile=profile)
    if not creds:
        _jprint({"error": "no_credentials"})
        return 1
    token = lark_im.get_tenant_token(creds["app_id"], creds["app_secret"])
    email = creds.get("email", "")
    open_id = lark_im.lookup_open_id_by_email(token, email) if email else ""

    # ── Group-name shortcut ───────────────────────────────────────────────
    # When --group-name is given, look up the group across ALL bot chats
    # and auto-detect need_mention based on group membership.
    if args.group_name:
        match = find_group_by_name(token, args.group_name, open_id or None)
        if not match:
            _jprint({"error": "group_not_found", "group_name": args.group_name})
            return 1
        chat_id_to_activate = match["chat_id"]

        model = str(args.session_model).strip()
        if "/" in model:
            model = model.split("/", 1)[1]
        bot_open_id = ""
        try:
            bot_info = lark_im.get_bot_info(token)
            bot_open_id = bot_info.get("open_id", "")
        except Exception:
            pass

        from handoff_lifecycle import compute_need_mention
        need_mention = compute_need_mention(token, chat_id_to_activate, bot_open_id)

        handoff_db.activate_handoff(
            session_id,
            chat_id_to_activate,
            session_model=model,
            operator_open_id=open_id,
            bot_open_id=bot_open_id,
            need_mention=need_mention,
            config_profile=profile,
        )
        _persist_profile_env(profile)
        _jprint({
            "status": "ready",
            "chat_id": chat_id_to_activate,
            "session_id": session_id,
            "project_dir": project_dir,
        })
        return 0

    # ── Step B: discover ──────────────────────────────────────────────────
    workspace_id = handoff_config.get_workspace_id()
    groups = find_groups_for_workspace(token, workspace_id, open_id or None)
    handoff_db.prune_stale_sessions()
    sessions = handoff_db.get_active_sessions()
    session_by_chat = {s["chat_id"]: s for s in sessions}

    enriched = []
    for g in groups:
        chat_id = g.get("chat_id", "")
        sess = session_by_chat.get(chat_id)
        enriched.append({
            "chat_id": chat_id,
            "name": g.get("name", ""),
            "active": bool(sess),
            "last_checked": sess.get("last_checked") if sess else None,
            "activated_at": sess.get("activated_at") if sess else None,
            "session_tool": sess.get("session_tool") if sess else "",
            "session_model": sess.get("session_model") if sess else "",
        })
    enriched.sort(key=lambda x: (x.get("name", ""), x.get("chat_id", "")))

    # ── Step C: decision tree ─────────────────────────────────────────────
    chat_id_to_activate = None

    if args.mode == "new":
        # Always create a new group
        existing_names = [g["name"] for g in enriched]
        machine = handoff_config._get_machine_name()
        worktree = get_worktree_name()
        chat_id_to_activate = create_handoff_group(
            token, open_id, worktree, machine, existing_names, workspace_id=workspace_id
        )

    elif args.mode == "no-ask":
        best = _pick_inactive(enriched)
        if best:
            chat_id_to_activate = best["chat_id"]
        else:
            existing_names = [g["name"] for g in enriched]
            machine = handoff_config._get_machine_name()
            worktree = get_worktree_name()
            chat_id_to_activate = create_handoff_group(
                token, open_id, worktree, machine, existing_names, workspace_id=workspace_id
            )

    else:  # default
        n = len(enriched)
        if n == 0:
            # Auto-create
            machine = handoff_config._get_machine_name()
            worktree = get_worktree_name()
            chat_id_to_activate = create_handoff_group(
                token, open_id, worktree, machine, [], workspace_id=workspace_id
            )
        else:
            inactive = [g for g in enriched if not g.get("active")]
            occupied = [g for g in enriched if g.get("active")]

            if len(inactive) == 1 and len(occupied) == 0:
                # Exactly one group, inactive — auto-select, no prompt
                chat_id_to_activate = inactive[0]["chat_id"]
            elif len(inactive) >= 1:
                # Multiple inactive: auto-pick most recent (same as no-ask)
                best = _pick_inactive(enriched)
                chat_id_to_activate = best["chat_id"]
            else:
                # All occupied — Claude must ask
                _jprint({
                    "status": "choose",
                    "groups": enriched,
                    "reason": "all_occupied",
                    "session_id": session_id,
                    "project_dir": project_dir,
                })
                return 0

    # ── Step D: activate ─────────────────────────────────────────────────
    model = str(args.session_model).strip()
    if "/" in model:
        model = model.split("/", 1)[1]

    operator_open_id = ""
    bot_open_id = ""
    try:
        operator_open_id = open_id
        bot_info = lark_im.get_bot_info(token)
        bot_open_id = bot_info.get("open_id", "")
    except Exception:
        pass

    from handoff_lifecycle import compute_need_mention
    need_mention = compute_need_mention(token, chat_id_to_activate, bot_open_id)

    handoff_db.activate_handoff(
        session_id,
        chat_id_to_activate,
        session_model=model,
        operator_open_id=operator_open_id,
        bot_open_id=bot_open_id,
        need_mention=need_mention,
        config_profile=profile,
    )
    _persist_profile_env(profile)

    _jprint({
        "status": "ready",
        "chat_id": chat_id_to_activate,
        "session_id": session_id,
        "project_dir": project_dir,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
