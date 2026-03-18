// --- Durable Object: HandoffSession ---
// Keyed by routing key (e.g. "chat:oc_xxx" or a root message ID).
// Stores replies in-memory (persisted to DO storage) and supports
// WebSocket (preferred) and HTTP long-polling for client notification.
//
// Uses the Hibernation API for WebSocket: the DO can hibernate while
// WebSocket connections stay open. CF runtime handles protocol-level
// keepalive. The DO wakes on push (HTTP) or client message (WebSocket).
//
// Stale reply cleanup: replies older than 72 hours are automatically
// purged via the Alarm API. An alarm is scheduled on each push and
// re-scheduled after cleanup if replies remain.

const REPLY_TTL_MS = 72 * 60 * 60 * 1000; // 72 hours

export class HandoffSession {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.replies = [];
    this.waitingPolls = []; // HTTP long-poll backward compat
    this.takeover = false;

    // Load persisted state — blocks all fetch/WS events until done.
    // Works correctly across hibernation (constructor re-runs on wake).
    state.blockConcurrencyWhile(async () => {
      this.replies = (await state.storage.get("replies")) || [];
      this.takeover = (await state.storage.get("takeover")) || false;
    });
  }

  async fetch(request) {
    const url = new URL(request.url);

    // --- WebSocket upgrade (Hibernation API) ---
    if ((request.headers.get("Upgrade") || "").toLowerCase() === "websocket") {
      return this.handleWebSocketUpgrade(url);
    }

    if (request.method === "POST" && url.pathname === "/push") {
      let reply;
      try {
        reply = await request.json();
      } catch (e) {
        return Response.json({ error: "bad request" }, { status: 400 });
      }
      this.replies.push(reply);
      await this.state.storage.put("replies", this.replies);
      // Schedule cleanup alarm if none is pending
      const alarm = await this.state.storage.getAlarm();
      if (!alarm) {
        await this.state.storage.setAlarm(Date.now() + REPLY_TTL_MS);
      }
      // Wake HTTP long-polls
      for (const entry of this.waitingPolls) entry.resolve();
      this.waitingPolls = [];
      // Push to WebSocket clients
      this.broadcastWebSocket({ replies: [reply], count: 1 });
      return Response.json({ ok: true });
    }

    if (request.method === "GET" && url.pathname === "/poll") {
      return this.handlePoll(url);
    }

    if (request.method === "GET" && url.pathname === "/get") {
      return this.handleGet(url);
    }

    if (request.method === "POST" && url.pathname === "/takeover") {
      this.takeover = true;
      await this.state.storage.put("takeover", true);
      // Wake HTTP long-polls (they re-check this.takeover and clear it)
      for (const entry of this.waitingPolls) entry.resolve();
      this.waitingPolls = [];
      // Push to WebSocket clients (they receive it instantly).
      // Don't clear the flag here — let consumers (handlePoll,
      // handleWebSocketUpgrade) clear it when they consume it.
      this.broadcastWebSocket({ replies: [], count: 0, takeover: true });
      return Response.json({ ok: true });
    }

    if (request.method === "POST" && url.pathname === "/ack") {
      const before = url.searchParams.get("before");
      if (!before) {
        return Response.json({ error: "missing before" }, { status: 400 });
      }
      const oldLen = this.replies.length;
      // Note: strict > filter removes replies with create_time <= before (inclusive).
      // Replies with create_time > before (strictly newer) are kept for next poll.
      this.replies = this.replies.filter((r) => r.create_time > before);
      await this.state.storage.put("replies", this.replies);
      return Response.json({
        removed: oldLen - this.replies.length,
        remaining: this.replies.length,
      });
    }

    return new Response("Not found", { status: 404 });
  }

  // --- WebSocket ---

  async handleWebSocketUpgrade(url) {
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);

    this.state.acceptWebSocket(server);

    const since = url.searchParams.get("since") || "";

    if (this.takeover) {
      this.takeover = false;
      await this.state.storage.delete("takeover");
      server.send(JSON.stringify({ replies: [], count: 0, takeover: true }));
    } else {
      const filtered = since
        ? this.replies.filter((r) => r.create_time > since)
        : this.replies;
      if (filtered.length > 0) {
        server.send(
          JSON.stringify({ replies: filtered, count: filtered.length }),
        );
      }
    }

    return new Response(null, { status: 101, webSocket: client });
  }

  broadcastWebSocket(data) {
    const message = JSON.stringify(data);
    for (const ws of this.state.getWebSockets()) {
      try {
        ws.send(message);
      } catch {
        // WebSocket may already be closed
      }
    }
  }

  // Hibernation API: called when a WebSocket client sends a message.
  // The DO may have been hibernated — constructor's blockConcurrencyWhile
  // ensures this.replies is loaded before this runs.
  async webSocketMessage(ws, message) {
    try {
      const data = JSON.parse(message);

      if (data.ack) {
        const oldLen = this.replies.length;
        // Note: strict > filter removes replies with create_time <= ack (inclusive).
        // Replies with create_time > ack (strictly newer) are kept for next poll.
        this.replies = this.replies.filter((r) => r.create_time > data.ack);
        if (this.replies.length !== oldLen) {
          await this.state.storage.put("replies", this.replies);
        }
      }

      if (data.ping) {
        ws.send(JSON.stringify({ pong: true }));
      }
    } catch {
      // Ignore invalid messages
    }
  }

  webSocketClose(ws, code, reason, wasClean) {
    // Nothing to clean up — runtime removes ws from getWebSockets()
  }

  webSocketError(ws, error) {
    try {
      ws.close(1011, "WebSocket error");
    } catch {}
  }

  // --- Alarm: purge stale replies ---

  async alarm() {
    const cutoff = String(Date.now() - REPLY_TTL_MS);
    this.replies = this.replies.filter((r) => r.create_time > cutoff);
    await this.state.storage.put("replies", this.replies);
    // Re-schedule if replies remain (they'll expire later)
    if (this.replies.length > 0) {
      await this.state.storage.setAlarm(Date.now() + REPLY_TTL_MS);
    }
  }

  // --- HTTP long-poll (backward compat) ---

  async handlePoll(url) {
    const since = url.searchParams.get("since") || "";
    const timeout = Math.min(
      parseInt(url.searchParams.get("timeout") || "25", 10),
      55,
    );

    if (this.takeover) {
      this.takeover = false;
      await this.state.storage.delete("takeover");
      return Response.json({ replies: [], count: 0, takeover: true });
    }

    let filtered = since
      ? this.replies.filter((r) => r.create_time > since)
      : this.replies;
    if (filtered.length > 0) {
      return Response.json({ replies: filtered, count: filtered.length });
    }

    await new Promise((resolve) => {
      const entry = { resolve };
      this.waitingPolls.push(entry);
      setTimeout(() => {
        const idx = this.waitingPolls.indexOf(entry);
        if (idx !== -1) this.waitingPolls.splice(idx, 1);
        resolve();
      }, timeout * 1000);
    });

    if (this.takeover) {
      this.takeover = false;
      await this.state.storage.delete("takeover");
      return Response.json({ replies: [], count: 0, takeover: true });
    }

    filtered = since
      ? this.replies.filter((r) => r.create_time > since)
      : this.replies;
    return Response.json({ replies: filtered, count: filtered.length });
  }

  async handleGet(url) {
    const since = url.searchParams.get("since") || "";

    if (this.takeover) {
      this.takeover = false;
      await this.state.storage.delete("takeover");
      return Response.json({ replies: [], count: 0, takeover: true });
    }

    const filtered = since
      ? this.replies.filter((r) => r.create_time > since)
      : this.replies;
    return Response.json({ replies: filtered, count: filtered.length });
  }
}

