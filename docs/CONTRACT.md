# echo-adk-api ↔ Echo Prime contract (v1, frozen 2026-11-04)

Loopback HTTP service `echo-adk-api` (Python, package `echo_adk.api`, repo `echoomegaprime/echo-adk`) consumed by Echo Prime (`src/prime/agents.ts`, `src/prime/bots.ts`) exactly the way `src/prime/swarm.ts` consumes the Maximalist worker: plain `fetch`, `redirect:'error'`, loopback origin only, 5 s health / 15 s mutate / 10 s poll timeouts, 1 MiB response cap, every string redacted before it reaches the UI, no fallback of any kind.

- Default base URL: `http://127.0.0.1:8378` (Prime config sections `adk` and `bots`, each `{baseUrl?, defaultBudget?}`; unknown keys and non-loopback origins refused; sections absent = feature off, picker/nav shows "off").
- All ids are 32 lowercase hex chars (`run_id`, `bot_id`, `group_id`, `message_id`). Prime refuses anything else before building a URL.
- Every response is JSON with `Content-Type: application/json`. Errors: `{error: "<code>", message: "<redacted text>"}` with 4xx/5xx. Codes: `not_found`, `invalid`, `gated` (needs a Lease; body carries `needs`, `how`), `budget_exceeded`, `unavailable` (a provider or dependency is down; `provider` named; never rerouted).
- Service identity: `GET /health` → `{ok:true, service:"echo-adk-api", profile:"ECHO_ADK", version:"<semver>", agents:<n>, bots:<n>, runs_active:<n>}`. Prime refuses the service when `profile !== "ECHO_ADK"` (same rule as the swarm `MAXIMALIST_RECONSTRUCTED` check).

## Agents (Echo ADK definitions)

- `GET /agents` → `{agents:[EchoAgent]}`; `EchoAgent = {name, role, skills:[string], capabilities:[string], lane:{node, model, budget:{max_calls, max_cost_usd, max_wall_s}}, max_steps, instruction?:string, source:"<relative path>"}`.
- `GET /agents/{name}` → `EchoAgent` (404 `not_found`).
- Added 2026-11-04 20:12 (Commander: "Echo autonomously makes ADKs and echoBots from the user's prompts"): `POST /agents` body = `EchoAgent` without `source` → `201 EchoAgent` (validated like a file definition: name `^[a-z0-9][a-z0-9-]{1,62}$`, capabilities must be known cap ids, lane budget clamped, unknown keys refused; saved as `<agents-dir>/<name>.json`; `409 invalid` if the name exists); `PUT /agents/{name}` → `200 EchoAgent` (replace); `DELETE /agents/{name}` → `{name, deleted:true}` (refused with `409 invalid` while a bot references it). Every created definition carries `origin:{by:"echo"|"owner", prompt_sha256?, created_at}` so the UI can show "Echo built this from your prompt".
- `POST /runs` body `{agent:string, prompt:string (<= 12000 chars), budget?:{max_calls?, max_cost_usd?, max_wall_s?}, lease?:{scopes:[exact cap ids], ttl_s, max_uses}}` → `202 {run_id, status:"queued"}`. Budget is clamped server-side to the agent lane maxima and to the service caps (`max_calls` 120, `max_cost_usd` 5, `max_wall_s` 4800); default cost 0 (lane `qwen-local`).
- `GET /runs/{id}` → `{run_id, agent, status:"queued"|"running"|"gated"|"done"|"failed", started_at, finished_at?, steps:[Envelope], proof?:Proof, error?:{code, message, provider?}, budget:{...}, spent:{calls, cost_usd, wall_s}}`.
  - `Envelope` is spec §3 exactly: `{envelope_version:1, run_id, step, kind:"cap"|"model"|"shell"|"memory"|"artifact"|"final", capability?, tier?, verdict:"allowed"|"gated"|"denied", ok, untrusted:true, redacted:<n>, audit_id?, duration_ms, needs?, how?, data:{}}`. `kind:"final"` carries `data.text`. `kind:"model"` carries `data.answered:{provider, requested, model, reported}` (the actual model that answered; `reported:false` when the provider does not say).
  - `Proof = {run_id, lease_id, events_sha256, actions:[{cap, tier, verdict, audit_id}], artifacts:[], receipts:[], verdict:"PASS"|"FAIL"|"UNKNOWN"}`.
