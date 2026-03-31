#!/usr/bin/env python3
"""Handoff configuration: credentials, worker URL, project identity.

Non-Lark-specific configuration and utility functions extracted from lark_im.py.
"""

import json
import os
import re
import socket
import sys

HANDOFF_HOME = os.path.expanduser("~/.handoff")
_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]+$")
_CHAT_ID_MAX_LEN = 128
_PROFILE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def is_valid_chat_id(chat_id):
    """Return True if chat_id contains only URL-safe characters.

    Intentionally loose — must work across Lark, Slack, and future platforms.
    Rejects characters that could cause URL path injection (/, ?, #, &, spaces).
    """
    if not chat_id or not isinstance(chat_id, str):
        return False
    if len(chat_id) > _CHAT_ID_MAX_LEN:
        return False
    return bool(_CHAT_ID_RE.match(chat_id))


def default_config_file():
    return os.path.join(HANDOFF_HOME, "config.json")


CONFIG_FILE = default_config_file()


# ---------------------------------------------------------------------------
# Profile support
# ---------------------------------------------------------------------------


def validate_profile_name(name):
    """Raise ValueError if *name* is not a valid profile identifier."""
    if not name or not isinstance(name, str):
        raise ValueError(f"Invalid profile name: {name!r}")
    if not _PROFILE_NAME_RE.match(name):
        raise ValueError(
            f"Invalid profile name: {name!r} — "
            "must contain only letters, digits, hyphens, and underscores"
        )
    return name


def get_default_profile():
    """Read the default profile name from ~/.handoff/default_profile.

    Returns the profile name string, or None if the file does not exist.
    """
    path = os.path.join(HANDOFF_HOME, "default_profile")
    try:
        with open(path) as f:
            name = f.read().strip()
        if name:
            validate_profile_name(name)
            return name
    except (FileNotFoundError, ValueError):
        pass
    return None


def set_default_profile(name):
    """Write the default profile name to ~/.handoff/default_profile."""
    validate_profile_name(name)
    path = os.path.join(HANDOFF_HOME, "default_profile")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(name + "\n")


def resolve_profile(explicit=None):
    """Resolve the active profile name.

    Resolution chain (first non-empty wins):
      1. *explicit* argument
      2. HANDOFF_PROFILE environment variable
      3. ~/.handoff/default_profile file
      4. "default"

    This is a pure function — it reads from its arguments, env vars, and
    files, never from module-level state.
    """
    if explicit:
        validate_profile_name(explicit)
        return explicit
    env = os.environ.get("HANDOFF_PROFILE", "").strip()
    if env:
        validate_profile_name(env)
        return env
    file_default = get_default_profile()
    if file_default:
        return file_default
    return "default"


def config_path(profile):
    """Return the config file path for a given profile.

    "default" → ~/.handoff/config.json
    Others   → ~/.handoff/profiles/<name>.json
    """
    validate_profile_name(profile)
    if profile == "default":
        return os.path.join(HANDOFF_HOME, "config.json")
    return os.path.join(HANDOFF_HOME, "profiles", f"{profile}.json")


def list_profiles():
    """List all available profile names. Always includes "default"."""
    profiles = set()
    if os.path.exists(os.path.join(HANDOFF_HOME, "config.json")):
        profiles.add("default")
    profiles_dir = os.path.join(HANDOFF_HOME, "profiles")
    if os.path.isdir(profiles_dir):
        for fname in os.listdir(profiles_dir):
            if fname.endswith(".json"):
                name = fname[:-5]
                try:
                    validate_profile_name(name)
                    profiles.add(name)
                except ValueError:
                    pass
    # Always include "default" even if config.json doesn't exist yet
    profiles.add("default")
    return sorted(profiles)


def _require_project_dir():
    """Return HANDOFF_PROJECT_DIR (or CLAUDE_PROJECT_DIR fallback) or raise.

    Hooks (PostToolUse, Notification, etc.) run as subprocesses and may only
    have CLAUDE_PROJECT_DIR in their env, not HANDOFF_PROJECT_DIR.  The main
    process and Bash tool calls get HANDOFF_PROJECT_DIR via the session env
    file.  We try both so the DB path resolves in either context.
    """
    project_dir = os.environ.get("HANDOFF_PROJECT_DIR") or os.environ.get(
        "CLAUDE_PROJECT_DIR"
    )
    if not project_dir:
        raise RuntimeError(
            "HANDOFF_PROJECT_DIR is not set. "
            "Ensure the SessionStart hook has run to persist it."
        )
    return project_dir


def resolve_chat_id(session_id=None):
    """Resolve chat_id from session or HANDOFF_CHAT_ID env var.

    In agent mode, the SDK's claude process has a different session_id
    than the agent process, so session lookup may fail. HANDOFF_CHAT_ID
    provides a direct fallback.

    Returns (chat_id, session_or_None, profile).
    Raises RuntimeError if neither source provides a chat_id.
    """
    import handoff_db

    sid = session_id or os.environ.get("HANDOFF_SESSION_ID", "")
    session = handoff_db.resolve_session(sid) if sid else None
    direct_chat_id = os.environ.get("HANDOFF_CHAT_ID", "").strip()

    if not session and not direct_chat_id:
        raise RuntimeError(f"No active session for {sid} and HANDOFF_CHAT_ID not set")

    chat_id = (session.get("chat_id") if session else None) or direct_chat_id
    if not chat_id:
        raise RuntimeError("Could not resolve chat_id")

    profile = (session.get("config_profile", "default") if session
               else os.environ.get("HANDOFF_CONFIG_PROFILE", "default"))

    return chat_id, session, profile