// --- Helpers ---

function getStub(env, key) {
  const id = env.HANDOFF_SESSION.idFromName(key);
  return env.HANDOFF_SESSION.get(id);
}

function isDOQuotaError(e) {
  const msg = (e && e.message) || "";
  const lower = msg.toLowerCase();
  return (
    lower.includes("exceeded allowed duration") ||
    lower.includes("durable objects free tier") ||
    lower.includes("exceeded its cpu time limit")
  );
}

async function pushToDO(env, key, reply) {
  const stub = getStub(env, key);
  await stub.fetch(
    new Request("http://do/push", {
      method: "POST",
      body: JSON.stringify(reply),
    }),
  );
}

// --- Auth helper ---

function checkApiKey(request, env) {
  const key = env.API_KEY;
  if (!key) return new Response("API_KEY not configured on worker", { status: 500 });
  const auth = request.headers.get("Authorization") || "";
  if (auth === `Bearer ${key}`) return null;
  return new Response("Unauthorized", { status: 401 });
}

// --- Main worker ---

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "POST" && url.pathname === "/webhook") {
      return handleWebhook(request, env);
    }

    // Card action callback (separate path, also accepts challenge)
    if (request.method === "POST" && url.pathname === "/card-action") {
      return handleWebhook(request, env);
    }

    // Health check (requires API_KEY auth)
    if (request.method === "GET" && url.pathname === "/health") {
      const denied = checkApiKey(request, env);
      if (denied) return denied;
      // Probe Durable Object availability
      let doStatus = "ok";
      try {
        const stub = getStub(env, "health-check");
        await stub.fetch(new Request("http://do/get"));
      } catch (e) {
        doStatus = isDOQuotaError(e)
          ? "quota_exhausted"
          : `error: ${e.message || e}`;
      }
      const doAvailable = doStatus === "ok";
      const hasVerifyToken = !!env.VERIFY_TOKEN;
      // Probe KV availability (read and write separately)
      let kvReadStatus = "ok";
      let kvWriteStatus = "ok";
      const kvTestKey = `health:${Date.now()}`;
      try {
        await env.LARK_REPLIES.put(kvTestKey, "1", { expirationTtl: 60 });
      } catch (e) {
        kvWriteStatus = `error: ${e.message || e}`;
      }
      try {
        await env.LARK_REPLIES.get(kvTestKey);
      } catch (e) {
        kvReadStatus = `error: ${e.message || e}`;
      }
      const kvReadOk = kvReadStatus === "ok";
      const kvWriteOk = kvWriteStatus === "ok";
      const ok = doAvailable && hasVerifyToken && kvReadOk && kvWriteOk;
      const reasons = [];
      if (doStatus === "quota_exhausted") reasons.push("Durable Objects quota exhausted");
      else if (!doAvailable) reasons.push(`Durable Objects ${doStatus}`);
      if (!hasVerifyToken) reasons.push("VERIFY_TOKEN not configured");
      if (!kvReadOk) reasons.push(`KV read ${kvReadStatus}`);
      if (!kvWriteOk) reasons.push(`KV write ${kvWriteStatus}`);
      return Response.json({
        ok,
        verify_token: hasVerifyToken,
        do_available: doAvailable,
        kv_read: kvReadOk,
        kv_write: kvWriteOk,
        ...(reasons.length > 0 && { reason: reasons.join("; ") }),
      });
    }

    // DO quota status (requires API_KEY auth, no DO access)
    if (request.method === "GET" && url.pathname.startsWith("/status/")) {
      const denied = checkApiKey(request, env);
      if (denied) return denied;
      const key = decodeURIComponent(url.pathname.split("/status/")[1]);
      if (!key) {
        return Response.json({ error: "missing key" }, { status: 400 });
      }
      const exhaustedAt = await env.LARK_REPLIES.get(`do_quota_exhausted:${key}`);
      return Response.json({
        do_quota_exhausted: !!exhaustedAt,
        exhausted_at: exhaustedAt || null,
      });
    }

    // --- All endpoints below require API_KEY auth ---

    // WebSocket endpoint (preferred over long-poll — much lower quota usage)
    if (request.method === "GET" && url.pathname.startsWith("/ws/")) {
      const denied = checkApiKey(request, env);
      if (denied) return denied;
      const upgrade = (request.headers.get("Upgrade") || "").toLowerCase();
      const connection = (request.headers.get("Connection") || "").toLowerCase();
      const hasUpgradeToken = connection
        .split(",")
        .map((s) => s.trim())
        .includes("upgrade");
      if (upgrade !== "websocket" || !hasUpgradeToken) {
        return new Response("Expected WebSocket upgrade", { status: 426 });
      }
      const key = decodeURIComponent(url.pathname.split("/ws/")[1]);
      if (!key) {
        return Response.json({ error: "missing key" }, { status: 400 });
      }
      const stub = getStub(env, key);
      // Forward the original request — CF runtime bridges the WebSocket
      // between the client and the DO's acceptWebSocket().
      return stub.fetch(request);
    }

    // Long-poll endpoint (fallback — must be checked before /replies/)
    if (request.method === "GET" && url.pathname.startsWith("/poll/")) {
      const denied = checkApiKey(request, env);
      if (denied) return denied;
      const key = decodeURIComponent(url.pathname.split("/poll/")[1]);
      if (!key) {
        return Response.json({ error: "missing key", replies: [], count: 0 });
      }
      const stub = getStub(env, key);
      const since = url.searchParams.get("since") || "";
      const timeout = url.searchParams.get("timeout") || "25";
      try {
        return await stub.fetch(
          new Request(
            `http://do/poll?since=${encodeURIComponent(since)}&timeout=${timeout}`,
          ),
        );
      } catch (e) {
        console.error("DO poll error:", e.message, e.stack);
        return Response.json(
          { error: e.message, replies: [], count: 0 },
          { status: 500 },
        );
      }
    }

    if (request.method === "GET" && url.pathname.startsWith("/replies/")) {
      const denied = checkApiKey(request, env);
      if (denied) return denied;
      return handleGetReplies(env, url);
    }

    // Acknowledge (remove) processed replies up to a timestamp
    if (
      request.method === "POST" &&
      url.pathname.startsWith("/replies/") &&
      url.pathname.endsWith("/ack")
    ) {
      const denied = checkApiKey(request, env);
      if (denied) return denied;
      return handleAckReplies(env, url);
    }

    // Signal a takeover — the old session's wait_for_reply.py will see this
    if (request.method === "POST" && url.pathname.startsWith("/takeover/")) {
      const denied = checkApiKey(request, env);
      if (denied) return denied;
      return handleTakeover(env, url);
    }

    // Register a sent message's chat_id so reactions can be routed
    if (request.method === "POST" && url.pathname === "/register-message") {
      const denied = checkApiKey(request, env);
      if (denied) return denied;
      return handleRegisterMessage(request, env);
    }

    // Relay a message to another handoff chat (cross-group messaging)
    if (request.method === "POST" && url.pathname === "/relay") {
      const denied = checkApiKey(request, env);
      if (denied) return denied;
      return handleRelay(request, env);
    }

    // Check and consume stop flag (set by Stop button card action)
    if (request.method === "GET" && url.pathname.startsWith("/stop/")) {
      const denied = checkApiKey(request, env);
      if (denied) return denied;
      const key = decodeURIComponent(url.pathname.split("/stop/")[1]);
      if (!key) {
        return Response.json({ stop: false });
      }
      const kvKey = `stop:${key}`;
      const val = await env.LARK_REPLIES.get(kvKey);
      if (val) {
        await env.LARK_REPLIES.delete(kvKey);
        return Response.json({ stop: true });
      }
      return Response.json({ stop: false });
    }

    return new Response("Not found", { status: 404 });
  },
};

