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

## Compared with OpenClaw skills

This repository is not trying to replace OpenClaw or ClawHub. It solves a different distribution problem.

OpenClaw's official skill flow is built around ClawHub and OpenClaw workspace/shared skill directories. That is a strong fit if your agent environment is centered on OpenClaw and you want registry-backed install, update, and sync flows.

This repository takes a different path:

- it is a plain GitHub repository, so the full source is visible before install
- it installs directly from a repo path with `npx skills add`
- it keeps one skill per repository, which makes ownership, issue tracking, and release history simpler
- it is aimed at Claude Code and OpenCode users who want a direct GitHub install path instead of an OpenClaw-specific registry flow

If you are already running OpenClaw and prefer ClawHub-managed installs, the OpenClaw path will feel more native. If you are operating in Claude Code or OpenCode and want a standalone public repo, this repository is the simpler fit.

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

和 OpenClaw 的区别，不是“谁更强”，而是分发方式不同：

- OpenClaw 官方更偏向 ClawHub 和 OpenClaw 自己的 workspace/shared skill 目录
- 这个仓库则是一个普通 GitHub 仓库，可以直接从 repo path 安装
- 对已经在 Claude Code / OpenCode 工作流里的用户，这种方式更直接
- 对希望用 ClawHub 做统一安装、更新、同步的 OpenClaw 用户，OpenClaw 官方路径会更自然

## License

MIT
