# Changelog

All notable changes to echo-adk. The format follows Keep a Changelog; versions follow SemVer.

## [Unreleased]

### Added
- `echo_adk.channels.openclaw`: echoBots chat channels over OpenClaw (transport, not runtime). Bot
  `channels:[{kind:"openclaw", target}]`, `PUT /bots/{id}/channels`, `POST /channels/openclaw/inbound`
  (in-process bot turn; reply to the originating chat), health gate (loopback `/healthz` + `/readyz` and a
  read-only scan of the gateway log for the three wedge modes) exposed as `channels[].state`.
- Approval notifications for gated turns, bot errors, daily-cap hits and finished group chats, Telegram
  first, with Approve/Deny callbacks; `GET /approvals`, `GET /approvals/{id}`, `POST /approvals/{id}/approve|deny`.
  Default deny (TTL), single use, one-run Lease, no always-allow, no tier >= 2 grant from chat, no token in a message.
- `CliTransport` (`openclaw message send`, no shell, `.cmd` shim resolved to `node openclaw.mjs`).
- `deploy/openclaw-hook/echo-adk-bridge`: OpenClaw internal hook that forwards mapped chats to the inbound route.
- 42 tests with a stand-in gateway, a fake gateway HTTP server, a fake `openclaw` CLI and log fixtures.

## [0.1.0] - 2026-11-04

First bootstrap of the Echo Agent Development Kit (Commander order 2026-11-04).

### Added
- `echo_adk.core.types`: `Role`, `Skill`, `Capability`, `Lane`+`Budget`, `Lease`, `Proof`, `EchoAgent`,
  `Envelope` with strict JSON (de)serialisation (unknown keys refused, 32-hex ids, budget caps
  120 calls / 5 USD / 4800 s, `untrusted` always true, `envelope_version` 1).
- `echo_adk.core.policy`: default-deny `CapabilityRegistry` (seeded with 12 capabilities verified live via
  `echo.caps.detail`), tier classification, allowlist + Lease scope + grant + budget + params checks ->
  `allowed|gated|denied` with `needs`/`how`.
- `echo_adk.core.models`: `LoopbackModel` (OpenAI-compatible, loopback origin only, 1 MiB cap),
  `CliModel` ported from Prime `providers.ts` (claude/codex/grok/argon specs, stdin rules, 28k argv cap
  for agy, allowlisted child env), `ProviderUnavailable`, `Answered`, strict models config.
- `echo_adk.core.broker`: `GateBroker` for `POST {ECHO_GATE_URL}/sdk/invoke`, key from
  `ECHO_ADK_GATE_KEY` at call time, local lease check before network, redacted `Envelope` results.
- `echo_adk.core.runner`: `run(agent, prompt, lease) -> Proof` with the one-JSON-Action protocol,
  append-only JSONL ledger, Proof hash, PASS/FAIL/UNKNOWN verdicts, stop events, cost accounting.
- `echo_adk.core.skills`: Agent-Skills loader/validator with progressive L1/L2/L3 loading.
- `echo_adk.agents`: `.json` loader plus a strict minimal YAML subset; shipped `agents/knowledge-scout.json`
  and `agents/forge-steward.json`; `docs/examples/knowledge-scout.yaml`.
- `echo_adk.api`: the frozen echo-adk-api contract on a loopback-only `ThreadingHTTPServer`, SQLite state,
  worker pool, budget clamps, service-wide daily cap; agent definitions managed over HTTP
  (`POST /agents` 201, `PUT /agents/{name}`, `DELETE /agents/{name}` with a 409 while a bot references the
  agent) saved as `<agents-dir>/<name>.json` with `origin:{by, prompt_sha256?, created_at}` (contract
  addition of 2026-11-04 20:12, "Echo autonomously makes ADKs and echoBots from the user's prompts").
- `echo_adk.bots`: echoBots (paused on creation, per-bot Lease and memory scope, scheduler, inbox,
  daily cost cap, groups of 2..6, delete leaves nothing behind).
- `echo_adk.mcp`: zero-dependency stdio JSON-RPC MCP server (`initialize`, `tools/list`, `tools/call`).
- `echo_adk.a2a`: Agent2Agent card generator and `echo-a2a/1.0` translator.
- `echo_adk.adk` (extra `adk`): `EchoModel(BaseLlm)`, `EchoToolset(BaseToolset)`,
  `EchoGovernancePlugin(BasePlugin)`, `app(agent)`; import-guarded; drift test against the core registry.
- `echo-adk` CLI: `run`, `validate`, `serve`, `card`, `mcp`.
- Deploy unit `deploy/echo-adk-api.service` and runbook; CI (pytest 3.10/3.12 + ruff).