async function handleWebhook(request, env) {
  let data;
  try {
    data = await request.json();
  } catch (e) {
    console.error("Bad JSON from Lark:", e);
    return new Response("Bad request", { status: 400 });
  }

  // URL verification challenge (works for both event and card callbacks).
  // Intentionally before VERIFY_TOKEN check: Lark sends this challenge when
  // configuring the webhook URL, before any events flow. The challenge itself
  // is a nonce that proves we control this endpoint — no secret needed.
  if (data.type === "url_verification") {
    return Response.json({ challenge: data.challenge });
  }

  // Event v2.0
  const header = data.header || {};
  const event = data.event || {};

  // Verify event source — reject forged/unauthenticated requests
  if (!env.VERIFY_TOKEN) {
    return new Response("VERIFY_TOKEN not configured", { status: 500 });
  }
  if (header.token !== env.VERIFY_TOKEN) {
    console.error("VERIFY_TOKEN mismatch — rejecting webhook event.",
      "event_type:", header.event_type || "unknown",
      "event_id:", header.event_id || "unknown",
    );
    return new Response("Forbidden", { status: 403 });
  }

  // Idempotency — skip duplicate deliveries (Lark retries on slow responses).
  // Wrapped in try/catch: KV write failures (e.g. free-tier quota exceeded)
  // must not crash the entire webhook handler — processing the event with a
  // small chance of a duplicate is far better than dropping it entirely.
  const eventId = header.event_id;
  if (eventId) {
    try {
      const seen = await env.LARK_REPLIES.get(`seen:${eventId}`);
      if (seen) return new Response("ok", { status: 200 });
      await env.LARK_REPLIES.put(`seen:${eventId}`, "1", { expirationTtl: 3600 });
    } catch (e) {
      console.error("KV idempotency check failed (non-fatal):", e.message || e);
      // Surface the KV error in the chat's poll stream so the user sees it.
      // We can't store a flag in KV (KV itself is broken), so push a system
      // warning into the DO which the polling client will pick up.
      const chatId =
        (event.message || {}).chat_id ||
        ((event.action || {}).value || {}).chat_id;
      if (chatId) {
        try {
          await pushToDO(env, `chat:${chatId}`, {
            text: `KV error: ${e.message || e}`,
            msg_type: "system_warning",
            create_time: String(Date.now()),
          });
        } catch (_) {
          // Best effort — DO may also be down
        }
      }
    }
  }

  // Card action callback (v2: header.event_type === "card.action.trigger")
  if (header.event_type === "card.action.trigger") {
    const action = event.action || {};
    const value = action.value || {};
    const { text: actionText } = extractActionInfo(action);

    // --- Stop button: store flag in KV, return "Stopping..." card ---
    if (actionText === "__stop__") {
      // Authorization: only owner and coowners can stop
      const approvers = value.approvers;
      if (Array.isArray(approvers) && approvers.length > 0) {
        const operatorId = (event.operator || {}).open_id || "";
        if (!approvers.includes(operatorId)) {
          return Response.json({
            toast: { type: "error", content: "Only the owner or coowners can stop" },
          });
        }
      }
      const chatId = value.chat_id;
      if (chatId) {
        await env.LARK_REPLIES.put(`stop:chat:${chatId}`, "1", {
          expirationTtl: 300,
        });
      }
      return Response.json({
        toast: { type: "warning", content: "Stopping..." },
        card: {
          type: "raw",
          data: {
            schema: "2.0",
            config: { update_multi: true },
            header: {
              title: { tag: "plain_text", content: "Stopping..." },
              template: "orange",
            },
            body: {
              direction: "vertical",
              elements: [
                { tag: "markdown", content: "Stop requested. Waiting for current tool to finish..." },
              ],
            },
          },
        },
      });
    }

    const result = await handleCardAction(event, env);

    // Unauthorized sender — return error toast, keep card unchanged
    if (result && result.unauthorized) {
      return Response.json({
        toast: { type: "error", content: "Only the operator or coowners can decide" },
      });
    }

    const title = value.title || "Confirmed";
    const body = value.body || "";

    // Use shared extraction so display matches stored reply
    const { text: displayText } = extractActionInfo(action);

    // Determine card color based on action: red for deny, green for approve
    const DENY_TEXTS = new Set(["n", "no", "deny", "reject", "0"]);
    const isDeny = DENY_TEXTS.has(displayText.toLowerCase());
    const template = isDeny ? "red" : "green";
    const toastType = isDeny ? "warning" : "success";
    const toastVerb = isDeny ? "Denied" : "Got it";

    const elements = [];
    if (body) {
      elements.push({
        tag: "div",
        text: { content: body, tag: "lark_md" },
      });
    }
    elements.push({
      tag: "div",
      text: {
        content: `> Selected: **${displayText}**`,
        tag: "lark_md",
      },
    });
    return Response.json({
      toast: { type: toastType, content: `${toastVerb}: ${displayText}` },
      card: {
        type: "raw",
        data: {
          header: {
            title: { tag: "plain_text", content: title },
            template,
          },
          elements,
        },
      },
    });
  }

  if (header.event_type === "im.message.receive_v1") {
    try {
      await handleMessage(event, env);
    } catch (e) {
      if (isDOQuotaError(e)) {
        const chatId = (event.message || {}).chat_id;
        if (chatId) {
          await env.LARK_REPLIES.put(
            `do_quota_exhausted:chat:${chatId}`,
            String(Date.now()),
            { expirationTtl: 3600 },
          );
        }
        console.error("DO quota exhausted on message push:", e.message);
      } else {
        throw e;
      }
    }
  }

  if (header.event_type === "im.message.reaction.created_v1") {
    await handleReaction(event, env);
  }

  return new Response("ok", { status: 200 });
}

