#!/usr/bin/env python3
"""Convenience wrapper for Step E (silence → tabs → card → wait).

Runs the standard startup sequence and then blocks in wait_for_reply.py so the
model cannot forget to enter the loop.
"""

import argparse
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import script_utils


def _restore_profile():
    try:
        script_utils.run_tool("restore iTerm profile", "iterm2_silence.py", "off")
    except Exception as exc:  # pragma: no cover - best effort
        print(f"[handoff] failed to restore iTerm profile: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Step E and wait for reply")
    parser.add_argument(
        "--session-model", required=True, help="Model id (e.g. gpt-5.1-codex)"
    )
    parser.add_argument(
        "--tab-url", default=None, help="Override tab URL passed to tabs-start"
    )
    parser.add_argument(
        "--timeout", type=int, default=None, help="Override wait_for_reply timeout"
    )
    parser.add_argument(
        "--skip-silence", action="store_true", help="Skip iterm2_silence on"
    )
    parser.add_argument("--skip-tabs", action="store_true", help="Skip tabs-start")
    parser.add_argument(
        "--skip-card", action="store_true", help="Skip send-status-card start"
    )
    parser.add_argument(
        "--no-ws", action="store_true", help="Force HTTP long-poll for wait_for_reply"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Override wait_for_reply backoff interval",
    )
    args = parser.parse_args()

    restore_needed = False
    try:
        if not args.skip_silence:
            script_utils.run_tool("silence on", "iterm2_silence.py", "on")
            restore_needed = True

        if not args.skip_tabs:
            tab_args = ["tabs-start", "--session-model", args.session_model]
            if args.tab_url:
                tab_args.extend(["--tab-url", args.tab_url])
            script_utils.run_tool("tabs-start", "handoff_ops.py", *tab_args)

        if not args.skip_card:
            script_utils.run_tool(
                "send status card",
                "handoff_ops.py",
                "send-status-card",
                "start",
                "--session-model",
                args.session_model,
            )

        wait_cmd = [sys.executable, script_utils.script_path("wait_for_reply.py")]
        if args.timeout is not None:
            wait_cmd.extend(["--timeout", str(args.timeout)])
        if args.interval is not None:
            wait_cmd.extend(["--interval", str(args.interval)])
        if args.no_ws:
            wait_cmd.append("--no-ws")

        proc = subprocess.run(wait_cmd)
        if proc.returncode != 0 and restore_needed:
            _restore_profile()
            restore_needed = False
        return proc.returncode
    except subprocess.CalledProcessError as exc:
        if restore_needed:
            _restore_profile()
            restore_needed = False
        print(
            f"[handoff] start_and_wait failed during '{' '.join(exc.cmd)}'",
            file=sys.stderr,
        )
        return exc.returncode or 1
    except Exception as exc:  # pragma: no cover - defensive
        if restore_needed:
            _restore_profile()
        print(f"[handoff] start_and_wait error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
