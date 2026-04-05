#!/usr/bin/env python3
"""
Reproduce stale task notification leak + test re-query fix.

Uses a single background task for simplicity.
"""

import asyncio
import anyio
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    AssistantMessage, TextBlock, ResultMessage,
    TaskStartedMessage, TaskNotificationMessage,
    SystemMessage,
)


def msg_text(msg):
    if isinstance(msg, AssistantMessage):
        return "".join(b.text for b in msg.content if isinstance(b, TextBlock) and b.text)
    return ""


async def main():
    options = ClaudeAgentOptions(
        allowed_tools=["Read", "Glob", "Grep", "Bash"],
        permission_mode="bypassPermissions",
        cwd="/tmp",
    )

    async with ClaudeSDKClient(options=options) as client:
        # === Turn 1: spawn 1 background task ===
        print("=== Turn 1: spawn background task ===")
        await client.query(
            "Spawn a background agent (run_in_background=true) to run: sleep 30 && echo bg_done\n"
            "After spawning, immediately say 'Spawned.' and END YOUR TURN.\n"
            "Do NOT wait for the background task."
        )

        pending = set()
        async for msg in client.receive_response():
            mtype = type(msg).__name__
            if isinstance(msg, TaskStartedMessage):
                pending.add(msg.task_id)
                print(f"  {mtype} task={msg.task_id}")
            elif isinstance(msg, AssistantMessage):
                text = msg_text(msg)
                if text:
                    print(f"  {mtype} text={text[:60]!r}")
            elif isinstance(msg, ResultMessage):
                print(f"  {mtype} cost=${getattr(msg, 'total_cost_usd', 0):.4f}")
            else:
                print(f"  {mtype}")

        print(f"Pending: {pending}")
        if not pending:
            print("Model waited for task. Re-run.")
            return

        stale_ids = set(pending)
        print(f"\nWaiting 35s for background task to complete...")
        await asyncio.sleep(35)

        # === Turn 2: smart turn with re-query fix ===
        print("\n=== Turn 2: smart turn (detect stale → re-query) ===")

        user_prompt = "What is 7*8? Just answer with the number, nothing else."
        max_retries = 3

        for attempt in range(max_retries):
            label = f"attempt-{attempt + 1}"
            print(f"\n  [{label}] Sending query...")
            await client.query(user_prompt)

            found_stale = False
            result = None

            async for msg in client.receive_response():
                mtype = type(msg).__name__

                if isinstance(msg, TaskNotificationMessage):
                    if msg.task_id in stale_ids:
                        stale_ids.discard(msg.task_id)
                        found_stale = True
                        print(f"  [{label}] *** STALE {mtype} task={msg.task_id} — dropping this query ***")
                    else:
                        print(f"  [{label}] {mtype} task={msg.task_id}")

                elif isinstance(msg, AssistantMessage):
                    text = msg_text(msg)
                    if text:
                        if found_stale:
                            print(f"  [{label}] DROPPED AssistantMessage: {text[:60]!r}")
                        else:
                            result = text
                            print(f"  [{label}] AssistantMessage: {text[:60]!r}")

                elif isinstance(msg, ResultMessage):
                    cost = getattr(msg, "total_cost_usd", 0) or 0
                    if not found_stale and not result:
                        result = getattr(msg, "result", "") or ""
                    print(f"  [{label}] ResultMessage cost=${cost:.4f}")

                else:
                    print(f"  [{label}] {mtype}")

            if not found_stale:
                # Clean turn — we got a real answer
                print(f"\n  [{label}] Clean turn. Result: {result!r}")
                break
            else:
                print(f"  [{label}] Stale turn dropped. Retrying...")

        # Verify
        print(f"\nFinal result: {result!r}")
        if result and "56" in result:
            print("✓ FIX WORKS — correct answer after re-query!")
        elif result and "56" not in result:
            print(f"✗ Wrong answer: {result!r}")
        else:
            print("✗ No result")


if __name__ == "__main__":
    anyio.run(main)