async function handleMessage(event, env) {
  const message = event.message || {};
  const sender = event.sender || {};

  // Bot messages are no longer filtered server-side. The client filters
  // out its own bot's messages using bot_open_id from the session table.
  // This allows other bots in the group to be heard by the handoff session.

  const rootId = message.root_id;
  const chatId = message.chat_id;
  if (!rootId && !chatId) return; // Neither thread reply nor chat message

  // Extract content based on message type
  let text = "";
  let image_key = "";
  let file_key = "";
  let file_name = "";
  const msg_type = message.message_type || "unknown";

  if (msg_type === "text") {
    try {
      const content = JSON.parse(message.content || "{}");
      text = content.text || "";
    } catch {
      // ignore parse errors
    }
  } else if (msg_type === "image") {
    try {
      const content = JSON.parse(message.content || "{}");
      image_key = content.image_key || "";
    } catch {
      // ignore parse errors
    }
    text = "[image]";
  } else if (msg_type === "file") {
    try {
      const content = JSON.parse(message.content || "{}");
      file_key = content.file_key || "";
      file_name = content.file_name || "";
      text = `[file: ${file_name || "unknown"}]`;
    } catch {
      text = "[file]";
    }
  } else if (msg_type === "post") {
    try {
      const content = JSON.parse(message.content || "{}");
      // Post can be direct {title, content} or locale-keyed {en_us: {title, content}}
      let post = content;
      if (!Array.isArray(content.content)) {
        const locale = Object.keys(content)[0];
        post = content[locale] || {};
      }
      const paragraphs = post.content || [];
      const parts = [];
      const imageKeys = [];
      for (const para of paragraphs) {
        for (const elem of para) {
          if (elem.text) parts.push(elem.text);
          else if (elem.tag === "img") {
            parts.push("[image]");
            if (elem.image_key) imageKeys.push(elem.image_key);
          }
        }
      }
      text = parts.join("\n") || "[post]";
      if (imageKeys.length) image_key = imageKeys.join(",");
    } catch {
      text = "[post]";
    }
  } else if (msg_type === "sticker") {
    try {
      const content = JSON.parse(message.content || "{}");
      file_key = content.file_key || "";
    } catch {
      // ignore parse errors
    }
    text = "[sticker]";
  } else if (msg_type === "merge_forward") {
    // Merge-forwarded conversation thread — store the message_id so the
    // client can call the Lark API to read child messages.
    text = "[merge_forward]";
  } else {
    text = `[${msg_type} message]`;
  }

  // Extract mention open_ids for client-side @-mention filtering (sidecar mode)
  const mentions = (message.mentions || []).map((m) => ({
    key: m.key,
    id: (m.id || {}).open_id || "",
    name: m.name || "",
  }));

  const reply = {
    text,
    msg_type,
    image_key: image_key || undefined,
    file_key: file_key || undefined,
    file_name: file_name || undefined,
    parent_id: message.parent_id || undefined,
    mentions: mentions.length ? mentions : undefined,
    sender_type: sender.sender_type || "unknown",
    sender_id: (sender.sender_id || {}).open_id || "",
    create_time: message.create_time || "",
    message_id: message.message_id || "",
  };

  // Push to DO — store under both chat key and thread key when applicable.
  // wait_for_reply.py polls chat:${chatId}, so thread replies must also
  // be stored there (same pattern as handleCardAction).
  const pushes = [];
  if (chatId) pushes.push(pushToDO(env, `chat:${chatId}`, reply));
  if (rootId) pushes.push(pushToDO(env, rootId, reply));
  await Promise.all(pushes);
}

