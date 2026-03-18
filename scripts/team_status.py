#!/usr/bin/env python3
"""Read Claude Code Agent Teams state and format for Lark.

Reads team config from ~/.claude/teams/ and task lists from ~/.claude/tasks/
to produce status summaries that can be sent to Lark during handoff.

Usage:
    python3 team_status.py list              # List all active teams
    python3 team_status.py status <team_id>  # Detailed status of a team
    python3 team_status.py tasks <team_id>   # Task list for a team
"""

import json
import os
import sys

TEAMS_DIR = os.path.expanduser("~/.claude/teams")
TASKS_DIR = os.path.expanduser("~/.claude/tasks")


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _list_teams():
    """List teams that have task files (active or recent)."""
    teams = []
    if not os.path.isdir(TASKS_DIR):
        return teams

    for team_id in os.listdir(TASKS_DIR):
        team_path = os.path.join(TASKS_DIR, team_id)
        if not os.path.isdir(team_path):
            continue

        task_files = [
            f for f in os.listdir(team_path)
            if f.endswith(".json") and not f.startswith(".")
        ]
        if not task_files:
            continue

        tasks = []
        for tf in task_files:
            data = _load_json(os.path.join(team_path, tf))
            if data and isinstance(data, dict):
                tasks.append(data)

        if not tasks:
            continue

        completed = sum(1 for t in tasks if t.get("status") == "completed")
        in_progress = sum(1 for t in tasks if t.get("status") == "in_progress")
        pending = sum(1 for t in tasks if t.get("status") == "pending")

        # Try to load team config
        config = None
        config_path = os.path.join(TEAMS_DIR, team_id, "config.json")
        if os.path.isfile(config_path):
            config = _load_json(config_path)

        teams.append({
            "team_id": team_id,
            "task_count": len(tasks),
            "completed": completed,
            "in_progress": in_progress,
            "pending": pending,
            "config": config,
        })

    return teams


def _get_tasks(team_id):
    """Get all tasks for a team."""
    team_path = os.path.join(TASKS_DIR, team_id)
    if not os.path.isdir(team_path):
        return []

    tasks = []
    for f in os.listdir(team_path):
        if not f.endswith(".json") or f.startswith("."):
            continue
        data = _load_json(os.path.join(team_path, f))
        if data and isinstance(data, dict):
            tasks.append(data)

    # Sort by ID
    tasks.sort(key=lambda t: int(t.get("id", 0)))
    return tasks


def _status_emoji(status):
    if status == "completed":
        return "[done]"
    if status == "in_progress":
        return "[working]"
    return "[pending]"


def cmd_list():
    """List all teams with task summaries."""
    teams = _list_teams()
    if not teams:
        print(json.dumps({"teams": [], "message": "No active teams found"}))
        return

    result = []
    for t in teams:
        members = []
        if t["config"] and "members" in t["config"]:
            members = [m.get("name", "?") for m in t["config"]["members"]]

        result.append({
            "team_id": t["team_id"],
            "members": members,
            "tasks": f'{t["completed"]}/{t["task_count"]} done',
            "in_progress": t["in_progress"],
            "pending": t["pending"],
        })

    print(json.dumps({"teams": result}))


def cmd_status(team_id):
    """Detailed status for a team."""
    tasks = _get_tasks(team_id)
    if not tasks:
        print(json.dumps({"error": f"No tasks found for team {team_id}"}))
        return 1

    # Team config
    config = None
    config_path = os.path.join(TEAMS_DIR, team_id, "config.json")
    if os.path.isfile(config_path):
        config = _load_json(config_path)

    members = []
    if config and "members" in config:
        members = [{"name": m.get("name", "?"), "type": m.get("agentType", "?")}
                   for m in config["members"]]

    completed = [t for t in tasks if t.get("status") == "completed"]
    in_progress = [t for t in tasks if t.get("status") == "in_progress"]
    pending = [t for t in tasks if t.get("status") == "pending"]

    print(json.dumps({
        "team_id": team_id,
        "members": members,
        "total": len(tasks),
        "completed": len(completed),
        "in_progress": len(in_progress),
        "pending": len(pending),
        "completed_tasks": [{"id": t["id"], "subject": t.get("subject", "")} for t in completed],
        "in_progress_tasks": [{"id": t["id"], "subject": t.get("subject", ""), "active": t.get("activeForm", "")} for t in in_progress],
        "pending_tasks": [{"id": t["id"], "subject": t.get("subject", "")} for t in pending],
    }))


def cmd_tasks(team_id):
    """Full task list for a team."""
    tasks = _get_tasks(team_id)
    if not tasks:
        print(json.dumps({"error": f"No tasks found for team {team_id}"}))
        return 1

    print(json.dumps({"team_id": team_id, "tasks": tasks}))


def format_status_card(team_id):
    """Format team status as a Lark-friendly markdown string."""
    tasks = _get_tasks(team_id)
    if not tasks:
        return None

    config = None
    config_path = os.path.join(TEAMS_DIR, team_id, "config.json")
    if os.path.isfile(config_path):
        config = _load_json(config_path)

    lines = []

    # Members
    if config and "members" in config:
        members = config["members"]
        names = ", ".join(m.get("name", "?") for m in members)
        lines.append(f"**Team** ({len(members)} members): {names}")
        lines.append("")

    # Progress bar
    total = len(tasks)
    completed = sum(1 for t in tasks if t.get("status") == "completed")
    pct = int(completed / total * 100) if total else 0
    bar_filled = int(pct / 5)
    bar = "█" * bar_filled + "░" * (20 - bar_filled)
    lines.append(f"**Progress:** {bar} {completed}/{total} ({pct}%)")
    lines.append("")

    # Task breakdown
    for t in tasks:
        status = t.get("status", "pending")
        subject = t.get("subject", f"Task {t.get('id', '?')}")
        active = t.get("activeForm", "")

        if status == "completed":
            lines.append(f"  ~~{subject}~~")
        elif status == "in_progress":
            detail = f" — *{active}*" if active else ""
            lines.append(f"  **▶ {subject}**{detail}")
        else:
            blocked = t.get("blockedBy", [])
            suffix = f" (blocked by {', '.join(str(b) for b in blocked)})" if blocked else ""
            lines.append(f"  ○ {subject}{suffix}")

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: team_status.py [list|status|tasks|card] [team_id]", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "list":
        cmd_list()
    elif cmd == "status":
        if len(sys.argv) < 3:
            print("Usage: team_status.py status <team_id>", file=sys.stderr)
            sys.exit(1)
        cmd_status(sys.argv[2])
    elif cmd == "tasks":
        if len(sys.argv) < 3:
            print("Usage: team_status.py tasks <team_id>", file=sys.stderr)
            sys.exit(1)
        cmd_tasks(sys.argv[2])
    elif cmd == "card":
        if len(sys.argv) < 3:
            print("Usage: team_status.py card <team_id>", file=sys.stderr)
            sys.exit(1)
        card = format_status_card(sys.argv[2])
        if card:
            print(card)
        else:
            print("No tasks found.", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