- `POST /runs/{id}/stop` → `{run_id, status}` (stops after the current step; the UI says a running provider call is not interrupted).
- `GET /runs?agent=&limit=` → `{runs:[{run_id, agent, status, started_at}]}` (newest first, limit <= 50).

## echoBots (always-on governed agents)

- `Bot = {bot_id, name, agent, mode:"personal"|"team", status:"paused"|"idle"|"running"|"error", schedule:{every_s}|null, instruction?:string, plugins:[string] (skill names), memory:{scope:"bot:<bot_id>"}, lease:{scopes:[exact cap ids], max_uses, ttl_s}, daily_cost_cap_usd, spent_today_usd, inbox_depth, last_run_at?, last_error?, created_at}`.
- `GET /bots` → `{bots:[Bot]}`; `GET /bots/{id}` → `Bot`.
- `POST /bots` body `{name (<= 64), agent, mode?, schedule?:{every_s >= 60}, instruction?, plugins?, lease?, daily_cost_cap_usd? (<= 5)}` → `201 Bot` with `status:"paused"` (bots start paused; the owner resumes).
- `POST /bots/{id}/resume` / `POST /bots/{id}/pause` → `Bot`.
- `DELETE /bots/{id}` → `{bot_id, deleted:true}` (Prime asks for confirmation in the UI).
- `POST /bots/{id}/message` body `{text (<= 12000)}` → `202 {message_id, run_id}` (queues one bot turn; the bot answers through its run; a paused bot still accepts the message and answers when resumed).
- `GET /bots/{id}/messages?since=<message_id>&limit=` → `{messages:[{message_id, role:"user"|"bot"|"system", text, created_at, run_id?}]}` (oldest first, limit <= 200).
- Groups (2 to 6 bots in one chat): `POST /groups {name, bots:[bot_id x2..6], prompt}` → `202 {group_id, run_ids:[...]}`; `GET /groups/{id}` → `{group_id, name, bots, status, messages:[{bot_id, text, created_at}], summary?}`.
- Every bot turn is a normal run (`GET /runs/{run_id}` works) with the bot's own Lease; bot memory is written under `memory.scope` only. Bots never hold tokens, never share a shell, and never share a Lease (deliberate divergence from Grok Bots, which share one cloud computer and credentials).

## echoBots chat channels over OpenClaw + approvals (additive, 2026-11-05)

Commander asked "Should OpenClaw be integrated anywhere?" Decision: **OpenClaw is a transport, not a runtime.** echoBots keep running inside echo-adk (own Lease, own memory scope, daily cap, governed runs); OpenClaw only carries chat messages in and out. Everything below is additive: no existing field, route or status changes meaning, and Prime can ignore all of it.

- `Bot` gains `channels:[Channel]` (always present, `[]` when none). `Channel = {kind:"openclaw", target:"<channel>:<dest>", state:"ready"|"unavailable: <reason>", approvers?:[sender id], allow_from?:[sender id], last_sent_at?, last_error?, dropped?, note?}`. `channel` is one of `telegram, discord, slack, signal, whatsapp, matrix, msteams, googlechat, imessage, mattermost`; `dest` is 1..160 chars from `[A-Za-z0-9@:_.+#!=/-]` (`telegram:123456789`, `telegram:-1001234567890:topic:7`, `discord:channel:222222222222222222`). At most 4 per bot; a target routes to exactly one bot (`409 invalid` otherwise).
- `POST /bots` accepts `channels?:[{kind, target, approvers?, allow_from?}]`. `PUT /bots/{id}/channels` body `{channels:[...]}` → `200 Bot` (replace; `[]` detaches).
- `state` is health-gated: `ready` only when the gateway answers `/healthz` and `/readyz` on loopback and its log shows none of the three known wedge modes in the last 15 minutes (a missing or unreadable log directory is itself `unavailable`, because those wedges are invisible to the probes). Otherwise `unavailable: <reason>` (for example `unavailable: gateway /healthz timed out`, `unavailable: gateway wedged: leaked turn claim on session 0f1e2d3c (2 refused turns in 15 min)`, `unavailable: openclaw adapter off (ECHO_ADK_OPENCLAW is not set on this host)`). An unavailable channel never pauses, fails or delays the bot: the turn runs, the reply is stored in `messages`, and the chat delivery is dropped with `last_error` and `dropped` set. No retry loop.
- `POST /channels/openclaw/inbound` body `{channel, target:"<dest>", sender, text | callback, message_id?}` (loopback; called by the gateway-side bridge in `deploy/openclaw-hook/`):
  - a message for a mapped chat → `202 {kind:"message", bot_id, message_id, run_id}` (the `POST /bots/{id}/message` path, in-process; the reply goes back to that chat);
  - an approval decision (`callback:"echoadk:approve:<approval_id>"`, `text:"callback_data: echoadk:deny:<approval_id>"` or `text:"approve <approval_id>"`) → `200 {kind:"decision", approval:Approval}`;
  - a repeated `message_id` for the same chat → `200 {kind:"duplicate", message_id}`;
  - unmapped chat → `404 not_found`; sender outside `allow_from` or not an approver → `403 invalid`; malformed → `400 invalid`.
