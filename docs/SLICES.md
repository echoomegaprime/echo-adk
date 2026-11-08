# Build slices (research doc D.2) and status

Twelve 2-4 h slices from `ECHO_ADK_RESEARCH.md` section D.2. This records the bootstrap plus unreleased
local automation work on `agent/echobots-autonomy-20261107`. Repository implementation is distinguished
from live integration and deployment evidence; a checked item is not a production certificate.

Legend: `[x]` delivered in this branch, `[~]` partially delivered (what is missing is stated), `[ ]` open.

- [x] **1. echo-adk repo bootstrap** - `pyproject` (py >= 3.10, zero-dep core, extras `adk`, `dev`), CI lint+test
  on 3.10/3.12, `SECURITY.md` stating the five rules, MIT license, README, CONTRIBUTING, CHANGELOG.
  *Deviation:* not started from `echo-repo-template`; `python -m echo_adk doctor` is replaced by
  `echo-adk validate agents/` (exit 0) and `GET /health`. The `echo.builds.log` receipt is the parent agent's step.
- [x] **2. Core primitives + envelope** - `Role, Skill, Capability, Lane(+Budget), Lease, Proof, EchoAgent, Envelope`
  dataclasses with strict JSON round-trip; `Capability.from_caps_detail()`. *Deviation:* schema validation
  against 20 live caps across 5 namespaces is not in the test suite (tests are offline); 12 caps were verified
  live on 2026-11-04 and shipped as `BUILTIN_CAPABILITIES`.
- [~] **3. EchoBroker** - `GateBroker`: key from env at call time (never logged), `invoke(capability, params, lease,
  run_id, step, tier)`, redaction, lease check before network. *Missing:* a live tier-0 call against FORGE
  (tests use a fake gate on loopback); `idempotency_key` is not sent (the gate envelope does not define it).
- [~] **4. EchoModel (no-token)** - `EchoModel(BaseLlm)` over `LoopbackModel`/`CliModel`; refuses `api_key`
  config; `ProviderUnavailable` names the provider; a forced failure yields exactly one model call (tested).
  *Missing:* the `echo.llm.call` / `echo.qwen.local.openai_chat` route with `allow_reroute=false` (the model
  path is the loopback OpenAI-compatible endpoint); an end-to-end `adk run` against FORGE Qwen.
- [x] **5. EchoToolset** - `EchoToolset(BaseToolset)` generating one `EchoCapabilityTool(BaseTool)` per allowlisted
  registered cap with a `FunctionDeclaration` from `input_schema`; default-deny; tier >= 2 refused without a
  lease `grant_ref`; `tool_filter` semantics; drift test vs the core policy. *Deviation:* no live tier-0
  executions in CI.
- [~] **6. EchoGovernancePlugin** - `before_tool` (classification/lease), `after_tool` (redact + `untrusted:true`),
  `on_*_error` (no silent retry), `after_run` (Proof to ledger). *Missing:* `after_run` -> `echo.continuity.capture`
  + `echo.builds.log` (needs the live broker; the Proof is written locally and `receipts` stays empty).
- [~] **7. EchoMemoryService + EchoArtifactService** - the core's local `MemoryStore` now supplies bounded,
  exact-scope recall and durable redacted write receipts in final envelope data. Integrity checks and
  legacy filename filtering are implemented in `echo_adk/core/ledger.py` and `echo_adk/core/runner.py`.
  *Missing:* the external service adapters, Crystal/ECF search+capture acknowledgments, and MinIO/ESH
  artifacts. A local receipt is not an external memory acknowledgment; `Proof.receipts` is unchanged.
- [x] **8. Skills bridge** - Agent-Skills loader/validator (`name` <= 64 kebab + dir match, `description` <= 1024,
  L1 -> L2 -> L3 references with traversal refusal); `echo-adk validate --skills-dir` reports violations.
  *Deviation:* `SkillToolset` wiring in the ADK adapter is not included; the fleet's 34 skills were not scanned in CI.
- [x] **9. MCP + A2A adapters** - zero-dep stdio MCP server (`initialize`, `tools/list`, `tools/call` through
  policy+broker; only JSON-RPC on stdout); A2A v1 agent card (`/.well-known/agent-card.json` shape) and
  `echo-a2a/1.0` translator both ways. *Deviation:* no `to_a2a` Starlette app (needs a2a-sdk); `McpToolset`/
  `RemoteA2aAgent` consumption is not exercised in CI.
- [~] **10. Deploy slice** - `deploy/echo-adk-api.service` (User=forge, loopback :8378, MemoryHigh/Max 300/400M,
  EnvironmentFile `/etc/echo/echo-adk.env`) + `deploy/README.md`. *Missing:* the unit is not installed on
  FORGE as part of this change; live installation, CertForge submit/verdict, Harbormaster manifest, and
  rollback drill are not verified by the repository tests. `DatabaseSessionService` remains open.
- [ ] **11. Eval harness** - not built (`.evalset.json`, `tool_trajectory_avg_score`).
- [ ] **12. TS parity ADR** - not written.

## Decisions taken in this branch (open questions E.*)

- **E.3 broker scope:** the kit mints exact-cap-id Leases per run/bot by default (R3 by construction); `*` is refused
  at every entry point (`Lease`, `POST /runs`, `POST /bots`).
