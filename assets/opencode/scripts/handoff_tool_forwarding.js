const TOOL_SKIP_COMMANDS = [
  "send_to_group.py",
  "send_and_wait.py",
  "wait_for_reply.py",
  "handoff_ops.py",
  "on_notification.py",
  "on_post_tool_use.py",
  "on_session_start.py",
  "on_session_end.py",
  "permission_bridge.py",
  "permission_core.py",
  "iterm2_silence.py",
  "preflight.py",
  "lark_im.py",
]

export const TOOL_EVENT_TYPES = new Set([
  "tool.execute.after",
  "tool.execute.error",
  "tool.execute.failed",
  "tool.executed",
  "tool.error",
  "tool.failed",
])

export const EXT_LANG = {
  ".py": "python",
  ".js": "javascript",
  ".ts": "typescript",
  ".tsx": "tsx",
  ".jsx": "jsx",
  ".json": "json",
  ".yaml": "yaml",
  ".yml": "yaml",
  ".sh": "bash",
  ".zsh": "bash",
  ".rb": "ruby",
  ".rs": "rust",
  ".go": "go",
  ".java": "java",
  ".kt": "kotlin",
  ".swift": "swift",
  ".css": "css",
  ".scss": "scss",
  ".html": "html",
  ".xml": "xml",
  ".sql": "sql",
  ".md": "markdown",
  ".toml": "toml",
  ".c": "c",
  ".cpp": "cpp",
  ".h": "c",
  ".hpp": "cpp",
}

const truncate = (text, maxBody) =>
  text.length <= maxBody ? text : `${text.slice(0, maxBody)}\n... (truncated)`

export const firstString = (...values) => {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value
  }
  return ""
}

const firstNumber = (...values) => {
  for (const value of values) {
    if (typeof value === "number" && Number.isFinite(value)) return value
    if (typeof value === "string") {
      const parsed = Number(value)
      if (Number.isFinite(parsed)) return parsed
    }
  }
  return null
}

const isToolInfraCommand = (command) =>
  TOOL_SKIP_COMMANDS.some((skip) => command.includes(skip))

const relPath = (input) => {
  if (!input) return input
  const parts = input.split("/").filter(Boolean)
  if (parts.length >= 2) return `${parts[parts.length - 2]}/${parts[parts.length - 1]}`
  return parts[0] || input
}

export const langForPath = (input) => {
  if (!input) return ""
  const dot = input.lastIndexOf(".")
  if (dot < 0) return ""
  return EXT_LANG[input.slice(dot).toLowerCase()] || ""
}

const toolNameFromPayload = (payload) =>
  firstString(
    payload?.tool?.name,
    payload?.toolName,
    payload?.name,
    payload?.tool?.type,
  ).toLowerCase()

export const diffLinesByCount = (oldText, newText) => {
  const oldLines = oldText.split("\n")
  const newLines = newText.split("\n")

  const oldCounts = new Map()
  const newCounts = new Map()

  for (const line of oldLines) oldCounts.set(line, (oldCounts.get(line) || 0) + 1)
  for (const line of newLines) newCounts.set(line, (newCounts.get(line) || 0) + 1)

  const removed = []
  for (const line of oldLines) {
    const c = newCounts.get(line) || 0
    if (c > 0) newCounts.set(line, c - 1)
    else removed.push(line)
  }

  const added = []
  for (const line of newLines) {
    const c = oldCounts.get(line) || 0
    if (c > 0) oldCounts.set(line, c - 1)
    else added.push(line)
  }

  return { removed, added }
}

const formatBashToolEvent = (type, props, maxBody) => {
  const payload = props || {}
  const toolName = toolNameFromPayload(payload)
  if (toolName && toolName !== "bash") return null

  const command = firstString(
    payload?.input?.command,
    payload?.toolInput?.command,
    payload?.args?.command,
    payload?.command,
  )

  const description = firstString(
    payload?.input?.description,
    payload?.toolInput?.description,
    payload?.args?.description,
    payload?.description,
  )

  // Guard: bail on empty/falsy command (prevents blank error cards)
  if (!command) return null
  if (isToolInfraCommand(command)) return null

  const result = payload?.result || payload?.output || payload?.toolResponse || payload
  const stdout = firstString(result?.stdout, result?.out, result?.output)
  const stderr = firstString(result?.stderr, result?.err)
  const exitCode = firstNumber(result?.exitCode, result?.code, payload?.exitCode)
  const failed = type.includes("error") || type.includes("failed") || (exitCode !== null && exitCode !== 0)
  const error = firstString(payload?.error, result?.error, failed ? result?.message : "")

  const label = description || command || "bash"
  const title = `$ ${label.length > 80 ? `${label.slice(0, 77)}...` : label}`

  if (error) {
    return {
      title: "",
      body: `**${title}**\n\n<font color="red">Error:</font>\n\`\`\`\n${truncate(error, maxBody)}\n\`\`\``,
      color: "red",
    }
  }

  const output = [stdout, stderr].filter(Boolean).join("\n").trim()
  if (!output && !failed) return null

  if (failed) {
    return {
      title: "",
      body: `**${title}**${output ? `\n\n\`\`\`\n${truncate(output, maxBody)}\n\`\`\`` : ""}${exitCode !== null ? `\n\nExit code: ${exitCode}` : ""}`,
      color: "red",
    }
  }

  return {
    title: "",
    body: `**${title}**\n\n\`\`\`\n${truncate(output, maxBody)}\n\`\`\``,
    color: "grey",
  }
}

const formatEditToolEvent = (props, maxBody) => {
  const payload = props || {}
  const toolName = toolNameFromPayload(payload)
  if (toolName && toolName !== "edit") return null

  const input = payload?.input || payload?.toolInput || payload?.args || {}
  const filePath = firstString(input?.file_path, input?.filePath, input?.path)
  const oldString = firstString(input?.old_string, input?.oldString)
  const newString = firstString(input?.new_string, input?.newString)
  if (!filePath || (!oldString && !newString)) return null

  const { removed, added } = diffLinesByCount(oldString, newString)
  if (removed.length === 0 && added.length === 0) return null

  const lang = langForPath(filePath)
  const parts = [`**Edit: ${relPath(filePath)}**`]
  if (removed.length > 0) {
    parts.push(`<font color="red">Removed:</font>\n\`\`\`${lang}\n${removed.join("\n")}\n\`\`\``)
  }
  if (added.length > 0) {
    parts.push(`<font color="green">Added:</font>\n\`\`\`${lang}\n${added.join("\n")}\n\`\`\``)
  }

  return {
    title: "Edit",
    body: truncate(parts.join("\n\n"), maxBody),
    color: "grey",
  }
}

const formatWriteToolEvent = (props, maxBody) => {
  const payload = props || {}
  const toolName = toolNameFromPayload(payload)
  if (toolName && toolName !== "write") return null

  const input = payload?.input || payload?.toolInput || payload?.args || {}
  const filePath = firstString(input?.file_path, input?.filePath, input?.path)
  const content = firstString(input?.content)
  if (!filePath) return null

  const lines = content ? content.split("\n").length : 0
  return {
    title: "Write",
    body: truncate(`**Write: ${relPath(filePath)}**\nCreated file (${lines} ${lines === 1 ? "line" : "lines"})`, maxBody),
    color: "grey",
  }
}

export const formatToolEvent = (type, props, maxBody) =>
  formatBashToolEvent(type, props, maxBody) ||
  formatEditToolEvent(props, maxBody) ||
  formatWriteToolEvent(props, maxBody)
