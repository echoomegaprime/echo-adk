// echo-adk bridge (OpenClaw internal hook, event message:received).
// Forwards messages from echoBot-mapped chats to echo-adk-api on loopback. No token, no retries.

const DEFAULT_URL = "http://127.0.0.1:8378";
const MAX_TEXT = 12000;
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);

export function bridgeUrl(env = process.env) {
  const raw = (env.ECHO_ADK_BRIDGE_URL || DEFAULT_URL).trim();
  let url;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" || url.username || url.password || !LOOPBACK_HOSTS.has(url.hostname)) {
    return null;
  }
  return `${url.protocol}//${url.host}`;
}

export function targets(env = process.env) {
  return new Set(
    String(env.ECHO_ADK_BRIDGE_TARGETS || "")
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean),
  );
}

export function toInbound(event, allowed) {
  if (!event || event.type !== "message" || event.action !== "received") return null;
  const ctx = event.context || {};
  const meta = ctx.metadata || {};
  const channel = String(ctx.channelId || meta.provider || "").toLowerCase();
  const target = String(ctx.conversationId || meta.to || "");
  if (!channel || !target || !allowed.has(`${channel}:${target}`)) return null;
  const text = typeof ctx.content === "string" ? ctx.content : "";
  if (!text.trim()) return null;
  const body = {
    channel,
    target,
    sender: String(meta.senderId || ctx.from || "unknown"),
    text: text.slice(0, MAX_TEXT),
  };
  if (ctx.messageId) body.message_id = String(ctx.messageId).slice(0, 128);
  return body;
}

export default async function handler(event) {
  const body = toInbound(event, targets());
  if (!body) return;
  const base = bridgeUrl();
  if (!base) {
    console.warn("[echo-adk-bridge] refusing a non-loopback ECHO_ADK_BRIDGE_URL");
    return;
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  try {
    const res = await fetch(`${base}/channels/openclaw/inbound`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      redirect: "error",
      signal: controller.signal,
    });
    if (!res.ok) console.warn(`[echo-adk-bridge] echo-adk refused the message: HTTP ${res.status}`);
  } catch (err) {
    console.warn(`[echo-adk-bridge] echo-adk unreachable: ${(err && err.name) || "error"}`);
  } finally {
    clearTimeout(timer);
  }
}