def _get_machine_name():
    """Get the machine name, preferring macOS ComputerName over hostname.

    On macOS, socket.gethostname() can return the IP address (e.g. 192.168.0.114)
    when configd is out of sync with System Settings. Read the ComputerName from
    the SystemConfiguration plist as the primary source.
    """
    if sys.platform == "darwin":
        try:
            import plistlib
            plist_path = "/Library/Preferences/SystemConfiguration/preferences.plist"
            with open(plist_path, "rb") as f:
                data = plistlib.load(f)
            name = data.get("System", {}).get("System", {}).get("ComputerName")
            if name:
                return name
        except Exception:
            pass
    return socket.gethostname().split(".")[0]


def get_workspace_id():
    """Compute the workspace ID from machine name + project directory.

    Identifies the physical code location (machine + folder path).
    Used as a tag in Lark group descriptions to associate groups with projects.
    """
    project_dir = _require_project_dir()
    machine = _get_machine_name()
    folder = project_dir.replace("/", "-").strip("-")
    return f"{machine}-{folder}"


def get_worktree_name():
    """Get worktree name from git toplevel or branch, falling back to folder name."""
    import subprocess

    cwd = os.environ.get("HANDOFF_PROJECT_DIR") or os.getcwd()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=5,
        )
        if result.returncode == 0:
            name = os.path.basename(result.stdout.strip())
            if name:
                return name
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return os.path.basename(cwd)


# ---------------------------------------------------------------------------
# Temporary files and cleanup
# ---------------------------------------------------------------------------


def handoff_tmp_dir():
    """Base temporary directory for handoff runtime artifacts."""
    custom = os.environ.get("HANDOFF_TMP_DIR", "").strip()
    if custom:
        return custom
    return "/tmp/handoff"


def default_poll_timeout(session):
    """Return the appropriate poll timeout in seconds based on the session model.

    GPT-based models require a bounded timeout (540s) because their tool-use
    runtime has a hard 600s limit.  All other models (Claude, Gemini, etc.)
    can block indefinitely (0) which reduces background-task churn.
    """
    model = (session or {}).get("session_model", "") or ""
    if "gpt" in model.lower():
        return 540
    return 0


# ---------------------------------------------------------------------------
# Config file I/O
# ---------------------------------------------------------------------------


def _load_config(profile):
    """Read raw JSON from the config file for *profile*.

    Returns dict or None on error.
    """
    path = config_path(profile)
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _resolve_im_config(raw):
    """Extract IM-specific credentials from a raw config dict.

    Format: {"default_im": "lark", "ims": {"lark": {"app_id": ...}}}

    Returns dict with app_id/app_secret/email keys, or None if required
    fields are missing.
    """
    if raw is None:
        return None
    ims = raw.get("ims")
    if not isinstance(ims, dict):
        return None
    provider = raw.get("default_im", "lark")
    im_cfg = ims.get(provider)
    if not isinstance(im_cfg, dict):
        return None
    if not im_cfg.get("app_id") or not im_cfg.get("app_secret"):
        return None
    return im_cfg


def load_credentials(profile):
    """Load app_id, app_secret, email from config file.

    Returns the config dict, or None if the file doesn't exist or
    required fields (app_id, app_secret) are missing.
    """
    return _resolve_im_config(_load_config(profile))


def load_worker_url(profile):
    """Load the Cloudflare Worker URL from config. Returns None if missing."""
    raw = _load_config(profile)
    if raw is None:
        return None
    url = raw.get("worker_url", "").strip()
    return url or None


def load_api_key(profile):
    """Load the Worker API key from config. Returns None if not set."""
    raw = _load_config(profile)
    if raw is None:
        return None
    return raw.get("worker_api_key", "").strip() or None


def _worker_auth_headers(profile):
    """Return curl args for Worker API auth. Empty list if no key configured."""
    key = load_api_key(profile)
    if key:
        return ["-H", f"Authorization: Bearer {key}"]
    return []


def save_credentials(
    app_id=None,
    app_secret=None,
    email=None,
    worker_url=None,
    worker_api_key=None,
    *,
    profile,
):
    """Save credentials to the config file for *profile*.

    IM-specific fields (app_id, app_secret, email) go under ims.lark;
    infrastructure fields (worker_url, worker_api_key) stay top-level.
    """
    target = config_path(profile)
    raw = {}
    if os.path.exists(target):
        try:
            with open(target) as f:
                raw = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            pass

    raw.setdefault("default_im", "lark")
    raw.setdefault("ims", {})

    provider = raw.get("default_im", "lark")
    im_cfg = raw["ims"].setdefault(provider, {})

    # Apply IM-specific updates
    if app_id:
        im_cfg["app_id"] = app_id
    if app_secret:
        im_cfg["app_secret"] = app_secret
    if email:
        im_cfg["email"] = email

    # Apply top-level updates
    if worker_url:
        raw["worker_url"] = worker_url
    if worker_api_key:
        raw["worker_api_key"] = worker_api_key

    os.makedirs(os.path.dirname(target), exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)