/**
 * Extract action text and message type from a card action event.
 * Used by both handleCardAction (reply storage) and the card callback
 * response (toast/display text) to ensure consistent formatting.
 */
function extractActionInfo(action) {
  const value = action.value || {};
  const formValue = action.form_value;
  if (
    formValue &&
    typeof formValue === "object" &&
    Object.keys(formValue).length > 0
  ) {
    // Form submission — form_value contains {field_name: value} pairs.
    // If there's exactly one field, use its value directly; otherwise JSON.
    const entries = Object.entries(formValue);
    const text =
      entries.length === 1 ? String(entries[0][1]) : JSON.stringify(formValue);
    return { text, msgType: "form_action" };
  }
  if (action.option !== undefined) {
    return { text: action.option || "", msgType: "select_action" };
  }
  if (action.input_value !== undefined) {
    return { text: action.input_value || "", msgType: "input_action" };
  }
  return { text: value.action || "", msgType: "button_action" };
}

async function handleCardAction(event, env) {
  // v2 format: event.action.value contains our custom data
  const action = event.action || {};
  const operator = event.operator || {};
  const value = action.value || {};

  const rootId = value.root_id;
  const chatId = value.chat_id;
  const nonce = value.nonce;

  const { text: actionText, msgType } = extractActionInfo(action);

  if ((!rootId && !chatId && !nonce) || !actionText) return;

  // Check approver authorization: if the card carries an approvers list,
  // only those open_ids may approve/deny. Others see an error toast.
  const approvers = value.approvers;
  if (Array.isArray(approvers) && approvers.length > 0) {
    const operatorId = operator.open_id || "";
    if (!approvers.includes(operatorId)) {
      return { unauthorized: true };
    }
  }

  // Store as a reply so wait_for_reply.py / permission_bridge.py picks it up
  const reply = {
    text: actionText,
    msg_type: msgType,
    sender_type: "user",
    sender_id: operator.open_id || "",
    create_time: String(Date.now()),
    message_id: "",
  };

  // Push to DO under chat key (handoff groups), root key (threads),
  // and/or nonce key (permission request correlation).
  const pushes = [];
  if (chatId) pushes.push(pushToDO(env, `chat:${chatId}`, reply));
  if (rootId) pushes.push(pushToDO(env, rootId, reply));
  if (nonce) pushes.push(pushToDO(env, nonce, reply));
  await Promise.all(pushes);
}

