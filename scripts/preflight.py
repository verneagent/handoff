#!/usr/bin/env python3
"""Preflight check for handoff mode. Verifies all requirements are met."""

import json
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import handoff_config
import lark_im


def check_credentials():
    try:
        with open(handoff_config.CONFIG_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return False, (
            f"Config file not found: {handoff_config.CONFIG_FILE}\n"
            f"Create it with app_id, app_secret, worker_url, and email.\n"
            f"  app_id/app_secret: Get from https://open.larksuite.com/app → your app → Credentials\n"
            f"  worker_url: Deploy the worker and note the URL. Run /handoff init\n"
            f"  email: Your Lark login email. Run /handoff init to configure"
        )
    except json.JSONDecodeError:
        return False, (
            f"Config file has invalid JSON: {handoff_config.CONFIG_FILE}\n"
            f"Fix the syntax or delete it and run /handoff init to recreate."
        )

    # Resolve IM-specific config
    ims = data.get("ims")
    if not isinstance(ims, dict):
        return False, (
            f"Missing 'ims' section in {handoff_config.CONFIG_FILE}\n"
            f"Run /handoff init to configure."
        )
    provider = data.get("default_im", "lark")
    im_cfg = ims.get(provider)
    if not isinstance(im_cfg, dict):
        return False, (
            f"No config for IM provider '{provider}' in {handoff_config.CONFIG_FILE}"
        )

    missing = []
    if not im_cfg.get("app_id"):
        missing.append("app_id")
    if not im_cfg.get("app_secret"):
        missing.append("app_secret")
    if not im_cfg.get("email"):
        missing.append("email")

    if missing:
        return False, (f"Missing {', '.join(missing)} in {handoff_config.CONFIG_FILE}")
    return True, None


def check_worker_url():
    url = handoff_config.load_worker_url()
    if not url:
        return False, (
            f"Missing worker_url.\n"
            f"Add worker_url to {handoff_config.CONFIG_FILE}\n"
            f"Deploy the worker and note the URL. Run /handoff init"
        )
    return True, url


def check_api_key():
    key = handoff_config.load_api_key()
    if not key:
        return False, (
            f"Missing worker_api_key in {handoff_config.CONFIG_FILE}\n"
            f'Generate a random key: python3 -c "import secrets; print(secrets.token_urlsafe(32))"\n'
            f"Add it to the config file AND as a Cloudflare Worker secret:\n"
            f"  cd .claude/skills/handoff/worker && npx wrangler secret put API_KEY"
        )
    return True, None


def check_worker_reachable(worker_url):
    try:
        result = subprocess.run(
            [
                "curl",
                "-s",
                "--max-time",
                "10",
                *handoff_config._worker_auth_headers(),
                f"{worker_url}/health",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return False, f"Worker unreachable: curl failed ({result.stderr.strip()})"
        if result.stdout.strip() == "Unauthorized":
            return False, (
                "Worker returned 401 Unauthorized. "
                "Check that worker_api_key in config matches the API_KEY secret on the worker."
            )
        data = json.loads(result.stdout)
        if not data.get("ok"):
            return False, f"Worker returned unexpected response: {result.stdout[:200]}"
    except Exception as e:
        return False, f"Worker unreachable: {e}"

    # Check if VERIFY_TOKEN is configured on the worker
    if not data.get("verify_token"):
        return False, (
            "Worker VERIFY_TOKEN not configured — webhook events will be rejected.\n"
            "Set it: cd .claude/skills/handoff/worker && npx wrangler secret put VERIFY_TOKEN\n"
            "Use the Verification Token from your Lark app's Event Subscriptions page."
        )
    return True, None


def check_token():
    creds = handoff_config.load_credentials()
    if not creds:
        return False, "Skipped (no credentials)"
    try:
        lark_im.get_tenant_token(creds["app_id"], creds["app_secret"])
    except Exception as e:
        return False, f"Failed to get Lark tenant token: {e}"
    return True, None


def _load_required_hooks():
    """Load required hook names from hooks.json (single source of truth)."""
    hooks_json = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "hooks.json",
    )
    try:
        with open(hooks_json) as f:
            return list(json.load(f).keys())
    except (FileNotFoundError, json.JSONDecodeError):
        # Fallback if hooks.json is missing
        return [
            "Notification",
            "PermissionRequest",
            "PostToolUse",
            "SessionStart",
            "SessionEnd",
        ]


def check_hooks():
    required = _load_required_hooks()
    found = set()

    # Check global settings (~/.claude/)
    for fname in ["settings.json", "settings.local.json"]:
        path = os.path.expanduser(f"~/.claude/{fname}")
        try:
            with open(path) as f:
                data = json.load(f)
            for hook_name in required:
                if data.get("hooks", {}).get(hook_name):
                    found.add(hook_name)
        except (FileNotFoundError, json.JSONDecodeError):
            continue

    # Check project settings (.claude/) if available
    try:
        project_dir = handoff_config._require_project_dir()
        for fname in ["settings.json", "settings.local.json"]:
            path = os.path.join(project_dir, ".claude", fname)
            try:
                with open(path) as f:
                    data = json.load(f)
                for hook_name in required:
                    if data.get("hooks", {}).get(hook_name):
                        found.add(hook_name)
            except (FileNotFoundError, json.JSONDecodeError):
                continue
    except RuntimeError:
        pass  # Global-only install, no project dir yet

    if [h for h in required if h not in found]:
        return False, "Handoff is not initialized. Run /handoff init."
    return True, None


def check_opencode_plugin():
    """Check that the OpenCode handoff plugin files are installed in the project."""
    try:
        project_dir = handoff_config._require_project_dir()
    except RuntimeError:
        return False, "HANDOFF_PROJECT_DIR is not set"

    files = [
        os.path.join(project_dir, ".opencode", "plugins", "handoff.ts"),
        os.path.join(project_dir, ".opencode", "scripts", "permission_bridge.py"),
        os.path.join(project_dir, ".opencode", "scripts", "handoff_tool_forwarding.js"),
    ]
    missing = [os.path.relpath(f, project_dir) for f in files if not os.path.exists(f)]
    if missing:
        return False, (
            f"OpenCode plugin files not installed: {', '.join(missing)}\n"
            f"Run /handoff init to install them from the skill's assets."
        )
    return True, None


def _redact(val):
    """Redact a secret value for display."""
    if not val:
        return "(missing)"
    return "***" + val[-4:] if len(val) > 4 else "****"


def report():
    """Print a detailed status report of all configured values."""
    print("=== Handoff Configuration Report ===\n")

    # Config file
    print(f"Config file: {handoff_config.CONFIG_FILE}")
    config = {}
    try:
        with open(handoff_config.CONFIG_FILE) as f:
            config = json.load(f)
    except FileNotFoundError:
        print("  (file not found)\n")
    except json.JSONDecodeError:
        print("  (invalid JSON)\n")

    # Top-level infrastructure fields
    for field, redact in [("worker_url", False), ("worker_api_key", True)]:
        val = config.get(field, "")
        display = _redact(val) if redact else (val or "(missing)")
        print(f"  {field}: {display}")

    # IM-specific fields
    im_cfg = handoff_config._resolve_im_config(config) or {}
    provider = config.get("default_im", "lark")
    print(f"  IM provider: {provider}")
    for field, redact in [("app_id", False), ("app_secret", True), ("email", False)]:
        val = im_cfg.get(field, "")
        display = _redact(val) if redact else (val or "(missing)")
        print(f"  {field}: {display}")

    print("\nHealth:")
    creds_ok, creds_detail = check_credentials()
    if creds_ok:
        token_ok, token_detail = check_token()
        if token_ok:
            print("  Lark token: OK")
        else:
            print(f"  Lark token: FAIL ({token_detail})")
    else:
        print(f"  Lark token: SKIP ({creds_detail})")

    worker_url = config.get("worker_url", "")
    if worker_url:
        worker_ok, worker_detail = check_worker_reachable(worker_url)
        if worker_ok:
            print("  Worker health: OK")
        else:
            print(f"  Worker health: FAIL ({worker_detail})")
    else:
        print("  Worker health: SKIP (worker_url missing)")

    # Hooks
    print(f"\nHooks:")
    try:
        project_dir = handoff_config._require_project_dir()
    except RuntimeError:
        print("  HANDOFF_PROJECT_DIR is not set — cannot check hooks")
        project_dir = ""  # skip hook scanning below
    for fname in ["settings.json", "settings.local.json"]:
        path = os.path.join(project_dir, ".claude", fname)
        try:
            with open(path) as f:
                data = json.load(f)
            hooks = data.get("hooks", {})
            found = []
            for hook_name in _load_required_hooks():
                if hooks.get(hook_name):
                    found.append(hook_name)
            if found:
                print(f"  project/{fname}: {', '.join(found)}")
            else:
                print(f"  project/{fname}: (no handoff hooks)")
        except FileNotFoundError:
            print(f"  project/{fname}: (file not found)")
        except json.JSONDecodeError:
            print(f"  project/{fname}: (invalid JSON)")

    for fname in ["settings.json", "settings.local.json"]:
        path = os.path.expanduser(f"~/.claude/{fname}")
        if _has_handoff_hooks(path):
            print(f"  global/{fname}: has handoff hooks")
        elif os.path.exists(path):
            print(f"  global/{fname}: (no handoff hooks)")

    ok, detail = check_dual_install()
    if not ok:
        print(f"\n  [WARN] Dual install: {detail}")

    print()


def _has_handoff_hooks(settings_path):
    """Return True if a settings file contains any handoff hook entries."""
    try:
        with open(settings_path) as f:
            data = json.load(f)
        for entries in data.get("hooks", {}).values():
            for entry in entries:
                for hook in entry.get("hooks", []):
                    if "handoff/scripts/" in hook.get("command", ""):
                        return True
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        pass
    return False


def check_dual_install():
    """Warn if handoff hooks are installed both globally and at project level.

    Both sets of hooks run simultaneously in Claude Code, causing duplicate
    Lark messages and duplicate permission cards.
    """
    global_settings = os.path.expanduser("~/.claude/settings.json")
    global_local = os.path.expanduser("~/.claude/settings.local.json")
    global_has = _has_handoff_hooks(global_settings) or _has_handoff_hooks(global_local)
    if not global_has:
        return True, None  # No global install, no conflict

    try:
        project_dir = handoff_config._require_project_dir()
    except RuntimeError:
        return True, None  # Can't check project dir, skip

    project_has = any(
        _has_handoff_hooks(os.path.join(project_dir, ".claude", fname))
        for fname in ["settings.json", "settings.local.json"]
    )
    if not project_has:
        return True, None  # Only global install, fine

    return False, (
        "Handoff hooks are installed BOTH globally (~/.claude/settings.json) "
        "and at project level (.claude/settings.json).\n"
        "Both sets of hooks run simultaneously, causing duplicate Lark messages "
        "and duplicate permission request cards.\n"
        "Fix: run /handoff deinit in one context to remove the duplicate install."
    )


def _parse_tool(argv):
    """Return tool name from --tool <name> or --tool=<name>.
    Accepts --skip-hooks as a backward-compat alias for --tool opencode.
    Defaults to 'claude'.
    """
    args = argv[1:]
    if "--skip-hooks" in args:
        return "opencode"
    for i, arg in enumerate(args):
        if arg == "--tool" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--tool="):
            return arg.split("=", 1)[1]
    return "claude"


def main():
    if "--report" in sys.argv:
        report()
        return

    tool = _parse_tool(sys.argv)
    checks = [
        ("Lark credentials", check_credentials),
        ("Worker URL", check_worker_url),
        ("Worker API key", check_api_key),
        ("Lark token", check_token),
    ]
    if tool == "opencode":
        checks.append(("OpenCode plugin", check_opencode_plugin))
    else:
        checks.append(("Hooks configured", check_hooks))

    errors = []
    warnings = []
    worker_url = None

    for name, fn in checks:
        ok, detail = fn()
        if ok:
            print(f"  [OK] {name}")
            if name == "Worker URL":
                worker_url = detail
        else:
            print(f"  [FAIL] {name}: {detail}")
            errors.append(detail)

    # Check worker reachability separately (needs URL from previous check)
    if worker_url:
        ok, detail = check_worker_reachable(worker_url)
        if ok:
            print(f"  [OK] Worker reachable")
        else:
            print(f"  [FAIL] Worker reachable: {detail}")
            errors.append(detail)

    # Dual-install warning (non-blocking)
    ok, detail = check_dual_install()
    if not ok:
        print(f"  [WARN] Dual install detected: {detail}")
        warnings.append(detail)

    if errors:
        print(f"\n{len(errors)} issue(s) found. Fix them before using /handoff.")
        sys.exit(1)
    elif warnings:
        print(f"\n{len(warnings)} warning(s). Handoff will work but may send duplicate messages.")
    else:
        print("\nAll checks passed. Ready for handoff.")


if __name__ == "__main__":
    main()
