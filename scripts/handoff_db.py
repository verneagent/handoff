#!/usr/bin/env python3
"""Handoff SQLite database: sessions, messages, guests, working state.

Non-Lark-specific database operations extracted from lark_im.py.
"""

import hashlib
import json
import os
import sqlite3
import time

import handoff_config
from handoff_config import _require_project_dir


# ---------------------------------------------------------------------------
# SQLite database — single file stores handoff state and message history
# ---------------------------------------------------------------------------


def _db_path():
    """Return the path to the handoff SQLite database.

    Uses ~/.handoff/projects/<project>/handoff-data.db where <project>
    is derived from the working directory.
    """
    project_dir = _require_project_dir()
    project_name = project_dir.replace("/", "-")
    return os.path.join(
        os.path.join(handoff_config.HANDOFF_HOME, "projects"),
        project_name,
        "handoff-data.db",
    )


_db_initialized = set()  # tracks which DB files have been schema-checked

_SESSIONS_COLS = {
    "session_id",
    "chat_id",
    "session_tool",
    "session_model",
    "last_checked",
    "activated_at",
    "operator_open_id",
    "bot_open_id",
    "sidecar_mode",
    "guests",
    "config_profile",
}
_CHAT_PREFS_COLS = {
    "chat_id",
    "message_filter",
    "autoapprove",
}
_WORKING_STATE_COLS = {
    "session_id",
    "message_id",
    "created_at",
    "counter",
}
_MESSAGES_COLS = {
    "message_id",
    "chat_id",
    "direction",
    "source_message_id",
    "message_time",
    "text",
    "title",
    "sent_at",
}


def _check_schema(conn, table, expected_cols):
    """Check if a table has the expected columns. Drop and recreate if not.

    Schema has been stable since early 2025. Rather than migrating data from
    ancient schemas, just drop and recreate — sessions are transient and
    message history is non-critical.
    """
    # Safety: table names are hardcoded constants from _get_db(), never user input.
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not info:
        return  # Table doesn't exist yet, will be created by caller
    actual_cols = {row[1] for row in info}
    if expected_cols <= actual_cols:
        return  # All expected columns present
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"DROP TABLE IF EXISTS {table}_new")