async function handleReaction(event, env) {
  const messageId = event.message_id || "";
  const reactionType = (event.reaction_type || {}).emoji_type || "";
  const operatorId = (event.user_id || {}).open_id || "";
  const actionTime = event.action_time || String(Date.now());

  if (!messageId || !reactionType) return;

  // Look up chat_id from registered message mapping (stays on KV)
  const chatId = await env.LARK_REPLIES.get(`msgchat:${messageId}`);
  if (!chatId) return; // Unknown message, can't route

  const reply = {
    text: reactionType,
    msg_type: "reaction",
    target_message_id: messageId,
    sender_type: "user",
    sender_id: operatorId,
    create_time: actionTime,
    message_id: "",
  };

  await pushToDO(env, `chat:${chatId}`, reply);
}

async function handleRegisterMessage(request, env) {
  let data;
  try {
    data = await request.json();
  } catch {
    return Response.json({ error: "bad request" }, { status: 400 });
  }

  const messageId = data.message_id;
  const chatId = data.chat_id;
  if (!messageId || !chatId) {
    return Response.json(
      { error: "missing message_id or chat_id" },
      { status: 400 },
    );
  }

  // Store mapping with 7-day TTL (stays on KV — ephemeral, low volume)
  await env.LARK_REPLIES.put(`msgchat:${messageId}`, chatId, {
    expirationTtl: 604800,
  });
  return Response.json({ ok: true });
}

