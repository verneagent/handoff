# handoff

`handoff` is a standalone agent skill for Claude Code and OpenCode.

It hands an active CLI session over to Lark so a human can temporarily leave the terminal without leaving the work, while preserving working context, session state, and the project thread.

## Why this skill exists

When a coding session becomes long, the weakest point is often not the model. It is the moment the human needs to step away. `handoff` exists so that stepping away from the terminal does not mean abandoning the same working thread.

What it gives you:

- continue the same collaboration thread from Lark on your phone
- temporarily step away from the terminal without leaving the work
- preserve project-level context instead of restarting from scratch
- keep the handoff flow explicit, inspectable, and scriptable
- support both Claude Code and OpenCode in one repository
- support sidecar mode, so the bot can join an existing Lark group where teammates already are
- support guest and coowner roles, so colleagues can help while the original operator is away

## Install

```bash
npx skills add -g verneagent/handoff
```

## Scope

- Supported: Claude Code, OpenCode
- Not positioned as a Codex skill

The hook and plugin model in this repository is built for Claude Code and OpenCode workflows.

## 中文说明

`handoff` 是一个独立的 agent skill，面向 Claude Code 和 OpenCode。

它解决的问题不是“让模型更聪明”，而是“当人临时离开终端时，如何不离开工作本身”。你可以把正在进行的 CLI 会话交接到 Lark，在手机上继续和 agent 协作，同时尽量保留原来的项目上下文和会话状态。

它的主要价值：

- 让同一条协作线程从终端延续到手机
- 临时离开终端时，不用把工作线程一起丢下
- 避免频繁重新解释上下文
- 交接过程是显式的、可检查的、可脚本化的
- 同时支持 Claude Code 和 OpenCode
- 支持 sidecar 模式，可以直接接入现有 Lark 群，把同事拉进来一起帮忙
- 支持 guest 和 coowner 角色，让其他人能接手协助，而不是所有操作都卡在单一 owner 身上

安装方式：

```bash
npx skills add -g verneagent/handoff
```

## License

MIT