def _get_db():
    """Open (and auto-create) the handoff database. Returns a connection."""
    db_path = _db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5)
    if db_path not in _db_initialized:
        conn.execute("PRAGMA journal_mode=WAL")
        # Drop legacy table from pre-sessions era
        conn.execute("DROP TABLE IF EXISTS state")
        # Check existing tables — drop and recreate if schema is outdated
        _check_schema(conn, "sessions", _SESSIONS_COLS)
        _check_schema(conn, "messages", _MESSAGES_COLS)
        _check_schema(conn, "chat_preferences", _CHAT_PREFS_COLS)
        _check_schema(conn, "working_state", _WORKING_STATE_COLS)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "  session_id TEXT NOT NULL PRIMARY KEY,"
            "  chat_id TEXT NOT NULL UNIQUE,"
            "  session_tool TEXT NOT NULL,"
            "  session_model TEXT NOT NULL,"
            "  last_checked INTEGER,"
            "  activated_at INTEGER NOT NULL,"
            "  operator_open_id TEXT NOT NULL DEFAULT '',"
            "  bot_open_id TEXT NOT NULL DEFAULT '',"
            "  sidecar_mode INTEGER NOT NULL DEFAULT 0,"
            "  guests TEXT NOT NULL DEFAULT '[]',"
            "  config_profile TEXT NOT NULL DEFAULT 'default'"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            "  message_id TEXT NOT NULL PRIMARY KEY,"
            "  chat_id TEXT NOT NULL,"
            "  direction TEXT NOT NULL DEFAULT 'sent',"
            "  source_message_id TEXT,"
            "  message_time INTEGER,"
            "  text TEXT,"
            "  title TEXT,"
            "  sent_at INTEGER"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chat_preferences ("
            "  chat_id TEXT NOT NULL PRIMARY KEY,"
            "  message_filter TEXT NOT NULL DEFAULT 'concise',"
            "  autoapprove INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS working_state ("
            "  session_id TEXT NOT NULL PRIMARY KEY,"
            "  message_id TEXT NOT NULL,"
            "  created_at INTEGER NOT NULL,"
            "  counter INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        conn.commit()
        _db_initialized.add(db_path)
    return conn


def _normalize_session_tool():
    """Resolve session tool name from HANDOFF_SESSION_TOOL env var."""
    tool = os.environ.get("HANDOFF_SESSION_TOOL", "").strip()
    if not tool:
        raise RuntimeError(
            "HANDOFF_SESSION_TOOL is not set. "
            "Ensure the SessionStart hook has run to persist it."
        )
    return tool


def try_claim_chat(session_id, chat_id, session_model,
                   operator_open_id="", bot_open_id="", need_mention=False,
                   config_profile="default"):
    """Atomically claim a chat for a session.

    Returns (ok, owner_session_id).
    If ok is False, owner_session_id is the session that currently owns chat_id.
    """
    if not session_id or not chat_id:
        return False, None
    tool = _normalize_session_tool()
    conn = _get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        owner = conn.execute(
            "SELECT session_id FROM sessions WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        if owner and owner[0] != session_id:
            conn.execute("ROLLBACK")
            return False, owner[0]

        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.execute(
            "INSERT INTO sessions"
            " (session_id, chat_id, session_tool, session_model, activated_at,"
            "  operator_open_id, bot_open_id, sidecar_mode, guests, config_profile)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, chat_id, tool, session_model, int(time.time()),
             operator_open_id or "", bot_open_id or "",
             1 if need_mention else 0, "[]", config_profile or "default"),
        )
        conn.execute("COMMIT")
        return True, session_id
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        owner = conn.execute(
            "SELECT session_id FROM sessions WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return False, owner[0] if owner else None
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def register_session(session_id, chat_id, session_model,
                     operator_open_id="", bot_open_id="", need_mention=False,
                     config_profile="default"):
    """Register a handoff session in the local database."""
    ok, owner = try_claim_chat(
        session_id, chat_id,
        session_model=session_model,
        operator_open_id=operator_open_id,
        bot_open_id=bot_open_id,
        need_mention=need_mention,
        config_profile=config_profile,
    )
    if not ok:
        raise RuntimeError(f"chat_id {chat_id} is already owned by session {owner}")


def get_chat_owner_session(chat_id):
    """Return the session_id currently owning chat_id, or None."""
    if not chat_id:
        return None
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT session_id FROM sessions WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def takeover_chat(
    session_id,
    chat_id,
    session_model,
    expected_owner_session_id=None,
    operator_open_id="",
    bot_open_id="",
    need_mention=False,
    config_profile="default",
):
    """Atomically take over a chat for the given session.

    Compare-and-swap semantics:
    - If `expected_owner_session_id` is provided, takeover succeeds only when
      current owner is exactly expected_owner_session_id, or no owner exists
      (old owner ended concurrently).
    - If another owner is present and does not match expected owner, takeover
      fails and returns that owner.

    Returns tuple: (ok, owner_session_id, replaced_owner_session_id)
    """
    if not session_id or not chat_id:
        return False, None, None

    tool = _normalize_session_tool()
    conn = _get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            "SELECT session_id FROM sessions WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        current_owner = row[0] if row else None

        if current_owner and current_owner != session_id:
            # Strict compare-and-swap:
            # - expected owner provided -> must match exactly
            # - expected owner omitted -> only allow if no owner is present
            if expected_owner_session_id:
                if current_owner != expected_owner_session_id:
                    conn.execute("ROLLBACK")
                    return False, current_owner, None
            else:
                conn.execute("ROLLBACK")
                return False, current_owner, None

        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

        replaced_owner = None
        if current_owner and current_owner != session_id:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (current_owner,))
            replaced_owner = current_owner

        conn.execute(
            "INSERT INTO sessions"
            " (session_id, chat_id, session_tool, session_model, activated_at,"
            "  operator_open_id, bot_open_id, sidecar_mode, guests, config_profile)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, chat_id, tool, session_model, int(time.time()),
             operator_open_id or "", bot_open_id or "",
             1 if need_mention else 0, "[]", config_profile or "default"),
        )
        conn.execute("COMMIT")
        return True, session_id, replaced_owner
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        owner = conn.execute(
            "SELECT session_id FROM sessions WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return False, owner[0] if owner else None, None
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


_STALE_THRESHOLD_SECONDS = 30 * 24 * 60 * 60  # 30 days


def prune_stale_sessions():
    """Delete clearly stale session rows (older than 30 days).

    Uses last_checked (ms) when available, otherwise activated_at (s).
    Returns the number of deleted rows.
    """
    now_s = int(time.time())
    cutoff_s = now_s - _STALE_THRESHOLD_SECONDS
    cutoff_ms = cutoff_s * 1000

    conn = _get_db()
    try:
        cur = conn.execute(
            "DELETE FROM sessions WHERE "
            "((last_checked IS NOT NULL AND last_checked != '' "
            "  AND CAST(last_checked AS INTEGER) < ?) "
            " OR ((last_checked IS NULL OR last_checked = '') "
            "  AND COALESCE(activated_at, 0) < ?))",
            (cutoff_ms, cutoff_s),
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def unregister_session(session_id):
    """Remove a handoff session from the local database."""
    conn = _get_db()
    try:
        conn.execute(
            "DELETE FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()


def resolve_session(hook_session_id):
    """Resolve a session by ID, handling post-compaction session_id changes.

    After Claude Code context compaction, the session_id in hook_input may
    differ from the one in the DB (registered at handoff activation). This
    function detects the mismatch and updates the DB atomically.

    Resolution order:
    1. Direct lookup by hook_session_id
    2. If not found, check HANDOFF_SESSION_ID env var (set by SessionStart
       hook at activation time — survives compaction because the env file
       is not rewritten)
    3. If env var differs and that session exists, update the DB record
       to use the new hook_session_id

    Returns dict (same as get_session) or None.
    """
    session = get_session(hook_session_id)
    if session:
        return session

    # Compaction fallback: env var has the original session_id
    env_sid = os.environ.get("HANDOFF_SESSION_ID", "").strip()
    if not env_sid or env_sid == hook_session_id:
        return None

    session = get_session(env_sid)
    if not session:
        return None

    # In agent mode, the SDK has a different session_id than the agent
    # process. Don't adopt it — just return the agent's session as-is.
    if os.environ.get("HANDOFF_SESSION_TOOL") == "Claude Agent SDK":
        return session

    # Found the original session — adopt the new session_id (compaction)
    conn = _get_db()
    try:
        conn.execute(
            "UPDATE sessions SET session_id = ? WHERE session_id = ?",
            (hook_session_id, env_sid),
        )
        conn.commit()
    finally:
        conn.close()

    # Update the env file so future hooks use the new session_id directly
    env_file = os.environ.get("CLAUDE_ENV_FILE", "")
    if env_file and os.path.exists(env_file):
        try:
            import shlex
            import tempfile as _tf
            with open(env_file) as f:
                lines = [
                    l if not l.startswith("export HANDOFF_SESSION_ID=")
                    else f"export HANDOFF_SESSION_ID={shlex.quote(hook_session_id)}\n"
                    for l in f.readlines()
                ]
            dir_name = os.path.dirname(env_file) or "."
            fd, tmp = _tf.mkstemp(dir=dir_name)
            with os.fdopen(fd, "w") as f:
                f.writelines(lines)
            os.rename(tmp, env_file)
        except Exception:
            pass

    # Also update the in-process env var
    os.environ["HANDOFF_SESSION_ID"] = hook_session_id

    # Re-read with the updated session_id
    return get_session(hook_session_id)


def get_session(session_id):
    """Look up a session by ID. Returns dict or None.

    Dict keys: session_id, chat_id, session_tool, session_model,
    last_checked, activated_at, message_filter, operator_open_id, bot_open_id,
    need_mention, guests.
    """
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT s.session_id, s.chat_id, s.last_checked, s.activated_at"
            " , s.session_tool, s.session_model"
            " , p.message_filter"
            " , s.operator_open_id"
            " , s.bot_open_id"
            " , s.sidecar_mode"
            " , s.guests"
            " , p.autoapprove"
            " , s.config_profile"
            " FROM sessions s"
            " LEFT JOIN chat_preferences p ON s.chat_id = p.chat_id"
            " WHERE s.session_id = ?",
            (session_id,),
        ).fetchone()
        if row:
            guests_raw = row[10] or "[]"
            try:
                guests = json.loads(guests_raw)
            except (json.JSONDecodeError, TypeError):
                guests = []
            return {
                "session_id": row[0],
                "chat_id": row[1],
                "last_checked": row[2],
                "activated_at": row[3],
                "session_tool": row[4],
                "session_model": row[5],
                "message_filter": row[6] or "concise",
                "operator_open_id": row[7] or "",
                "bot_open_id": row[8] or "",
                "need_mention": bool(row[9]),
                "guests": guests,
                "autoapprove": bool(row[11]),
                "config_profile": row[12] or "default",
            }
        return None
    finally:
        conn.close()


MESSAGE_FILTER_LEVELS = ("verbose", "important", "concise")


def set_message_filter(chat_id, level):
    """Set the message filter level for a chat group.

    Persists in chat_preferences table (survives session changes).
    level: 'verbose' (all), 'important' (edit+write), 'concise' (none).
    """
    if level not in MESSAGE_FILTER_LEVELS:
        raise ValueError(f"Invalid filter level: {level}")
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO chat_preferences (chat_id, message_filter)"
            " VALUES (?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET message_filter = ?",
            (chat_id, level, level),
        )
        conn.commit()
    finally:
        conn.close()


def set_autoapprove(chat_id, enabled):
    """Enable or disable autoapprove for a chat group.

    Persists in chat_preferences table (survives session changes).
    When enabled, permission requests are auto-approved without asking the user.
    """
    val = 1 if enabled else 0
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO chat_preferences (chat_id, autoapprove)"
            " VALUES (?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET autoapprove = ?",
            (chat_id, val, val),
        )
        conn.commit()
    finally:
        conn.close()


