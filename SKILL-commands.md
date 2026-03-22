# Handoff Sub-commands

Reference for all `/handoff` sub-command implementations. Read by `SKILL.md` on demand.

Prefer deterministic helpers in `python3 .claude/skills/handoff/scripts/handoff_ops.py ...` over inline snippets.

## List Groups (`/handoff chats`)

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py list-groups --scope user
```

Print as a formatted table. Do NOT enter Handoff mode.

## List All Groups (`/handoff chats_admin`)

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py list-groups --scope all
```

Print as a formatted table. Do NOT enter Handoff mode.

## Status (`/handoff status`)

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py status
```

Print workspace status in a fixed pretty format (workspace, DB, groups, session details). Use `--format json` for machine-readable output. Do NOT enter Handoff mode.

## Delete Group (`/handoff delete_admin [group name]`)

**Guard:** Cannot run during handoff mode. Refuse and ask user to send `handback` first.

1. Discover candidate groups with `list-groups --scope user`.
2. Filter by provided group name substring (case-insensitive) if given.
3. Ask for confirmation / selection.
4. For each selected chat:

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py dissolve-chat --chat-id '<CHAT_ID>'
python3 .claude/skills/handoff/scripts/handoff_ops.py cleanup-sessions --chat-id '<CHAT_ID>'
```

Print summary and stop.

## Purge Empty Groups (`/handoff purge_admin`)

**Guard:** Cannot run during handoff mode (ask user to send `handback` first).

1. Discover empty groups:

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py find-empty-groups
```

2. If none: report and stop.
3. Ask for confirmation.
4. For each selected chat:

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py dissolve-chat --chat-id '<CHAT_ID>'
python3 .claude/skills/handoff/scripts/handoff_ops.py cleanup-sessions --chat-id '<CHAT_ID>'
```

Print summary and stop.

## Deinit (`/handoff deinit`)

Reverse of `/handoff init`: collect all decisions upfront, confirm once, then execute in one batch.

1. **Detect installed components:**
   - **Claude Code hooks:** Scan `.claude/settings.json` and `.claude/settings.local.json` for hook entries whose `command` contains `handoff/scripts/`. Note which files have them.
   - **OpenCode plugin files:** Check whether `.opencode/plugins/handoff.ts`, `.opencode/scripts/permission_bridge.py`, `.opencode/scripts/handoff_tool_forwarding.js` exist.

2. **Collect decisions** (ask all questions before doing anything):
   - If Claude Code hooks found → ask whether to remove them (default: Yes)
   - If OpenCode plugin files found → ask whether to remove them (default: Yes)
   - Ask: "Also delete `~/.handoff/config.json`?" (default: No)
   - Ask: "Also delete the handoff skill itself (`.claude/skills/handoff/`)?" (default: No)

3. **Confirm once.** Show a summary of what will be removed and ask the user to confirm before proceeding.

4. **Execute in one batch:**

   - **Remove Claude Code hooks:** For each settings file with handoff hooks, remove only entries whose `command` contains `handoff/scripts/`. Leave non-handoff entries untouched. Use the `Edit` tool.
   - **Remove OpenCode plugin files** (restore to skill assets first, then delete):
     ```bash
     SKILL=".claude/skills/handoff"
     mkdir -p "$SKILL/assets/opencode/plugins" "$SKILL/assets/opencode/scripts"
     cp .opencode/plugins/handoff.ts "$SKILL/assets/opencode/plugins/"
     cp .opencode/scripts/permission_bridge.py "$SKILL/assets/opencode/scripts/"
     cp .opencode/scripts/handoff_tool_forwarding.js "$SKILL/assets/opencode/scripts/"
     rm -f .opencode/plugins/handoff.ts \
           .opencode/scripts/permission_bridge.py \
           .opencode/scripts/handoff_tool_forwarding.js
     ```
   - **Delete config** (if selected):
     ```bash
     python3 .claude/skills/handoff/scripts/handoff_ops.py deinit-config
     ```
   - **Delete skill** (if selected):
     ```bash
     rm -rf .claude/skills/handoff
     ```

5. Print a summary of what was removed and stop.

## Clear (`/handoff clear`)

Deletes current project's chat group(s) and handoff DB.

1. Confirm with user.
2. Run deterministic clear:

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py clear-project
```

3. Print summary and stop.

## Diagnostic (`/handoff diag`)

Tests the permission bridge end-to-end: sends a card with Approve/Deny buttons, polls for the user's click, and reports whether the round-trip works.

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py diag --mode ws --timeout 60
```

Options:
- `--mode ws` (default): Poll via WebSocket only
- `--mode http`: Poll via HTTP long-poll only
- `--mode both`: Try WebSocket first, fall back to HTTP
- `--chat-id <ID>`: Target a specific chat (auto-detected if omitted)
- `--timeout <N>`: Max seconds to wait for a button click (default: 60, only used for HTTP)

Outputs JSON with a `steps` array showing each stage (credentials, worker, ack, send_card, poll). The `ok` field indicates overall success. If the poll step fails, the card action callback may not be configured in the Lark app, or the poll method may have issues (compare `--mode ws` vs `--mode http`).

This MUST run with `dangerouslyDisableSandbox: true` (Claude Code only — opencode has no sandbox). Print the JSON output to the user. Do NOT enter Handoff mode.

## Profile Commands

### List Profiles (`/handoff profile`)

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py profile-list
```

Outputs JSON with `profiles` (sorted list), `default_profile`, and `current_profile`. Print as a formatted list. Do NOT enter Handoff mode.

### Show Profile (`/handoff profile show`)

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py profile-show
```

Show the current profile name and its config file path. Use `--profile <name>` to inspect a specific profile.

### Set Default Profile (`/handoff profile set-default <name>`)

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py profile-set-default <NAME>
```

Writes the default profile to `~/.handoff/default_profile`. The default profile is used when no `--profile` argument or `HANDOFF_PROFILE` env var is set.

### Using profiles with other commands

All commands that load credentials support `--profile <name>`:

```bash
# Enter handoff with a specific profile
python3 .claude/skills/handoff/scripts/enter_handoff.py --session-model opus --profile work

# Run preflight with a specific profile
python3 .claude/skills/handoff/scripts/preflight.py --profile work

# Activate with a specific profile
python3 .claude/skills/handoff/scripts/handoff_ops.py --profile work activate --chat-id <ID> --session-model opus
```

Profile config files:
- `default` profile: `~/.handoff/config.json`
- Named profiles: `~/.handoff/profiles/<name>.json`

Profile resolution order: explicit `--profile` arg > `HANDOFF_PROFILE` env var > `~/.handoff/default_profile` file > `"default"`.

## Test Commands

- Log health check (plugin + permission bridge):

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py log-check --lines 4000
```

Recent-window check (best effort):

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py log-check --lines 4000 --since-minutes 30
```

- Single CI-friendly command (syntax + unit + simulation):

```bash
python3 .claude/skills/handoff/scripts/run_tests.py
```

- Unit + simulation tests only:

```bash
python3 -m unittest discover -s .claude/skills/handoff/scripts/tests -p 'test_*.py'
```

- Syntax check for scripts + tests:

```bash
python3 -m py_compile .claude/skills/handoff/scripts/*.py .claude/skills/handoff/scripts/tests/test_*.py
```
