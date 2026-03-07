#!/usr/bin/env python3
"""Toggle iTerm2 terminal notifications by switching profiles.

Usage:
    python3 iterm2_silence.py on     # Switch to silent profile
    python3 iterm2_silence.py off    # Restore original profile

Uses iTerm2's proprietary escape sequence to switch profiles per-session.
Requires the Handoff Silent dynamic profile to be installed at:
  ~/Library/Application Support/iTerm2/DynamicProfiles/handoff-silent.json
"""

import os
import sys

import handoff_config

SILENT_PROFILE = "Handoff Silent"

# Dynamic profile path and definition — auto-installed on first use.
_DYNAMIC_PROFILE_DIR = os.path.expanduser(
    "~/Library/Application Support/iTerm2/DynamicProfiles"
)
_DYNAMIC_PROFILE_PATH = os.path.join(_DYNAMIC_PROFILE_DIR, "handoff-silent.json")
_DYNAMIC_PROFILE = {
    "Profiles": [
        {
            "Name": SILENT_PROFILE,
            "Guid": "handoff-silent-profile-001",
            "Dynamic Profile Parent Name": "Default",
            "Silence Bell": True,
            "BounceIconInDock": False,
            "Visual Bell": False,
            "BM Growl": False,
        }
    ]
}


def _ensure_dynamic_profile():
    """Create or update the iTerm2 dynamic profile for silent mode."""
    import json as _json

    os.makedirs(_DYNAMIC_PROFILE_DIR, exist_ok=True)
    expected = _json.dumps(_DYNAMIC_PROFILE, indent=2) + "\n"
    try:
        with open(_DYNAMIC_PROFILE_PATH) as f:
            if f.read() == expected:
                return  # Already up-to-date
    except FileNotFoundError:
        pass
    with open(_DYNAMIC_PROFILE_PATH, "w") as f:
        f.write(expected)


def _state_file():
    """Return session-scoped state file path for the original profile name."""
    session_id = os.environ.get("HANDOFF_SESSION_ID", "")
    suffix = f"-{session_id}" if session_id else ""
    return os.path.join(
        handoff_config.HANDOFF_HOME,
        f"iterm2-original-profile{suffix}",
    )


def switch_profile(name):
    """Send iTerm2 escape sequence to switch the current session's profile."""
    sys.stdout.write(f"\033]1337;SetProfile={name}\a")
    sys.stdout.flush()


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("on", "off"):
        print("Usage: iterm2_silence.py on|off", file=sys.stderr)
        sys.exit(1)

    action = sys.argv[1]

    state_file = _state_file()

    if action == "on":
        _ensure_dynamic_profile()
        # Save original profile name before switching
        original = os.environ.get("ITERM_PROFILE", "Default")
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w") as f:
            f.write(original)
        switch_profile(SILENT_PROFILE)
        print(f"Switched to silent profile (was: {original})")

    elif action == "off":
        # Restore original profile
        original = "Default"
        try:
            with open(state_file) as f:
                original = f.read().strip() or "Default"
            os.remove(state_file)
        except FileNotFoundError:
            pass
        switch_profile(original)
        print(f"Restored profile: {original}")


if __name__ == "__main__":
    main()
