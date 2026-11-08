# Changelog

All notable changes to echo-adk. The format follows Keep a Changelog; versions follow SemVer.

## [Unreleased]

### Added

- Per-bot headless browser processes and persistent profiles with fixed host egress policy, bounded
  snapshots/opaque control refs, screenshots, cancellation, close/reset and actual runtime probes.
- Per-bot OAuth app catalog, S256 consent flow, encrypted grant storage, refresh, operation routing and
  explicit revocation receipts. Owner callback codes stay outside general SDK capability logging.
- Authenticated connector polling, deterministic prechecks and durable deduplicated event delivery.
- One-time, monthly, yearly and bounded five-field cron schedules; versioned editable notes and audited
  restore; portable team/department/templates with preview digests and interrupted-import recovery.
- Explicit per-bot ECF note capture through the existing admitted gate, fixed host project bindings,
  durable same-request reconciliation, validated persistence receipts and exact readback. It remains
  disabled until configured; controlled transport tests do not claim a live canonical write.
- Fifty-six closed SDK operation contracts now cover the native management and workspace APIs.
  They remain proposed registrations, requiring the existing authenticated Tier 2 host admission.

- Additive `/v2` API and `docs/CONTRACT_V2.md`, mirrored in
  `echo-cli/docs/echo-code/ECHO_BOTS_API_V2.md`; v1 response shapes remain versioned separately.
- Local calendar schedules with IANA timezones, ISO weekdays, explicit DST behavior, and `skip`/`once`
  missed-run policies; persisted scheduled jobs resume by their original run ID before execution starts.
- Bounded event inboxes with durable `(bot, source, event_id)` deduplication, payload-conflict rejection,
  untrusted event framing, and atomic message/run/receipt admission.
- Atomic SQLite service and per-bot budget reservations for direct, inbox, event, scheduled, and group
  runs; exact settlement, uncertain-cost reporting, holds spanning UTC midnight, and v2 budget health.
- Bounded recall from each bot's local memory scope, integrity-checked records, durable write receipts
  in final envelope data, and compatibility reads for legacy memory filenames. Crystal/ECF remains a
  separate integration; local receipts do not claim an external acknowledgment.
- V2 agent, run, bot, event, group and resource operations have closed SDK contracts and an authenticated
  host adapter. Registration stays explicit; direct target calls cannot substitute for SDK gate admission.
- Paused bot configuration updates, cooperative stop, and bounded installed-skill guidance with
  applied-skill receipts. Updating or stopping a bot never implicitly resumes it.
- Certification Forge App opt-in and a bounded standard-library journey that executes real local
  policy, SDK schema, durable event/budget, memory, and interval-scheduling checks. Offline evidence is
  explicitly separated from issuer verdicts and signed production E2E attestations.

### Fixed

- New Google Drive connections request explicit account selection together with offline
  consent, so owners with multiple signed-in accounts can choose the intended account.
  Existing configuration-fingerprint checks remain enforced; no account is hardcoded.
- Wall budgets are rechecked after model, SDK, catalog, and durable final writes. Late results preserve
  actual spend and completed receipts but return `budget_exceeded` with a `FAIL` proof. Built-in I/O waits
  narrow to the remaining allowance without mutating shared transports; unknown costs remain reserved.
- SDK management operations use fixed `adk.manage` or `echobots.manage` scopes while retaining
  Tier 2 HMAC admission. The adapter requires that named scope on the verified active owner's key
  and a single-use gate admission; task-context-only grants cannot replace verified key authority.
- Insufficient daily allowance returns 429 before admitting new work; simultaneous admissions cannot
  reserve the same funds. Deleting bot state preserves aggregate service spend and uncertainty.
- Queued bot leases begin at dispatch using the immutable admitted template; waiting while paused no
  longer consumes their TTL or widens their scopes. Direct run lease expiry remains unchanged.
- Interrupted work is not replayed automatically; completed inbox delivery and group reservations are
  recovered without another model call. Cancelling queued work preserves a bot's paused state.
- Pending inbox lookup uses indexed role/state projections rather than a capped scan of old history.
- The loopback API checks Host and browser-origin headers and requires JSON mutation bodies, with a
  1 MiB limit. V1 bot responses require an upgrade for schedule keys beyond `every_s` (calendar or explicit
  `missed` policy); v1 message responses omit event metadata that remains available through v2.

### Operational notes

- State migration is additive. Settle outstanding reservations before a downgrade, and use a consistent
  backup or reviewed migration because older code does not maintain the new accounting/inbox columns.
  Keep legacy memory files; old readers cannot discover new scope-encoded memory paths.
- Deployment instructions require complete stopped-writer backups, restore verification, and
  reconciliation of work admitted after the backup; reverting only the executable is insufficient.
- No isolated browser provider, Crystal/ECF service, live FORGE certification, or comparative bot
  evaluation is delivered by this change. See `docs/CONTRACT_V2.md` and `docs/SLICES.md` for exact limits.

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