async function handleRelay(request, env) {
  let data;
  try {
    data = await request.json();
  } catch {
    return Response.json({ error: "bad request" }, { status: 400 });
  }

  const toChatId = data.to_chat_id;
  const message = data.message || "";
  const fromChatId = data.from_chat_id || "";
  const fromChatName = data.from_chat_name || "";
  const fromWorkspace = data.from_workspace || "";

  if (!toChatId || !message) {
    return Response.json(
      { error: "missing to_chat_id or message" },
      { status: 400 },
    );
  }

  const reply = {
    text: message,
    msg_type: "relay",
    from_chat_id: fromChatId,
    from_chat_name: fromChatName,
    from_workspace: fromWorkspace,
    sender_type: "relay",
    sender_id: "",
    create_time: String(Date.now()),
    message_id: "",
  };

  try {
    await pushToDO(env, `chat:${toChatId}`, reply);
    return Response.json({ ok: true });
  } catch (e) {
    if (isDOQuotaError(e)) {
      return Response.json(
        { ok: false, error: "do_quota_exhausted" },
        { status: 503 },
      );
    }
    return Response.json({ ok: false, error: e.message }, { status: 500 });
  }
}

async function handleTakeover(env, url) {
  const key = decodeURIComponent(url.pathname.split("/takeover/")[1]);
  if (!key) {
    return Response.json({ error: "missing key" }, { status: 400 });
  }

  const stub = getStub(env, key);
  return stub.fetch(new Request("http://do/takeover", { method: "POST" }));
}

async function handleGetReplies(env, url) {
  try {
    const rootId = decodeURIComponent(url.pathname.split("/replies/")[1]);
    if (!rootId) {
      return Response.json({ error: "missing root_id", replies: [], count: 0 });
    }

    const since = url.searchParams.get("since") || "";
    const stub = getStub(env, rootId);
    return stub.fetch(
      new Request(`http://do/get?since=${encodeURIComponent(since)}`),
    );
  } catch (e) {
    return Response.json(
      { error: e.message, replies: [], count: 0 },
      { status: 500 },
    );
  }
}

async function handleAckReplies(env, url) {
  try {
    const parts = url.pathname.split("/replies/")[1].split("/ack")[0];
    const key = decodeURIComponent(parts);
    if (!key) {
      return Response.json({ error: "missing key" }, { status: 400 });
    }

    const before = url.searchParams.get("before");
    if (!before) {
      return Response.json(
        { error: "missing before parameter" },
        { status: 400 },
      );
    }

    const stub = getStub(env, key);
    return stub.fetch(
      new Request(`http://do/ack?before=${encodeURIComponent(before)}`, {
        method: "POST",
      }),
    );
  } catch (e) {
    return Response.json({ error: e.message }, { status: 500 });
  }
}