- **E.4 Google dependency:** optional extra only; the core never imports `google.adk` (tested in a subprocess).
- **E.2 A2A collision:** both cards are produced; the Agent2Agent card is primary for ADK consumers, `echo-a2a/1.0`
  for the existing fleet. The long-term wire is still the Commander's call.
- **Agents from prompts (Commander 2026-11-04 20:12):** `POST/PUT/DELETE /agents` let Echo (or the owner) create
  definitions at runtime; each carries `origin:{by, prompt_sha256?, created_at}`; capabilities must already be in the
  default-deny registry, so a prompt cannot grant itself a new tool.
- **echoBots boundaries:** each turn has an exact-scope lease and each bot has a distinct local memory scope.
  The requested workspaces below add optional separate browser processes/profiles and encrypted per-bot OAuth
  records. No arbitrary shell or per-bot OS sandbox is provisioned; availability reflects actual configuration.
  Per-bot `daily_cost_cap_usd <= 5`, service-wide `ECHO_ADK_DAILY_CAP_USD`
  default 200, and the single-run 5 USD ceiling are enforced through the reservation path described below.

## Unreleased local automation and reliability

- [x] **Versioned API** - `echo_adk/api/server.py` retains v1 wire shapes and adds `/v2`, feature/connection
  reporting, budget receipts, calendar bot responses, and the event endpoint. V1 bot responses fail with an
  explicit 409 upgrade message for any schedule keys beyond `every_s`; v1 messages omit v2 event metadata.
  Host, Origin, Fetch Metadata, JSON type, and body-size checks protect the
  native loopback surface. Contract: `docs/CONTRACT_V2.md`, mirrored in
  `echo-cli/docs/echo-code/ECHO_BOTS_API_V2.md`.
- [x] **Schedules and event inbox** - `echo_adk/bots/schedules.py` defines local calendars, DST gap/fold rules,
  and missed-run policies; `triggers.py` defines bounded untrusted events. `manager.py` persists occurrence
  IDs, deduplicates events, serializes bot turns/groups, and recovers completed delivery without model replay.
- [x] **Budget admission** - `echo_adk/api/budgets.py`, `store.py`, and `service.py` atomically reserve the
  service and bot scopes for every service run path, settle once, retain holds across UTC midnight, and
  report uncertain interrupted costs. SQLite uses WAL with FULL synchronization. Cancellation releases
  unstarted holds; deleting a bot preserves fleet spend/uncertainty and existing shared group transcripts.
- [x] **Queued lease lifetime** - the immutable bot template is captured at admission and minted at dispatch;
  pausing does not consume its TTL or expand its scopes. Direct run expiry and legacy queued authorization
  remain fail-closed.
- [x] **Long-lived inbox lookup** - indexed message role/state projections replace truncated history scans,
  with transactional legacy backfill. Recent pending messages remain visible after large message histories.
- [ ] **External delivery gates** - OS/container browser isolation, Crystal/ECF and artifact service acknowledgments,
  authenticated upstream webhook adapters, live broker/model certification, FORGE installation evidence,
  CertForge/Harbormaster approval, and a rollback drill are not delivered by these local changes. Product
  comparisons need a defined evaluation set and measured results; no superiority claim is implied.

Focused regression files are `tests/test_contract_v2.py`, `tests/test_bot_automation.py`,
`tests/test_bot_schedules.py`, `tests/test_budget_reservations.py`, `tests/test_store_inbox.py`, and
`tests/test_memory_recall.py`. Deployment acceptance is separate from these local tests.

Migration adds budget and inbox projections without removing legacy memory. Downgrades must first settle
new holds and then restore a consistent backup or use a reviewed reconciliation; old binaries do not
maintain the new columns or read new scope-encoded memory paths. See `docs/CONTRACT_V2.md` for restart,
deletion, migration, and rollback semantics.

## Requested per-bot workspaces (2026-11-08)

The owner explicitly requested per-bot browsers, an OAuth marketplace and the remaining automation
features. This authorizes a narrow update to the former no-browser contribution guideline: headless
owned browser workers are now optional, while R1–R5 and production release admission remain intact.

- [x] Browser process/profile isolation, deny-by-default host egress, opaque controls, bounded
  screenshots, cleanup and cancellation. A browser context is not labeled a container/VM.
- [x] Per-bot OAuth catalog, S256 state/redirect binding, encrypted credentials, refresh and revocation;
  owner-only callback is excluded from general SDK auditing and model tool arguments.
- [x] Read-only authenticated polling, deterministic conditions, bounded retry horizons and durable
  event IDs; no unauthenticated public webhook receiver is introduced.
- [x] One-time/monthly/yearly/cron schedules, revisioned notes and portable teams/departments/templates
  with exact preview hashes, paused empty leases, interrupted-import inspection and conservative recovery.
- [x] Versioned APIs and 53 proposed SDK capability contracts; Prime has matching generated schemas,
  resource panels and 32 owner chat tools. Runtime configuration/consent is distinct from source availability.
- [ ] Production installation, provider credentials/consent, approved SDK registration and caller scopes,
  and exact-source production certification remain separately required. Code/tests do not manufacture them.
