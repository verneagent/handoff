#!/usr/bin/env python3
"""Handoff skill self-upgrade: download latest from GitHub and install.

Usage:
    python3 scripts/upgrade.py [--check]

Flags:
    --check    Only check if an upgrade is available, don't install.

The script:
1. Detects the current install location (resolves symlinks)
2. Downloads the latest code from GitHub to a temp directory
3. Copies files to the install directory
4. Reinstalls hooks if hooks.json changed
5. Reports what changed
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = "verneagent/handoff"
BRANCH = "main"
SKILL_FILES = [
    "SKILL.md",
    "SKILL-setup.md",
    "SKILL-commands.md",
    "hooks.json",
    "CLAUDE.md",
    "README.md",
    ".gitignore",
]
SKILL_DIRS = ["scripts", "assets", "worker"]


def find_install_dir():
    """Find the handoff skill install directory.

    Resolution order:
    1. Script's own directory parent (most reliable)
    2. ~/.agents/skills/handoff (global install)
    3. ~/.claude/skills/handoff (user install, may be symlink)
    """
    # This script lives in <install_dir>/scripts/upgrade.py
    script_dir = os.path.dirname(os.path.abspath(__file__))
    install_dir = os.path.dirname(script_dir)

    # Resolve symlinks to get the real directory
    install_dir = os.path.realpath(install_dir)
    return install_dir


def file_hash(path):
    """SHA256 hash of a file, or None if it doesn't exist."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except FileNotFoundError:
        return None


def download_latest(tmp_dir):
    """Clone the latest code from GitHub into tmp_dir."""
    url = f"https://github.com/{REPO}.git"
    result = subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", BRANCH, url, tmp_dir],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        # Try SSH fallback
        ssh_url = f"git@github.com:{REPO}.git"
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", BRANCH, ssh_url, tmp_dir],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to clone: {result.stderr.strip()}"
            )


def get_remote_version(tmp_dir):
    """Get the latest commit hash from the downloaded repo."""
    result = subprocess.run(
        ["git", "rev-parse", "--short=8", "HEAD"],
        capture_output=True,
        text=True,
        cwd=tmp_dir,
        timeout=5,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def get_local_version(install_dir):
    """Get version info from the installed copy."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            capture_output=True,
            text=True,
            cwd=install_dir,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def sync_files(src_dir, dst_dir):
    """Copy files from src to dst, returning list of changed files."""
    changed = []

    # Sync individual files
    for fname in SKILL_FILES:
        src = os.path.join(src_dir, fname)
        dst = os.path.join(dst_dir, fname)
        if not os.path.exists(src):
            continue
        src_hash = file_hash(src)
        dst_hash = file_hash(dst)
        if src_hash != dst_hash:
            os.makedirs(os.path.dirname(dst) or dst_dir, exist_ok=True)
            shutil.copy2(src, dst)
            changed.append(fname)

    # Sync directories
    for dname in SKILL_DIRS:
        src = os.path.join(src_dir, dname)
        dst = os.path.join(dst_dir, dname)
        if not os.path.isdir(src):
            continue
        # Walk and compare
        for root, dirs, files in os.walk(src):
            rel_root = os.path.relpath(root, src)
            dst_root = os.path.join(dst, rel_root)
            os.makedirs(dst_root, exist_ok=True)
            for f in files:
                if f.startswith(".") or f.endswith(".pyc") or "__pycache__" in root:
                    continue
                src_file = os.path.join(root, f)
                dst_file = os.path.join(dst_root, f)
                if file_hash(src_file) != file_hash(dst_file):
                    shutil.copy2(src_file, dst_file)
                    rel_path = os.path.join(dname, os.path.relpath(src_file, src))
                    changed.append(rel_path)

        # Remove files that no longer exist in source
        if os.path.isdir(dst):
            for root, dirs, files in os.walk(dst):
                rel_root = os.path.relpath(root, dst)
                src_root = os.path.join(src, rel_root)
                for f in files:
                    if f.endswith(".pyc") or "__pycache__" in root:
                        continue
                    dst_file = os.path.join(root, f)
                    src_file = os.path.join(src_root, f)
                    if not os.path.exists(src_file):
                        os.remove(dst_file)
                        rel_path = os.path.join(dname, os.path.relpath(dst_file, dst))
                        changed.append(f"(removed) {rel_path}")

    return changed


def reinstall_hooks(install_dir):
    """Reinstall hooks from hooks.json if the install_hooks.py script exists."""
    install_script = os.path.join(install_dir, "scripts", "install_hooks.py")
    if os.path.exists(install_script):
        result = subprocess.run(
            [sys.executable, install_script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0, result.stdout.strip()
    return False, "install_hooks.py not found"


def main():
    parser = argparse.ArgumentParser(description="Upgrade handoff skill")
    parser.add_argument("--check", action="store_true",
                        help="Only check for updates, don't install")
    args = parser.parse_args()

    install_dir = find_install_dir()
    local_version = get_local_version(install_dir)

    print(f"Install dir: {install_dir}")
    print(f"Local version: {local_version}")

    # Download latest
    tmp_dir = tempfile.mkdtemp(prefix="handoff-upgrade-")
    try:
        print(f"Downloading latest from {REPO}...")
        download_latest(tmp_dir)
        remote_version = get_remote_version(tmp_dir)
        print(f"Remote version: {remote_version}")

        if args.check:
            if local_version == remote_version:
                print("Already up to date.")
                result = {"up_to_date": True, "version": local_version}
            else:
                print(f"Update available: {local_version} → {remote_version}")
                result = {
                    "up_to_date": False,
                    "local": local_version,
                    "remote": remote_version,
                }
            print(json.dumps(result))
            return 0

        # Check what would change
        hooks_before = file_hash(os.path.join(install_dir, "hooks.json"))

        # Sync files
        changed = sync_files(tmp_dir, install_dir)

        if not changed:
            print("Already up to date. No files changed.")
            print(json.dumps({"ok": True, "changed": 0, "version": remote_version}))
            return 0

        print(f"\nUpdated {len(changed)} file(s):")
        for f in changed[:20]:
            print(f"  {f}")
        if len(changed) > 20:
            print(f"  ... and {len(changed) - 20} more")

        # Check if hooks need reinstalling
        hooks_after = file_hash(os.path.join(install_dir, "hooks.json"))
        hooks_changed = hooks_before != hooks_after

        if hooks_changed:
            print("\nhooks.json changed — reinstalling hooks...")
            ok, msg = reinstall_hooks(install_dir)
            if ok:
                print(f"Hooks reinstalled. {msg}")
            else:
                print(f"Hook reinstall failed: {msg}")
                print("Run `/handoff init` to reinstall hooks manually.")

        skill_md_changed = any("SKILL.md" in f for f in changed)
        if skill_md_changed:
            print("\nSKILL.md changed — restart CLI to pick up new skill definition.")

        print(json.dumps({
            "ok": True,
            "changed": len(changed),
            "version": remote_version,
            "hooks_reinstalled": hooks_changed,
            "restart_needed": skill_md_changed,
        }))
        return 0

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main() or 0)
