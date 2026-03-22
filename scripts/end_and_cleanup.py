#!/usr/bin/env python3
"""Mirror of Step 2 handback cleanup (status card → tabs → deactivate → silence)."""

import argparse
import json
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import handoff_config
import handoff_db
import lark_im
import script_utils


def _reset_working_state():
    """Update the 'Working...' card to 'Done ✓' and clear state/stop flag.

    Without this, handback leaves a stale 'Working...' card in the chat because
    end_and_cleanup sends via handoff_ops (not send_to_group.send()) which
    bypasses the normal reset path.
    """
    import fcntl
    import time as _time

    session_id = os.environ.get("HANDOFF_SESSION_ID", "")
    if not session_id:
        return

    tmp_dir = os.environ.get("HANDOFF_TMP_DIR", "/tmp/handoff")
    os.makedirs(tmp_dir, exist_ok=True)
    lock_path = os.path.join(tmp_dir, f"working-{session_id}.lock")

    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            msg_id = handoff_db.get_working_message(session_id)
            if msg_id:
                try:
                    _session = handoff_db.get_session(session_id)
                    _profile = _session.get("config_profile", "default") if _session else "default"
                    credentials = handoff_config.load_credentials(profile=_profile)
                    if credentials:
                        token = lark_im.get_tenant_token(
                            credentials["app_id"], credentials["app_secret"],
                        )
                        _, created_at, _ = handoff_db.get_working_state(session_id)
                        elapsed = int(_time.time()) - created_at if created_at else 0
                        if elapsed < 60:
                            body = f"Completed in {elapsed}s"
                        else:
                            mins = elapsed // 60
                            secs = elapsed % 60
                            body = f"Completed in {mins}m {secs}s"
                        done_card = lark_im.build_card("Done ✓", body=body, color="green")
                        lark_im.update_card_message(token, msg_id, done_card)
                except Exception:
                    pass
            handoff_db.clear_working_message(session_id)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)

    # Clear stop flag
    flag_path = os.path.join(tmp_dir, f"stop-{session_id}.flag")
    try:
        os.unlink(flag_path)
    except FileNotFoundError:
        pass


def _load_chat_id_from_deactivate(output: str) -> str:
    try:
        data = json.loads(output.strip() or "{}")
        return str(data.get("chat_id") or "")
    except json.JSONDecodeError:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run handback cleanup steps")
    parser.add_argument("--session-model", required=True)
    parser.add_argument("--body", default="", help="Override body for end status card")
    parser.add_argument(
        "--chat-id", default="", help="Chat id to reuse when deactivate is skipped"
    )
    parser.add_argument(
        "--dissolve", action="store_true", help="Also dissolve the chat group"
    )
    parser.add_argument("--skip-card", action="store_true")
    parser.add_argument("--skip-tabs", action="store_true")
    parser.add_argument("--skip-deactivate", action="store_true")
    parser.add_argument("--skip-silence", action="store_true")
    args = parser.parse_args()

    chat_id = args.chat_id

    try:
        # Reset working card to "Done ✓" before sending handback card
        _reset_working_state()

        if not args.skip_card:
            card_args = [
                "send-status-card",
                "end",
                "--session-model",
                args.session_model,
            ]
            if args.body:
                card_args.extend(["--body", args.body])
            script_utils.run_tool("send handback card", "handoff_ops.py", *card_args)

        if not args.skip_tabs:
            script_utils.run_tool(
                "tabs-end",
                "handoff_ops.py",
                "tabs-end",
                "--session-model",
                args.session_model,
            )

        if not args.skip_deactivate:
            result = script_utils.run_tool(
                "deactivate",
                "handoff_ops.py",
                "deactivate",
                capture=True,
            )
            chat_id = chat_id or _load_chat_id_from_deactivate(result.stdout)

        if args.dissolve:
            if not chat_id:
                raise RuntimeError(
                    "chat_id is required to dissolve (supply --chat-id when skipping deactivate)"
                )
            script_utils.run_tool(
                "remove-user", "handoff_ops.py", "remove-user", "--chat-id", chat_id
            )
            script_utils.run_tool(
                "dissolve-chat", "handoff_ops.py", "dissolve-chat", "--chat-id", chat_id
            )
            script_utils.run_tool(
                "cleanup-sessions",
                "handoff_ops.py",
                "cleanup-sessions",
                "--chat-id",
                chat_id,
            )

        if not args.skip_silence:
            script_utils.run_tool("restore iTerm profile", "iterm2_silence.py", "off")

        return 0
    except subprocess.CalledProcessError as exc:
        print(
            f"[handoff] end_and_cleanup failed during '{' '.join(exc.cmd)}'",
            file=sys.stderr,
        )
        return exc.returncode or 1
    except Exception as exc:  # pragma: no cover
        print(f"[handoff] end_and_cleanup error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