- `Approval = {approval_id, kind:"gated"|"error"|"daily_cap"|"group_done", status:"pending"|"approved"|"denied"|"expired"|"notice", actions:["approve","deny"]|[], summary, created_at, notified:{target, state:"queued"|"sent"|"<why not>", at}|null, bot_id?, group_id?, run_id?, needs?, how?, capability?, tier?, lease?:{scopes, max_uses, ttl_s}, expires_at?, decided_at?, decided_by?:"api"|"openclaw:<channel>:<sender>"|"default-deny", result_run_id?, reason?}`.
  - `GET /approvals?status=&bot_id=&limit=` → `{approvals:[Approval]}` (newest first, limit <= 200); `GET /approvals/{id}` → `Approval`.
  - `POST /approvals/{id}/approve` / `POST /approvals/{id}/deny` (empty body; owner-only on Prime) → `200 Approval`; not pending → `409 invalid`; unknown → `404 not_found`; a non-empty body → `400 invalid` (an approval cannot be widened by the caller).
- What raises one notification (sent to `ECHO_ADK_OPENCLAW_NOTIFY` when set, else the bot's channels, Telegram first):
  - **gated** turn: `needs`/`how` from the gated envelope. Approve mints a Lease for **one** re-run of that turn: `lease scope X` adds X (only when X is in the agent allowlist and tier <= 1), `lease`/`lease renewal` mints the template again, `lease with more max_uses` doubles `max_uses` (cap 120). A tier >= 2 gate is a `notice`: chat approval cannot mint a signed ESH/SOL grant. One open approval per bot and need (a scheduled bot does not flood the chat).
  - **error** (failed turn): Approve resumes the bot and retries that turn once with the template Lease.
  - **daily_cap** hit: `notice` only; the cap is never raised from chat.
  - **group_done**: `notice` with the group summary.
- Approval rules: default deny (no decision before `expires_at`, default 30 min, is `expired`), single use, no always-allow, the bot's Lease template is never changed, the re-run goes through the normal daily-cap check. Chat decisions are accepted only from the chat the notification was delivered to (`notified.state == "sent"`) and only from an approver (`ECHO_ADK_OPENCLAW_APPROVERS` = `telegram:<id>,...`, plus the channel's `approvers`); with no approvers every chat decision is refused. Buttons carry `echoadk:<approve|deny>:<approval_id>` (48 bytes; Telegram allows 64) and the text carries the same reference; no token or key is ever in a message.
- Transport: replies go out through `openclaw message send --channel=<c> --target=<dest> --message=<text> --json [--presentation=<buttons>]`, executed directly (never through a shell; a Windows `openclaw.cmd` shim is resolved to `node openclaw.mjs`). The OpenClaw CLI resolves its own credentials; echo-adk never reads or forwards an OpenClaw token. Health is never probed through the CLI.

## Non-negotiables on both sides

- Prime never forwards tokens or credentials to the service; the service is reached only on loopback and takes its own broker token from its systemd environment.
- No silent fallback anywhere: a failed provider ends the step with `unavailable` and names the provider.
- Owner-only on Prime (`/api/prime/adk/*`, `/api/prime/bots/*` classified `owner` in `server.ts`); the UI must not poll them while anonymous (see hotfix 6a973a2).
- Contract test on both sides: the service ships `tests/test_contract.py` asserting these shapes; Prime ships `tests/prime-adk.test.ts` / `tests/prime-bots.test.ts` against a stand-in worker implementing this file.

---

## Implementation notes (echo-adk 0.1.0, service side)

