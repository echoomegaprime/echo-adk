# Build slices (research doc D.2) and status

Twelve 2-4 h slices from `ECHO_ADK_RESEARCH.md` section D.2. Status as of the `agent/echo-adk-core-20261104`
branch (commit "feat: bootstrap echo-adk core, api, bots, mcp, a2a, adk adapter").

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
- [ ] **7. EchoMemoryService + EchoArtifactService** - not built. The core ships a per-scope `MemoryStore`
  (JSONL) used by bots; Crystal/ECF search+capture and MinIO/ESH artifacts are open.
- [x] **8. Skills bridge** - Agent-Skills loader/validator (`name` <= 64 kebab + dir match, `description` <= 1024,
  L1 -> L2 -> L3 references with traversal refusal); `echo-adk validate --skills-dir` reports violations.
  *Deviation:* `SkillToolset` wiring in the ADK adapter is not included; the fleet's 34 skills were not scanned in CI.
- [x] **9. MCP + A2A adapters** - zero-dep stdio MCP server (`initialize`, `tools/list`, `tools/call` through
  policy+broker; only JSON-RPC on stdout); A2A v1 agent card (`/.well-known/agent-card.json` shape) and
  `echo-a2a/1.0` translator both ways. *Deviation:* no `to_a2a` Starlette app (needs a2a-sdk); `McpToolset`/
  `RemoteA2aAgent` consumption is not exercised in CI.
- [~] **10. Deploy slice** - `deploy/echo-adk-api.service` (User=forge, loopback :8378, MemoryHigh/Max 300/400M,
  EnvironmentFile `/etc/echo/echo-adk.env`) + `deploy/README.md`. *Missing:* the unit is not installed on
  FORGE, no CertForge submit/verdict, no Harbormaster manifest, no rollback drill, no `DatabaseSessionService`.
- [ ] **11. Eval harness** - not built (`.evalset.json`, `tool_trajectory_avg_score`).
- [ ] **12. TS parity ADR** - not written.

- [~] **13. echoBots OpenClaw channel + approvals** (branch `agent/echobots-openclaw-20261105`) - adapter, inbound
  route, health gate, approvals with Approve/Deny, bridge hook, 42 tests. *Missing:* the bridge is not installed on
  the QUENCH gateway; QUENCH OpenClaw has only Discord configured (no Telegram account); the FORGE unit runs with the
  adapter off because the gateway and its CLI live on QUENCH; no live send was made.

## Decisions taken in this branch (open questions E.*)

- **E.3 broker scope:** the kit mints exact-cap-id Leases per run/bot by default (R3 by construction); `*` is refused
  at every entry point (`Lease`, `POST /runs`, `POST /bots`).
- **E.4 Google dependency:** optional extra only; the core never imports `google.adk` (tested in a subprocess).
- **E.2 A2A collision:** both cards are produced; the Agent2Agent card is primary for ADK consumers, `echo-a2a/1.0`
  for the existing fleet. The long-term wire is still the Commander's call.
- **Agents from prompts (Commander 2026-11-04 20:12):** `POST/PUT/DELETE /agents` let Echo (or the owner) create
  definitions at runtime; each carries `origin:{by, prompt_sha256?, created_at}`; capabilities must already be in the
  default-deny registry, so a prompt cannot grant itself a new tool.
- **echoBots divergence from Grok Bots (coordinator, 2026-11-04):** nothing shared between bots (lease, memory,
  shell, browser, credentials); `DELETE` leaves nothing behind (tested); per-bot `daily_cost_cap_usd <= 5` refuses a
  turn with `budget_exceeded`; service-wide `ECHO_ADK_DAILY_CAP_USD` default 200; single-run ceiling 5 USD.
