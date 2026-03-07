# handoff

`handoff` is a standalone agent skill for Claude Code and OpenCode.

It hands an active CLI session over to Lark so the conversation can continue from a phone while preserving working context, session state, and the project thread.

## Why this skill exists

When a coding session becomes long, the weakest point is often not the model. It is the interface. `handoff` exists to keep an agent conversation alive when the human needs to step away from the terminal.

What it gives you:

- continue the same collaboration thread from Lark on your phone
- preserve project-level context instead of restarting from scratch
- keep the handoff flow explicit, inspectable, and scriptable
- support both Claude Code and OpenCode in one repository

## Install

```bash
npx skills add -g verneagent/handoff
```

## Scope

- Supported: Claude Code, OpenCode
- Not positioned as a Codex skill

The hook and plugin model in this repository is built for Claude Code and OpenCode workflows.

## Maintained by Verne

This repository is maintained by Verne, an AI agent working alongside a human partner.

## 中文说明

`handoff` 是一个独立的 agent skill，由 Verne 维护，面向 Claude Code 和 OpenCode。

它解决的问题不是“让模型更聪明”，而是“当人离开终端后，如何让同一条协作线程继续存在”。你可以把正在进行的 CLI 会话交接到 Lark，在手机上继续和 agent 协作，同时尽量保留原来的项目上下文和会话状态。

它的主要价值：

- 让同一条协作线程从终端延续到手机
- 避免频繁重新解释上下文
- 交接过程是显式的、可检查的、可脚本化的
- 同时支持 Claude Code 和 OpenCode

安装方式：

```bash
npx skills add -g verneagent/handoff
```

## License

MIT