def get_autoapprove(chat_id):
    """Check if autoapprove is enabled for a chat group."""
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT autoapprove FROM chat_preferences WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return bool(row[0]) if row else False
    finally:
        conn.close()


def get_guests(session_id):
    """Get the guest whitelist for a session.

    Returns list of dicts: [{"open_id": "ou_xxx", "name": "Alice"}, ...]
    """
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT guests FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return []
        try:
            return json.loads(row[0] or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
    finally:
        conn.close()


def set_guests(session_id, guests):
    """Replace the guest whitelist for a session.

    guests: list of dicts [{"open_id": "ou_xxx", "name": "Alice"}, ...]
    """
    conn = _get_db()
    try:
        conn.execute(
            "UPDATE sessions SET guests = ? WHERE session_id = ?",
            (json.dumps(guests, ensure_ascii=False), session_id),
        )
        conn.commit()
    finally:
        conn.close()


def add_guests(session_id, new_guests):
    """Add guests to the whitelist (skip duplicates by open_id).

    new_guests: list of dicts [{"open_id": "ou_xxx", "name": "Alice"}, ...]
    Returns (added, current) — lists of added guests and full current list.
    """
    current = get_guests(session_id)
    existing_ids = {g["open_id"] for g in current}
    added = []
    for g in new_guests:
        if g["open_id"] not in existing_ids:
            current.append(g)
            existing_ids.add(g["open_id"])
            added.append(g)
    if added:
        set_guests(session_id, current)
    return added, current


def remove_guests(session_id, open_ids):
    """Remove guests by open_id from the whitelist.

    open_ids: set or list of open_id strings to remove.
    Returns (removed, current) — lists of removed guests and remaining list.
    """
    current = get_guests(session_id)
    ids_to_remove = set(open_ids)
    removed = [g for g in current if g["open_id"] in ids_to_remove]
    remaining = [g for g in current if g["open_id"] not in ids_to_remove]
    if removed:
        set_guests(session_id, remaining)
    return removed, remaining


def get_member_roles(session_id):
    """Get a mapping of open_id → role for all session guests.

    Returns dict like {"ou_xxx": "coowner", "ou_yyy": "guest"}.
    Entries without a "role" field default to "guest".
    """
    guests = get_guests(session_id)
    return {g["open_id"]: g.get("role", "guest") for g in guests}


def set_working_message(session_id, message_id):
    """Store the "Working..." card message_id and increment counter.

    On INSERT, sets created_at to now. On UPDATE, preserves created_at
    so elapsed time is measured from card creation, not last update.
    Returns the new counter value.
    """
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO working_state (session_id, message_id, created_at, counter)"
            " VALUES (?, ?, ?, 1)"
            " ON CONFLICT(session_id) DO UPDATE"
            " SET message_id = ?, counter = counter + 1",
            (session_id, message_id, int(time.time()), message_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT counter FROM working_state WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row[0] if row else 1
    finally:
        conn.close()


def get_working_state(session_id):
    """Return (message_id, created_at, counter) for a session, or (None, 0, 0)."""
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT message_id, created_at, counter FROM working_state WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return (row[0], row[1], row[2]) if row else (None, 0, 0)
    finally:
        conn.close()


def get_working_message(session_id):
    """Return the working card message_id for a session, or None."""
    msg_id, _, _ = get_working_state(session_id)
    return msg_id


def clear_working_message(session_id):
    """Remove the working card state for a session."""
    conn = _get_db()
    try:
        conn.execute(
            "DELETE FROM working_state WHERE session_id = ?",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()


# --- Autoapprove card merging (reuses working_state table with "aa:" prefix) ---


def set_autoapprove_message(session_id, message_id):
    """Store the autoapprove card message_id for a session."""
    key = f"aa:{session_id}"
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO working_state (session_id, message_id, created_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(session_id) DO UPDATE"
            " SET message_id = ?, created_at = ?",
            (key, message_id, int(time.time()), message_id, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def get_autoapprove_message(session_id):
    """Return the autoapprove card message_id for a session, or None."""
    key = f"aa:{session_id}"
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT message_id FROM working_state WHERE session_id = ?",
            (key,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def clear_autoapprove_message(session_id):
    """Remove the autoapprove card state for a session."""
    key = f"aa:{session_id}"
    conn = _get_db()
    try:
        conn.execute(
            "DELETE FROM working_state WHERE session_id = ?",
            (key,),
        )
        conn.commit()
    finally:
        conn.close()


def get_active_sessions():
    """Return all active sessions as a list of dicts."""
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT session_id, chat_id, last_checked, activated_at,"
            " session_tool, session_model, operator_open_id, bot_open_id,"
            " sidecar_mode, config_profile"
            " FROM sessions",
        ).fetchall()
        return [
            {
                "session_id": r[0],
                "chat_id": r[1],
                "last_checked": r[2],
                "activated_at": r[3],
                "session_tool": r[4],
                "session_model": r[5],
                "operator_open_id": r[6] or "",
                "bot_open_id": r[7] or "",
                "need_mention": bool(r[8]),
                "config_profile": r[9] or "default",
            }
            for r in rows
        ]
    finally:
        conn.close()


def set_session_last_checked(session_id, ts):
    """Update the last_checked timestamp for a session.

    Args:
        session_id: The session ID to update.
        ts: Timestamp in milliseconds since epoch (int). Other types are
            converted to int. Invalid values result in NULL.
    """
    ts_value = None
    if ts is not None:
        try:
            if isinstance(ts, int):
                ts_value = ts
            elif isinstance(ts, (str, float)):
                # Use float() first to handle float strings like "123456.78"
                ts_value = int(float(str(ts).strip()))
            else:
                ts_value = None
        except (ValueError, TypeError):
            ts_value = None

    conn = _get_db()
    try:
        conn.execute(
            "UPDATE sessions SET last_checked = ? WHERE session_id = ?",
            (ts_value, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def activate_handoff(session_id, chat_id, session_model, operator_open_id="",
                     bot_open_id="", need_mention=False, config_profile="default"):
    """Activate handoff for a session (local DB only)."""
    register_session(
        session_id, chat_id,
        session_model=session_model,
        operator_open_id=operator_open_id,
        bot_open_id=bot_open_id,
        need_mention=need_mention,
        config_profile=config_profile,
    )


def deactivate_handoff(session_id):
    """Deactivate handoff for a session (local DB only).

    Returns the chat_id that was associated, or None.
    """
    session = get_session(session_id)
    chat_id = session["chat_id"] if session else None
    unregister_session(session_id)
    return chat_id


# ---------------------------------------------------------------------------
# Message tracking — resolves parent_id on replies and reactions
# ---------------------------------------------------------------------------


def record_sent_message(message_id, text="", title="", chat_id=None):
    """Record a sent message in the local database."""
    if not chat_id:
        raise ValueError("chat_id is required")
    conn = _get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO messages"
            " (message_id, chat_id, direction, source_message_id,"
            "  message_time, text, title, sent_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                chat_id,
                "sent",
                message_id,
                int(time.time() * 1000),
                text,
                title,
                int(time.time()),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def record_received_message(
    chat_id,
    text="",
    title="",
    source_message_id="",
    message_time=None,
):
    """Record a received message in the local database.

    Uses a namespaced primary key to avoid colliding with sent message IDs.
    """
    if not chat_id:
        return
    ts_ms = None
    if message_time is not None:
        try:
            ts_ms = int(str(message_time).strip())
        except ValueError:
            ts_ms = None
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)

    raw_id = str(source_message_id or "").strip()
    if raw_id:
        db_message_id = f"recv:{raw_id}"
    else:
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        db_message_id = f"recv:{chat_id}:{ts_ms}:{text_hash}"

    conn = _get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO messages"
            " (message_id, chat_id, direction, source_message_id,"
            "  message_time, text, title, sent_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                db_message_id,
                chat_id,
                "received",
                raw_id or None,
                ts_ms,
                text,
                title,
                int(time.time()),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_latest_sent_message(session_id):
    """Return the most recent sent message for a session's chat_id."""
    session = get_session(session_id)
    if not session:
        return None
    chat_id = session.get("chat_id", "")
    if not chat_id:
        return None
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT message_id, text, title, sent_at FROM messages"
            " WHERE chat_id = ? AND direction = 'sent'"
            " ORDER BY sent_at DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if row:
            return {"message_id": row[0], "text": row[1], "title": row[2], "sent_at": row[3]}
        return None
    finally:
        conn.close()


def is_bot_sent_message(message_id):
    """Check if a message_id was sent by the bot (exists in messages with direction='sent').

    Used by the need_mention interaction filter to detect replies to bot messages.
    The message_id here is the Lark source_message_id (not the internal DB key).
    """
    if not message_id:
        return False
    conn = _get_db()
    try:
        # Check both by primary key (message_id) and by source_message_id
        row = conn.execute(
            "SELECT 1 FROM messages"
            " WHERE direction = 'sent'"
            "   AND (message_id = ? OR source_message_id = ?)"
            " LIMIT 1",
            (message_id, message_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_unprocessed_messages(chat_id):
    """Return received messages newer than the last sent message.

    Used on handoff resume to replay messages that were received (recorded in
    DB by handle_result) but never processed by Claude due to an API crash.

    Returns list of dicts: [{text, message_id, create_time, msg_type}, ...],
    matching the same shape as worker poll replies so callers can treat them
    identically.  Returns [] if there are no unprocessed messages.
    """
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT MAX(message_time) FROM messages"
            " WHERE chat_id = ? AND direction = 'sent'",
            (chat_id,),
        ).fetchone()
        last_sent_ts = row[0] if row and row[0] else 0

        rows = conn.execute(
            "SELECT text, source_message_id, message_time FROM messages"
            " WHERE chat_id = ? AND direction = 'received'"
            "   AND message_time > ?"
            "   AND source_message_id IS NOT NULL"
            "   AND source_message_id != ''"
            " ORDER BY message_time ASC",
            (chat_id, last_sent_ts),
        ).fetchall()
        return [
            {
                "text": r[0] or "",
                "message_id": r[1] or "",
                "create_time": str(r[2]) if r[2] else "",
                "msg_type": "text",
                "sender_type": "user",
            }
            for r in rows
        ]
    finally:
        conn.close()


def lookup_parent_message(parent_id):
    """Look up a sent message by its message_id. Returns dict or None."""
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT message_id, text, title, sent_at FROM messages"
            " WHERE message_id = ? AND direction = 'sent'",
            (parent_id,),
        ).fetchone()
        if row:
            return {
                "message_id": row[0],
                "text": row[1],
                "title": row[2],
                "sent_at": row[3],
            }
        return None
    finally:
        conn.close()
