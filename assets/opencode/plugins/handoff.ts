/**
 * Handoff plugin for opencode — bridges handoff mode to Lark.
 *
 * This plugin provides the same functionality as Claude Code's handoff hooks:
 * - Permission bridging: routes tool approval prompts to Lark during handoff
 * - Session lifecycle: notifies Lark when sessions start/end
 * - Notification forwarding: sends toast notifications to Lark during handoff
 * - Environment injection: sets HANDOFF_PROJECT_DIR, HANDOFF_SESSION_ID, and HANDOFF_SESSION_TOOL
 *
 * The main handoff loop (poll → process → respond) is handled by the
 * handoff skill (.opencode/skills/handoff/SKILL.md), not this plugin.
 *
 * Reuses Python scripts from .claude/skills/handoff/scripts/ for all
 * Lark API operations (send_to_group.py, wait_for_reply.py, lark_im.py).
 *
 * Permission bridging uses the event bus ("permission.asked") + SDK client
 * to respond programmatically, since the typed "permission.ask" hook does
 * not fire in the current opencode runtime (v1.2.4).
 */

import type { Plugin } from "@opencode-ai/plugin"
import { appendFileSync, existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "fs"
import { Database } from "bun:sqlite"
import { homedir } from "os"
import { basename } from "path"
import { TOOL_EVENT_TYPES, diffLinesByCount, firstString, formatToolEvent, langForPath } from "../scripts/handoff_tool_forwarding.js"

const HANDOFF_TMP_DIR = process.env.HANDOFF_TMP_DIR || "/tmp/handoff"
const LOG = `${HANDOFF_TMP_DIR}/handoff-plugin.log`
const IDLE_NUDGE_DEBOUNCE_MS = Number(process.env.HANDOFF_IDLE_NUDGE_MS || "15000")
const TOOL_FORWARD_MAX_BODY = Number(process.env.HANDOFF_FORWARD_MAX_BODY || "3000")
const TOOL_FORWARD_DEDUPE_MS = Number(process.env.HANDOFF_FORWARD_DEDUPE_MS || "8000")
const FILE_EDITED_FALLBACK_ENABLED = process.env.HANDOFF_FORWARD_FILE_EDITED === "1"
const TOOL_EVENT_DEBUG = process.env.HANDOFF_TOOL_EVENT_DEBUG === "1"

const safeJson = (value: unknown, max = 3000): string => {
  try {
    const serialized = JSON.stringify(value)
    if (!serialized) return ""
    return serialized.length <= max ? serialized : `${serialized.slice(0, max)}...(truncated)`
  } catch {
    return ""
  }
}

const pathFromProps = (props: any): string =>
  firstString(
    props?.path,
    props?.filePath,
    props?.filepath,
    props?.file,
    props?.file?.path,
    props?.newPath,
    props?.oldPath,
  )

const formatSessionDiffBody = (props: any): { title: string; body: string } | null => {
  const diffs = Array.isArray(props?.diff) ? props.diff : []
  if (diffs.length === 0) return null

  const blocks: string[] = []
  const shown = diffs.slice(0, 3)
  for (const d of shown) {
    const file = firstString(d?.file, "(unknown)")
    const lang = langForPath(file)
    const before = firstString(d?.before)
    const after = firstString(d?.after)
    const adds = typeof d?.additions === "number" ? d.additions : undefined
    const dels = typeof d?.deletions === "number" ? d.deletions : undefined
    const status = firstString(d?.status)

    const headerParts = [
      `**Edit: ${file}**`,
      status ? `status: \`${status}\`` : "",
      typeof adds === "number" || typeof dels === "number"
        ? `+${adds || 0} / -${dels || 0}`
        : "",
    ].filter(Boolean)

    const bodyParts = [headerParts.join(" · ")]
    const { removed, added } = diffLinesByCount(before, after)

    const MAX_SECTION_LINES = 20
    if (removed.length > 0) {
      const cut = removed.length > MAX_SECTION_LINES
      const code = cut
        ? `${removed.slice(0, MAX_SECTION_LINES).join("\n")}\n... (truncated ${removed.length - MAX_SECTION_LINES} lines)`
        : removed.join("\n")
      bodyParts.push(`<font color="red">Removed:</font>\n\`\`\`${lang}\n${code}\n\`\`\``)
    }
    if (added.length > 0) {
      const cut = added.length > MAX_SECTION_LINES
      const code = cut
        ? `${added.slice(0, MAX_SECTION_LINES).join("\n")}\n... (truncated ${added.length - MAX_SECTION_LINES} lines)`
        : added.join("\n")
      bodyParts.push(`<font color="green">Added:</font>\n\`\`\`${lang}\n${code}\n\`\`\``)
    }

    if (removed.length === 0 && added.length === 0) {
      bodyParts.push("(no content change)")
    }
    blocks.push(bodyParts.join("\n\n"))
  }

  if (diffs.length > shown.length) {
    blocks.push(`... and ${diffs.length - shown.length} more file diff(s)`)
  }

  return {
    title: "",
    body: blocks.join("\n\n"),
  }
}

const LOG_MAX_BYTES = 256 * 1024   // rotate when log exceeds 256 KB
const LOG_KEEP_BYTES = 128 * 1024  // keep newest 128 KB after rotation

const rotateLogIfNeeded = () => {
  try {
    const size = statSync(LOG).size
    if (size <= LOG_MAX_BYTES) return
    const data = readFileSync(LOG, "utf-8")
    writeFileSync(LOG, data.slice(data.length - LOG_KEEP_BYTES))
  } catch {}
}

const log = (msg: string) => {
  try {
    mkdirSync(HANDOFF_TMP_DIR, { recursive: true })
    appendFileSync(LOG, `[${new Date().toISOString()}] ${msg}\n`)
    rotateLogIfNeeded()
  } catch {}
}

export const handoff: Plugin = async ({ $, directory, worktree, client }) => {
  const projectDir = worktree || directory
  const sharedScripts = `${projectDir}/.claude/skills/handoff/scripts`
  const ocScripts = `${projectDir}/.opencode/scripts`

  log(`Plugin loaded. projectDir=${projectDir}`)

  // Track current session ID from lifecycle events.
  // Global is fine — opencode runs one session at a time per plugin instance.
  let currentSessionId = ""

  // Debounce for session.idle handoff continuation nudges
  let lastIdleNudge = 0
  // Set when handoff transitions from active → inactive (handback).
  // Once true, idle nudges are permanently suppressed for this plugin instance.
  let handedBack = false
  let lastKnownActive = false
  // Deduplicate noisy tool events emitted by multiple channels.
  const recentToolForwardKeys = new Map<string, number>()
  // Permanent set — same diff content is never forwarded twice per session.
  const seenSessionDiffKeys = new Set<string>()
  // Set when the server is shutting down — suppresses idle nudges
  let serverDisposing = false

  // Compute DB path matching lark_im._db_path().
  // Normalize: strip trailing slash and resolve to match Python's os.getcwd().
  // Path encoding: replace all "/" with "-" to get the project name.
  const normalizedDir = projectDir.replace(/\/+$/, "")
  const projectName = normalizedDir.replace(/\//g, "-")
  const dbPath = `${homedir()}/.handoff/projects/${projectName}/handoff-data.db`

  /**
   * Synchronously deactivate handoff by writing directly to the SQLite DB.
   * Used during shutdown when spawning Python subprocesses may fail.
   */
  const deactivateHandoffSync = (): string => {
    try {
      if (!existsSync(dbPath) || !currentSessionId) return ""
      const db = new Database(dbPath)
      try {
        db.run("PRAGMA wal_checkpoint(PASSIVE)")
        const row = db
          .query("SELECT chat_id FROM sessions WHERE session_id = ?")
          .get(currentSessionId) as any
        if (!row) return ""

        const chatId = row.chat_id || ""

        db.run("DELETE FROM sessions WHERE session_id = ?", currentSessionId)
        log("deactivateHandoffSync: removed active session row")
        return chatId
      } finally {
        db.close()
      }
    } catch (err: any) {
      log(`deactivateHandoffSync: error: ${err?.message || err}`)
      return ""
    }
  }

  /**
   * Run a Python snippet that imports from lark_im.
   * Sets HANDOFF_PROJECT_DIR so the DB path resolves correctly.
   */
  const pyLarkIm = (code: string) =>
    $`python3 -c ${code}`
      .env({ ...process.env, HANDOFF_PROJECT_DIR: projectDir })
      .cwd(projectDir)
      .quiet()
      .nothrow()

  /**
   * Build a human-readable description from tool name and input metadata.
   * Mirrors format_tool_description() in permission_bridge.py (Claude Code).
   */
  const formatToolDescription = (toolName: string, toolInput: Record<string, any>): string => {
    if (!toolInput || Object.keys(toolInput).length === 0) return toolName

    if (toolName === "Bash") {
      const cmd = String(toolInput.command || "")
      const desc = String(toolInput.description || "")
      const parts: string[] = []
      if (desc) parts.push(desc)
      if (cmd) {
        const display = cmd.length <= 200 ? cmd : cmd.slice(0, 200) + "..."
        parts.push(`\`${display}\``)
      }
      return parts.length > 0 ? parts.join("\n") : toolName
    }

    if (toolName === "Write" || toolName === "Edit" || toolName === "Read") {
      const path = String(toolInput.file_path || toolInput.filePath || "")
      if (path) return `File: \`${path}\``
    }

    // Generic: show key=value pairs, truncated
    const parts: string[] = []
    for (const [k, v] of Object.entries(toolInput)) {
      if (v === undefined || v === null) continue
      const sv = String(v)
      const display = sv.length <= 100 ? sv : sv.slice(0, 100) + "..."
      parts.push(`**${k}:** \`${display}\``)
    }
    return parts.length > 0 ? parts.slice(0, 5).join("\n") : toolName
  }

  const toolSummary = (toolName: string, toolInput: Record<string, any>): string => {
    const name = toolName.toLowerCase()
    if (name === "bash") {
      const label = firstString(toolInput?.description, toolInput?.command, "bash")
      const trimmed = label.length > 60 ? `${label.slice(0, 57)}...` : label
      return `\`$ ${trimmed}\``
    }
    if (name === "edit" || name === "write") {
      const path = firstString(toolInput?.file_path, toolInput?.filePath, toolInput?.path, "file")
      const title = name === "edit" ? "Edit" : "Write"
      return `\`${title}: ${basename(path)}\``
    }
    return `\`${toolName || "tool"}\``
  }

  const sendWorkingCard = async (sessionId: string, summary: string) => {
    if (!sessionId) return
    const trimmed = summary?.trim() || ""
    if (trimmed.toLowerCase().includes("waiting for next message")) return
    const content = trimmed || "Working..."
    try {
      await pyLarkIm(`
import sys
sys.path.insert(0, '${sharedScripts}')
import lark_im

session_id = ${JSON.stringify(sessionId)}
summary = ${JSON.stringify(content)}

if not session_id:
    raise SystemExit(0)

session = lark_im.get_session(session_id)
if not session:
    raise SystemExit(0)

creds = lark_im.load_credentials()
if not creds:
    raise SystemExit(0)

token = lark_im.get_tenant_token(creds['app_id'], creds['app_secret'])
card = lark_im.build_markdown_card(summary, title='Working hard...', color='grey')

existing = lark_im.get_working_message(session_id)
if existing:
    try:
        lark_im.update_card_message(token, existing, card)
        raise SystemExit(0)
    except Exception:
        pass

chat_id = session.get('chat_id')
if not chat_id:
    raise SystemExit(0)

msg_id = lark_im.send_message(token, chat_id, card)
lark_im.set_working_message(session_id, msg_id)
`)
        .text()
    } catch (err: any) {
      log(`sendWorkingCard: error: ${err?.message || err}`)
    }
  }

  /**
   * Check if this session has an active handoff.
   * Uses synchronous SQLite read (Bun native) instead of spawning Python,
   * which is faster and more reliable — especially during session.idle handling
   * where Python subprocesses can fail under load.
   */
  const checkHandoff = (sessionIdOverride?: string): { active: boolean; chatId: string; messageFilter: string } => {
    const sessionId = sessionIdOverride || currentSessionId
    if (!sessionId) {
      return { active: false, chatId: "", messageFilter: "concise" }
    }
    try {
      if (!existsSync(dbPath)) return { active: false, chatId: "", messageFilter: "concise" }
      const db = new Database(dbPath)
      try {
        // Force WAL checkpoint so we see the latest committed data from
        // Python writers (deactivate, etc.) — prevents stale reads.
        db.run("PRAGMA wal_checkpoint(PASSIVE)")
        const row = db
          .query(
            "SELECT s.chat_id, p.message_filter"
            + " FROM sessions s"
            + " LEFT JOIN chat_preferences p ON s.chat_id = p.chat_id"
            + " WHERE s.session_id = ?"
          )
          .get(sessionId) as any
        return row
          ? { active: true, chatId: row.chat_id || "", messageFilter: row.message_filter || "concise" }
          : { active: false, chatId: "", messageFilter: "concise" }
      } finally {
        db.close()
      }
    } catch (err: any) {
      log(`checkHandoff: error: ${err?.message || err}`)
      return { active: false, chatId: "", messageFilter: "concise" }
    }
  }

  /**
   * Send a markdown card to a handoff chat.
   */
  const sendMarkdownCard = async (
    chatId: string,
    message: string,
    title: string,
    color: "grey" | "blue" | "green" | "red" = "grey",
  ) => {
    await pyLarkIm(`
import sys
sys.path.insert(0, '${sharedScripts}')
import lark_im

chat_id = ${JSON.stringify(chatId)}
message = ${JSON.stringify(message)}
title = ${JSON.stringify(title)}
color = ${JSON.stringify(color)}

creds = lark_im.load_credentials()
if not creds or not chat_id:
    raise SystemExit(0)

try:
    token = lark_im.get_tenant_token(creds['app_id'], creds['app_secret'])
    card = lark_im.build_markdown_card(message, title=title, color=color)
    msg_id = lark_im.send_message(token, chat_id, card)
    lark_im.record_sent_message(msg_id, text=message, title=title, chat_id=chat_id)
except Exception:
    pass
`).text()
  }

  const shouldForwardToolEvent = (key: string): boolean => {
    const now = Date.now()
    for (const [existing, ts] of recentToolForwardKeys.entries()) {
      if (now - ts > TOOL_FORWARD_DEDUPE_MS) recentToolForwardKeys.delete(existing)
    }
    const last = recentToolForwardKeys.get(key)
    if (typeof last === "number" && now - last < TOOL_FORWARD_DEDUPE_MS) return false
    recentToolForwardKeys.set(key, now)
    return true
  }

  const forwardToolCard = async (type: string, props: any, sessionId?: string) => {
    const toolName = firstString(props?.tool?.name, props?.toolName, props?.name).toLowerCase()
    // Avoid duplicate edit cards when session.diff is also emitted.
    // We prefer session.diff for edit presentation because it carries file-level diff metadata.
    if (type === "tool.execute.after" && toolName === "edit") return

    const formatted = formatToolEvent(type, props, TOOL_FORWARD_MAX_BODY)
    if (!formatted) return

    const eventKey = firstString(
      props?.id,
      props?.tool?.callID,
      props?.callID,
      props?.messageID,
      `${type}:${formatted.title}:${formatted.body.slice(0, 120)}`,
    )
    if (!shouldForwardToolEvent(eventKey)) return

    const resolvedSessionId = sessionId || currentSessionId
    const { active, chatId, messageFilter } = checkHandoff(resolvedSessionId)
    if (!active || !chatId) return

    const toolInput =
      props?.input ||
      props?.toolInput ||
      props?.args ||
      props?.tool?.input ||
      {}
    const summary = toolSummary(toolName, toolInput)

    // Message filter: concise = no forwarding, important = edit/write only
    if (messageFilter === "concise") {
      await sendWorkingCard(resolvedSessionId, summary)
      return
    }
    const isError = type.includes("error") || type.includes("failed")
    if (messageFilter === "important" && toolName === "bash" && !isError) {
      await sendWorkingCard(resolvedSessionId, summary)
      return
    }

    await sendMarkdownCard(chatId, formatted.body, formatted.title, formatted.color)
  }

  /**
   * Respond to a permission request via SDK client.
   */
  const respondPermission = async (
    sessionId: string,
    permId: string,
    response: "once" | "always" | "reject",
  ) => {
    try {
      await client.postSessionIdPermissionsPermissionId({
        path: { id: sessionId, permissionID: permId },
        body: { response },
      })
      log(`respondPermission: sent "${response}" for ${permId}`)
    } catch (err: any) {
      log(`respondPermission: error: ${err?.message || err}`)
    }
  }

  /**
   * Bridge a permission request to Lark and respond via SDK.
   * Called when a "permission.asked" event fires during active handoff.
   *
   * The event uses the v2 PermissionRequest schema:
   *   { id, sessionID, permission, patterns, metadata, always, tool }
   * NOT the v1 Permission type (which has type, title, pattern).
   */
  const bridgePermission = async (props: any) => {
    const permId = props?.id
    const sessionId = props?.sessionID || currentSessionId
    // v2 field names: "permission" (not "type"), no "title"
    const permission = props?.permission || "unknown"
    const patterns: string[] = props?.patterns || []
    const always: string[] = props?.always || []
    const metadata = props?.metadata || {}

    log(`bridgePermission: id=${permId}, session=${sessionId}, perm=${permission}, patterns=${JSON.stringify(patterns)}, always=${JSON.stringify(always)}`)

    if (!permId || !sessionId) {
      log("bridgePermission: missing permId or sessionId, skipping")
      return
    }

    // During handoff, ALL permissions go through Lark — even ones in the
    // "always" list. This matches Claude Code's behavior where the user
    // explicitly chose to route everything through Lark.

    // Build a human-readable description from tool metadata
    const message = formatToolDescription(permission, metadata)

    // Run permission_bridge.py to send card to Lark and poll for decision
    const result = await $`python3 ${ocScripts}/permission_bridge.py`
      .env({
        ...process.env,
        HANDOFF_PROJECT_DIR: projectDir,
        HANDOFF_TOOL_TYPE: permission,
        HANDOFF_TOOL_MESSAGE: message,
        HANDOFF_SESSION_ID: sessionId,
      })
      .cwd(projectDir)
      .quiet()
      .nothrow()
      .text()

    const decision = result.trim()
    log(`bridgePermission: decision="${decision}"`)

    if (decision === "ask") {
      // Not in handoff mode — don't interfere.
      // The TUI prompt is showing; if user is at CLI they can respond there.
      log(`bridgePermission: not in handoff, skipping`)
      return
    }

    if (decision === "") {
      // Script errored or returned empty — fail closed by denying.
      log(`bridgePermission: empty output, denying to fail closed`)
      await respondPermission(sessionId, permId, "reject")
      return
    }

    // Map decision to SDK response
    let response: "once" | "always" | "reject"
    if (decision === "always") {
      response = "always"
    } else if (decision === "allow") {
      response = "once"
    } else {
      response = "reject"
    }

    await respondPermission(sessionId, permId, response)
  }

  return {
    // Inject environment variables so Python scripts resolve paths correctly
    "shell.env": async (_input, output) => {
      output.env.HANDOFF_PROJECT_DIR = projectDir
      output.env.HANDOFF_SESSION_TOOL = "OpenCode"
      output.env.HANDOFF_SESSION_ID = currentSessionId
    },

    // Typed hook — kept as fallback in case future opencode versions fire it
    "permission.ask": async (input, output) => {
      log(`permission.ask HOOK FIRED: type=${input.type}, title=${input.title}, sessionID=${input.sessionID}`)

      const sessionId = input.sessionID || currentSessionId
      const { active } = checkHandoff(sessionId)
      if (!active) {
        log(`permission.ask: handoff not active, skipping`)
        return
      }

      const result = await $`python3 ${ocScripts}/permission_bridge.py`
        .env({
          ...process.env,
          HANDOFF_PROJECT_DIR: projectDir,
          HANDOFF_TOOL_TYPE: input.type,
          HANDOFF_TOOL_MESSAGE: input.title || "",
          HANDOFF_SESSION_ID: sessionId,
        })
        .cwd(projectDir)
        .quiet()
        .nothrow()
        .text()

      const decision = result.trim()
      log(`permission.ask: decision="${decision}"`)

      if (decision === "allow" || decision === "always") {
        output.status = "allow"
      } else if (decision === "deny") {
        output.status = "deny"
      }
      // "ask" = not in handoff, let TUI handle it (default)
    },

    // Documented hook for completed tool executions.
    // Prefer this over event bus when runtimes do not emit tool.* events.
    "tool.execute.after": async (input, output) => {
      const props = {
        tool: { name: input.tool, callID: input.callID },
        input: input.args,
        result: {
          output: output.output,
          ...(output.metadata || {}),
        },
      }
      await forwardToolCard("tool.execute.after", props, input.sessionID)
    },

    // Handle lifecycle and permission events
    event: async ({ event }) => {
      const type = event.type as string

      // Only log non-noisy events
      if (
        !type.startsWith("message.part.") &&
        !type.startsWith("message.updated") &&
        type !== "session.status" &&
        type !== "session.diff"
      ) {
        log(`event: ${type}`)
      }

      // Always log permission events for debugging
      if (type.includes("permission")) {
        const props = (event as any).properties
        log(`PERMISSION EVENT: ${type} ${safeJson(props)}`)
      }

      if (TOOL_EVENT_DEBUG && (type.includes("tool") || type.startsWith("message.part.tool") || type === "file.edited")) {
        const props = (event as any).properties
        log(`tool-event-debug: ${type} ${safeJson(props)}`)
      }

      if (type === "session.diff") {
        const props = (event as any).properties || {}
        const sessionId = firstString(props?.sessionID, props?.sessionId)
        const { active, chatId, messageFilter } = checkHandoff(sessionId)
        if (!active || !chatId) return
        // session.diff = edit diffs — show for verbose and important
        if (messageFilter === "concise") return

        const formatted = formatSessionDiffBody(props)
        if (!formatted) return

        const eventKey = `${sessionId}:${safeJson(props?.diff, 600)}`
        if (seenSessionDiffKeys.has(eventKey)) return
        seenSessionDiffKeys.add(eventKey)

        const body = formatted.body.length <= TOOL_FORWARD_MAX_BODY
          ? formatted.body
          : `${formatted.body.slice(0, TOOL_FORWARD_MAX_BODY)}\n... (truncated)`
        await sendMarkdownCard(chatId, body, formatted.title, "grey")
        return
      }

      // Permission requested — bridge to Lark
      // Only handle "permission.asked" (new prompt needing response).
      // "permission.updated" / "permission.replied" are informational — skip.
      if (type === "permission.asked") {
        const props = (event as any).properties
        log(`${type} properties: ${JSON.stringify(props)}`)

        const { active } = checkHandoff(props?.sessionID)
        if (!active) {
          log(`${type}: handoff not active, skipping bridge`)
          return
        }

        // Bridge asynchronously — don't block the event bus
        bridgePermission(props).catch((err) => {
          log(`bridgePermission error: ${err?.message || err}`)
        })
        return
      }

      // Fallback: some runtimes emit file.edited instead of tool events.
      if (FILE_EDITED_FALLBACK_ENABLED && type === "file.edited") {
        const props = (event as any).properties || {}
        const sessionId = firstString(props?.sessionID, props?.sessionId)
        const { active, chatId, messageFilter } = checkHandoff(sessionId)
        if (!active || !chatId) return
        // file.edited = edit diffs — show for verbose and important
        if (messageFilter === "concise") return

        const editedPath = pathFromProps(props)
        const message = editedPath
          ? `**Edit: ${editedPath}**\n\nSource event: \`file.edited\``
          : `**Edit detected**\n\nSource event: \`file.edited\`\n\n\`\`\`json\n${safeJson(props, 1200)}\n\`\`\``
        await sendMarkdownCard(chatId, message, "Edit", "grey")
        return
      }

      if (TOOL_EVENT_TYPES.has(type)) {
        const props = (event as any).properties || {}
        const sessionId = firstString(props?.sessionID, props?.sessionId)
        await forwardToolCard(type, props, sessionId)
        return
      }

      // Track session ID
      if (type === "session.created") {
        const props = (event as any).properties
        currentSessionId = props?.info?.id || props?.id || ""
        log(`session.created: id=${currentSessionId}`)
        return
      }

      // Session or server ended — clean up handoff if this session owned it.
      // opencode fires "server.instance.disposed" on exit, not always
      // "session.deleted", so we handle both.
      if (type === "session.deleted" || type === "server.instance.disposed") {
        if (type === "server.instance.disposed") serverDisposing = true

        // Use synchronous SQLite deactivation — spawning Python may fail
        // during shutdown since BunShell subprocess support is gone.
        const endedChatId = deactivateHandoffSync()
        if (!endedChatId) return

        // Best-effort async cleanup (may fail during shutdown)
        try {
          await $`python3 ${sharedScripts}/iterm2_silence.py off`
            .quiet()
            .nothrow()
        } catch {}

        try {
          await pyLarkIm(`
import sys
sys.path.insert(0, '${sharedScripts}')
import lark_im

chat_id = '${endedChatId}'
msg = 'The opencode session has ended. Start a new session and use /handoff to reconnect.'

creds = lark_im.load_credentials()
if not creds or not chat_id:
    raise SystemExit(0)

try:
    token = lark_im.get_tenant_token(creds['app_id'], creds['app_secret'])
    card = lark_im.build_markdown_card(msg, title='Session Ended', color='grey')
    msg_id = lark_im.send_message(token, chat_id, card)
    lark_im.record_sent_message(msg_id, text=msg, title='Session Ended', chat_id=chat_id)
except Exception:
    pass
`).text()
        } catch {}

        log(`Handoff cleaned up on ${type}`)
        return
      }

      // Session went idle during active handoff — the LLM may have finished
      // its turn without looping back to wait_for_reply.py (e.g. after a
      // permission denial). Nudge it to continue the handoff loop.
      if (type === "session.idle") {
        if (serverDisposing) {
          log("session.idle: skipped (server disposing)")
          return
        }
        if (handedBack) {
          log("session.idle: skipped (handedBack — handoff was deactivated)")
          return
        }
        const now = Date.now()
        if (now - lastIdleNudge < IDLE_NUDGE_DEBOUNCE_MS) {
          log("session.idle: skipped (debounce)")
          return
        }
        const { active, chatId } = checkHandoff()
        if (!active) {
          if (lastKnownActive) {
            handedBack = true
            log("session.idle: handoff deactivated — suppressing future nudges")
          }
          lastKnownActive = false
          log(`session.idle: skipped (handoff not active, sessionId=${currentSessionId})`)
          return
        }
        lastKnownActive = true

        lastIdleNudge = now
        log("session.idle during handoff — sending continuation prompt")
        try {
          await client.session.promptAsync({
            path: { id: currentSessionId },
            body: {
              parts: [{
                type: "text" as const,
                text: [
                  "CONTINUE HANDOFF — Session is idle but handoff remains active. KEEP WAITING:",
                  "",
                  "IMPORTANT: Do NOT end handoff. The user is still on Lark, just temporarily idle.",
                  "           Simply continue the loop below. Do NOT check session status or handback.",
                  "",
                  "**ABSOLUTE RULE**: On timeout, immediately re-run wait_for_reply.py WITHOUT sending",
                  "                 any status message. Stay silent during idle periods.",
                  "",
                  "1. If you have a pending response to send, send it AND wait for next message:",
                  "   python3 .claude/skills/handoff/scripts/send_and_wait.py '<your response>'",
                  "   (Bash timeout: 600000)",
                  "",
                  "   If nothing to send, just wait (SILENTLY — no status messages on timeout):",
                  "   python3 .claude/skills/handoff/scripts/wait_for_reply.py --timeout 0",
                  "   (Bash timeout: 600000)",
                  "",
                  "2. Process the user's request (read/edit files, run commands, etc.)",
                  "",
                  "3. Send your response AND wait via send_and_wait.py, then go to step 2.",
                  "",
                  "RULES:",
                  "- NEVER use AskUserQuestion (user is on Lark, not CLI).",
                  "- If 'handback' received, exit handoff cleanly.",
                  "- Do NOT re-run handoff entry steps (discover/create group/activate). Resume ONLY the wait → process → respond loop.",
                  "- If handoff was already handed back, STOP. Do not re-enter handoff.",
                  "Read .claude/skills/handoff/SKILL.md for the full protocol.",
                ].join("\n"),
              }],
            },
          })
          log("session.idle: continuation prompt sent")
        } catch (err: any) {
          log(`session.idle: prompt failed: ${err?.message || err}`)
        }
        return
      }

      // Forward toast notifications to Lark during handoff
      if (type === "tui.toast.show") {
        const { active, chatId } = checkHandoff()
        if (!active || !chatId) return

        const props = (event as any).properties || {}
        const message = props.message || "Notification"
        
        // Use red for critical notifications (quota, rate limit, errors)
        const lowerMsg = message.toLowerCase()
        const isCritical = lowerMsg.includes("quota") || 
                          lowerMsg.includes("limit") || 
                          lowerMsg.includes("usage") ||
                          lowerMsg.includes("exceeded") ||
                          lowerMsg.includes("rate limit")
        const color = isCritical ? "red" : "blue"

        await sendMarkdownCard(chatId, message, "Notification", color)
      }
    },
  }
}
