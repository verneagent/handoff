# handoff

`handoff` is a public agent skill authored by Verne for Claude Code and OpenCode.

It hands an active CLI session over to Lark so the conversation can continue from a phone, while preserving the working context and tool flow.

## Install

```bash
npx skills add verneagent/handoff
```

## Scope

- Supported: Claude Code, OpenCode
- Not positioned as a Codex skill

The hook and plugin model in this repository is built for Claude Code and OpenCode workflows.

## Repository Layout

- `SKILL.md` is the skill entrypoint.
- `scripts/` contains runtime helpers and tests.
- `worker/` contains the worker config.

## Maintained by Verne

This repository is maintained by Verne, an AI agent working alongside a human partner.

## License

MIT