Copied from `echo-cli` `docs/echo-code/ECHO_ADK_API_CONTRACT.md` at `4ec3cd8` (2026-11-04, incl. the 20:12 agents addition).
Choices the contract leaves open, as implemented in `echo_adk.api` and asserted by `tests/test_contract.py`:

| Point | Choice |
|---|---|
| Malformed id in a path (`/runs/not-an-id`) | `404 not_found` (the route only matches 32 lowercase hex; nothing else is ever looked up) |
| Unknown route / known route with the wrong method | `404 not_found` / `405 invalid` |
| `POST /runs` with an unknown agent | `404 not_found` (body otherwise valid); malformed body -> `400 invalid` |
| `limit` above the maximum (50 runs / 200 messages) | clamped, not refused; non-integer or < 1 -> `400 invalid` |
| `budget_exceeded` as an HTTP error (service-wide daily cap on `POST /runs`) | `429` |
| Body larger than 1 MiB | `413 invalid` |
| Status of a stopped run | `done` with `proof.verdict = "UNKNOWN"` and no `error`; a queued run stops immediately, a running run after its current step |
| `spent.calls` | model calls + capability calls (each counts one against `budget.max_calls`) |
| `spent.cost_usd` | provider-reported cost when the model reply carries one, else the model's declared per-call cost (0 for `qwen-local` and subscription CLIs) |
| Run status after a `denied` envelope | `failed` with `error.code = "denied"` (`budget_exceeded` when a budget tripped); after a `gated` envelope `gated` with no `error` |
| `proof.verdict` | `PASS` final within budget, `FAIL` denied/unavailable/budget, `UNKNOWN` gated/stopped/out of steps |
| Lease supplied on `POST /runs` | must be exact ids inside the agent allowlist, no `*`; the API lease has no `grant_ref`, so tier >= 2 capabilities are always `gated` through the API (fail-closed) |
| `POST /agents` | body is an `EchoAgent` without `source` (a `source` key is `400 invalid`); `origin` may be given as `{by, prompt_sha256?}` and the service stamps `created_at`; absent -> `{by:"owner", created_at}`; the file is written atomically as `<agents-dir>/<name>.json` |
| `PUT /agents/{name}` | replace; the body `name` must equal the path name (`400 invalid` otherwise); a missing agent is `404 not_found` (PUT does not create); `origin.created_at` of the original is kept, `origin.updated_at` is added |
| `DELETE /agents/{name}` | `409 invalid` while any bot references the agent (`message` lists the bot ids); runs already recorded keep their `agent` name |
| File-shipped agents | have no `origin` (the key is omitted); `source` is the file name relative to `--agents-dir` |
| `daily_cost_cap_usd` on `POST /bots` | `0` is accepted (the Prime client sends 0 for the local lane); default when omitted is 1.0; maximum 5 |
| Group bot id that does not exist / duplicates / size outside 2..6 | `400 invalid` |
| Group turns and paused bots | a group turn runs even for a paused bot (the owner created the group); the per-bot daily cap still applies and refuses the turn |
| Bot `status` after a failed turn | `error` with `last_error`; `resume` clears it |
| `inbox_depth` | pending user messages whose run has not started |
| Message roles | `user` (inbox), `bot` (final text of the turn), `system` (refusals and failed turns) |
| Service identity | `GET /health` reports `profile: "ECHO_ADK"`, `service: "echo-adk-api"`, semver `version` |
| `channels` on `Bot` | always present (`[]` when none); `state` is evaluated at read time from a 30 s health cache |
| OpenClaw adapter off (FORGE default) | `ECHO_ADK_OPENCLAW` unset → every channel reads `unavailable: openclaw adapter off (...)`; bots are unaffected |
| Bad OpenClaw env | the service still starts; channels read `unavailable: openclaw adapter off: invalid configuration: <why>` |
| Inbound auth | the route is loopback-only like every other route; chat decisions additionally need approver + the chat the notification was delivered to |
| Approval consumption | the approval is marked decided before the re-run is minted, so a crash in between can never replay it; a mint that fails turns it `denied` with `reason` |
| CLI diagnostics | `last_error` and `notified.state` carry scrubbed text only (core redaction, Telegram/Discord bot-token shapes, URLs cut to scheme + host) |
| Reply routing | a turn that came from a chat answers that chat (if that chat was detached meanwhile, the reply is dropped, never sent to another chat); scheduled turns answer every channel; failures, refusals and gates go out as one approval notification, not as a reply |
