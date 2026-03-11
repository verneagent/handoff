#!/usr/bin/env python3
"""Deterministic hook installer for the handoff skill.

Detects install scope (project vs global), resolves hook commands,
and merges them into the correct settings.json. Idempotent.

Usage:
    python3 install_hooks.py [--project-dir <dir>] [--dry-run]

Output (stdout): JSON with ok, scope, target, added, skipped fields.
Exit 0 on success, 1 on error.
"""

import argparse
import json
import os
import re
import sys


def main():
    parser = argparse.ArgumentParser(description="Install handoff hooks")
    parser.add_argument(
        "--project-dir",
        default=os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()),
        help="Project directory (default: $CLAUDE_PROJECT_DIR or cwd)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without writing",
    )
    args = parser.parse_args()

    project_dir = os.path.abspath(args.project_dir)

    # 1. Locate self → skill root is two parents up from scripts/install_hooks.py
    script_dir = os.path.dirname(os.path.abspath(__file__))
    skill_dir = os.path.dirname(script_dir)

    # 2. Scope detection
    project_hooks_path = os.path.join(
        project_dir, ".claude", "skills", "handoff", "hooks.json"
    )
    if os.path.isfile(project_hooks_path):
        scope = "project"
        hooks_path = project_hooks_path
    else:
        scope = "global"
        hooks_path = os.path.join(skill_dir, "hooks.json")

    # 3. Read hooks.json
    if not os.path.isfile(hooks_path):
        result = {"ok": False, "error": f"hooks.json not found at {hooks_path}"}
        print(json.dumps(result))
        sys.exit(1)

    with open(hooks_path, "r") as f:
        hooks_data = json.load(f)

    # 4. Resolve commands by scope
    resolved_hooks = {}
    for event_type, entries in hooks_data.items():
        resolved_entries = []
        for entry in entries:
            resolved_entry = dict(entry)
            resolved_hooks_list = []
            for hook in entry.get("hooks", []):
                resolved_hook = dict(hook)
                if scope == "global":
                    # Extract script filename from command and replace with literal path
                    cmd = hook.get("command", "")
                    m = re.search(r"scripts/(\w+\.py)", cmd)
                    if m:
                        script_name = m.group(1)
                        resolved_hook["command"] = (
                            f'python3 "{script_dir}/{script_name}"'
                        )
                    # If no match, keep command as-is (shouldn't happen)
                resolved_hooks_list.append(resolved_hook)
            resolved_entry["hooks"] = resolved_hooks_list
            resolved_entries.append(resolved_entry)
        resolved_hooks[event_type] = resolved_entries

    # 5. Determine target settings.json
    if scope == "project":
        target = os.path.join(project_dir, ".claude", "settings.json")
    else:
        target = os.path.expanduser("~/.claude/settings.json")

    # 6. Read target (or start with {})
    if os.path.isfile(target):
        with open(target, "r") as f:
            settings = json.load(f)
    else:
        settings = {}

    # 7. Merge
    if "hooks" not in settings:
        settings["hooks"] = {}

    added = []
    skipped = []

    for event_type, new_entries in resolved_hooks.items():
        if event_type not in settings["hooks"]:
            settings["hooks"][event_type] = []

        existing_list = settings["hooks"][event_type]

        for new_entry in new_entries:
            for new_hook in new_entry.get("hooks", []):
                new_cmd = new_hook.get("command", "")
                # Extract script filename for duplicate detection
                new_script_match = re.search(r"scripts/(\w+\.py)", new_cmd)
                new_script_name = new_script_match.group(1) if new_script_match else None

                # Check if any existing entry already has this script
                found = False
                if new_script_name:
                    for existing_entry in existing_list:
                        for existing_hook in existing_entry.get("hooks", []):
                            existing_cmd = existing_hook.get("command", "")
                            if new_script_name in existing_cmd:
                                found = True
                                break
                        if found:
                            break

                if found:
                    skipped.append(event_type)
                else:
                    existing_list.append(new_entry)
                    added.append(event_type)

    # 7b. Ensure permission pattern for handoff scripts.
    #     Single wildcard pattern covers all scripts in the directory.
    if "permissions" not in settings:
        settings["permissions"] = {}
    if "allow" not in settings["permissions"]:
        settings["permissions"]["allow"] = []

    allow_list = settings["permissions"]["allow"]
    perm_pattern = f'Bash(python3 "{script_dir}/"*)'
    perms_added = []
    perms_skipped = []

    if perm_pattern in allow_list:
        # Wildcard pattern already present
        perms_skipped.append(perm_pattern)
    else:
        # Remove any old per-script patterns from previous installs
        old_patterns = [
            p for p in allow_list
            if p.startswith("Bash(") and "handoff" in p and "scripts/" in p
        ]
        for old in old_patterns:
            allow_list.remove(old)
            perms_skipped.append(f"removed: {old}")
        allow_list.append(perm_pattern)
        perms_added.append(perm_pattern)

    # 8. Write
    if not args.dry_run:
        # Ensure parent directory exists
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
            f.write("\n")

    # 9. Write hooks-pending marker if any hooks were added
    if added and not args.dry_run:
        marker_dir = f"/private/tmp/claude-{os.getuid()}"
        os.makedirs(marker_dir, exist_ok=True)
        marker = os.path.join(marker_dir, "handoff-hooks-pending")
        with open(marker, "w") as f:
            f.write("")

    # 10. Warn about stale project-level skill dir on global install
    stale_warning = None
    if scope == "global":
        project_skill_dir = os.path.join(
            project_dir, ".claude", "skills", "handoff"
        )
        if os.path.isdir(project_skill_dir) and not os.path.isfile(
            os.path.join(project_skill_dir, "hooks.json")
        ):
            stale_warning = (
                f"⚠️  Stale .claude/skills/handoff/ found in {project_dir}. "
                f"This may shadow the global install in other tools (e.g. OpenCode). "
                f"Remove it with: rm -rf {project_skill_dir}"
            )
            print(stale_warning, file=sys.stderr)

    # 11. Print result
    result = {
        "ok": True,
        "scope": scope,
        "target": target,
        "added": added,
        "skipped": skipped,
        "perms_added": perms_added,
        "perms_skipped": perms_skipped,
    }
    if stale_warning:
        result["stale_project_dir"] = stale_warning
    if args.dry_run:
        result["dry_run"] = True
    print(json.dumps(result))


if __name__ == "__main__":
    main()
