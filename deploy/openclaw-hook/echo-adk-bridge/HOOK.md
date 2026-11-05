---
name: echo-adk-bridge
description: "Forward chat messages for echoBot-mapped chats to echo-adk-api on loopback"
metadata:
  { "openclaw": { "events": ["message:received"], "os": ["win32", "linux", "darwin"] } }
---

# echo-adk bridge

Forwards `message:received` events whose chat handle (`<channel>:<conversation id>`) is listed in
`ECHO_ADK_BRIDGE_TARGETS` to `POST {ECHO_ADK_BRIDGE_URL}/channels/openclaw/inbound` (default
`http://127.0.0.1:8378`). echo-adk turns each message into an echoBot turn and answers through
`openclaw message send`. Approval replies (`approve <id>`, `deny <id>`) and unclaimed Telegram button
presses (`callback_data: echoadk:approve:<id>`) travel the same way.

- Loopback only: a non-loopback `ECHO_ADK_BRIDGE_URL` is refused.
- Sends only `channel`, `target`, `sender`, `text` (12000 chars max) and `message_id`. No token, no
  config, no media.
- One bounded request per event (5 s timeout). A failure is logged without the message text and
  never retried.

## Limits (read before enabling)

- `message:received` is an observation event. The OpenClaw agent bound to the chat still receives
  the message. Bind echoBot chats to no agent, or replace this hook with a plugin whose
  `before_dispatch` hook returns `{ handled: true }`.
- The `context.conversationId` → target mapping comes from the 2026.9.6 hook docs and has not been
  checked against a live gateway. Confirm it with one message before relying on it.
- Not installed by echo-adk. Copy this directory to `~/.openclaw/hooks/echo-adk-bridge/` on the
  gateway host, set the environment on the gateway process, then run
  `openclaw hooks enable echo-adk-bridge`.

## Environment

| Variable | Meaning |
| --- | --- |
| `ECHO_ADK_BRIDGE_URL` | echo-adk-api origin on loopback (default `http://127.0.0.1:8378`) |
| `ECHO_ADK_BRIDGE_TARGETS` | Comma-separated chat handles to forward, for example `telegram:123456789,discord:channel:222222222222222222` |
