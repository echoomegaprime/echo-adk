# echo-adk

**Echo Agent Development Kit**: an Echo-native agent core (Role, Skill, Capability, Lane, Lease, Proof)
with Google ADK, MCP and A2A adapters.

One small, zero-dependency way to *define* an Echo agent and to *run* it under Echo's existing governance
(ESH tiers and grants, the SOL broker, CertForge/Harbormaster proof, ECF/Crystal memory), so the same agent
file is loadable by the core runner, by Google ADK (`adk web/run`), by any MCP client and by Agent2Agent peers.

- Core: Python >= 3.10, **standard library only**. FORGE runs 3.12.3.
- Optional extra `echo-adk[adk]` brings `google-adk` (needs Python >= 3.11) for the ADK adapter.
- Five invariants are enforced in the core, by construction (see `SECURITY.md`): no tokens in agents (R1),
  no silent fallback (R2), exact-scope leases (R3), a Proof for every run (R4), fail-closed default-deny (R5).

## Install

```bash
git clone https://github.com/echoomegaprime/echo-adk
cd echo-adk
python -m pip install -e ".[dev]"      # core + pytest/ruff; add "adk" to the extras for Google ADK
python -m pytest -q                    # all green, no network, no credentials
```

## Five-minute quickstart

```bash
# 1. look at an agent definition (data, not code)
cat agents/knowledge-scout.json
# {"name":"knowledge-scout","role":"researcher","skills":["echo-researcher-power"],
#  "capabilities":["echo.knowledge.search","echo.knowledge.get","echo.caps.search"],
#  "lane":{"node":"forge","model":"qwen-local","budget":{"max_calls":40,"max_cost_usd":0,"max_wall_s":600}},
#  "max_steps":12,"instruction":"..."}

# 2. validate every agent (and, optionally, a skills directory)
echo-adk validate agents/                      # exit 0, {"ok": true, ...}

# 3. run it once; the gate key is read from ECHO_ADK_GATE_KEY, the model is FORGE Qwen on loopback
export ECHO_GATE_URL=http://127.0.0.1:8000           # the Echo SDK gate (loopback)
export ECHO_ADK_MODEL_ENDPOINT=http://127.0.0.1:11441 # OpenAI-compatible loopback endpoint (qwen-local lane)
export ECHO_ADK_GATE_KEY=...                          # the ONLY place a credential ever appears; never logged
echo-adk run agents/knowledge-scout.json "what does the Forge hold on ESH grants?"
# -> prints the Proof {run_id, lease_id, events_sha256, actions[], verdict}; exit 0 PASS / 1 FAIL / 2 UNKNOWN

# 4. serve the frozen HTTP contract Echo Prime consumes (loopback only)
echo-adk serve --port 8378 --state-dir ~/.echo/echo-adk --agents-dir agents
curl -s http://127.0.0.1:8378/health
curl -s -X POST http://127.0.0.1:8378/runs -H 'Content-Type: application/json' \
     -d '{"agent":"forge-steward","prompt":"How is node forge?"}'

# 5. the same agent as an MCP tool server or an A2A card
echo-adk mcp agents/knowledge-scout.json        # stdio JSON-RPC: initialize, tools/list, tools/call
echo-adk card agents/knowledge-scout.json       # Agent2Agent card (/.well-known/agent-card.json shape)
echo-adk card --format echo-a2a agents/knowledge-scout.json
```

From Python:

```python
from echo_adk.agents import load_agent
from echo_adk.core.broker import GateBroker
from echo_adk.core.models import build_model
from echo_adk.core.runner import Runner
from echo_adk.core.types import Lease, new_id

agent = load_agent("agents/knowledge-scout.json")
run_id = new_id()
lease = Lease.mint(run_id, agent.capabilities, ttl_s=600, max_uses=40)  # exact cap ids, never "*"
result = Runner(build_model(agent.lane.model), GateBroker()).run(agent, "what is new on ESH?", lease, run_id=run_id)
print(result.status, result.proof.verdict, result.final_text)
```

With Google ADK installed (`pip install "echo-adk[adk]"`):

```python
from echo_adk.adk import app

application = app(agent, model=build_model(agent.lane.model), broker=GateBroker())
# application is a google.adk App: EchoModel(BaseLlm) + EchoToolset(BaseToolset) under EchoGovernancePlugin(BasePlugin)
```

## How a run works (spec section 4)

```
lease check -> [ model step -> one JSON Action -> policy -> broker -> Envelope -> ledger ] x <= max_steps -> Proof
```

- The model is told the allowed capabilities and must answer with exactly one JSON object:
  `{"action":"cap","capability":"echo.knowledge.search","params":{...}}` or `{"action":"final","text":"..."}`.
  Anything else is a `denied` envelope and the run stops.
- **Policy** (`echo_adk.core.policy`): registry membership (default-deny) -> known tier and schema -> agent
  allowlist -> Lease (present, unexpired, uses left, exact id in scope) -> tier >= 2 needs a `grant_ref` ->
  budget -> params. Verdicts are `allowed | gated | denied`; a `gated` envelope carries `needs` and `how` so a
  human can mint the right Lease.
- **Broker** (`echo_adk.core.broker`): `POST {ECHO_GATE_URL}/sdk/invoke` with
  `{envelope_version:1, capability, params, context}`; the key comes from `ECHO_ADK_GATE_KEY` at call time;
  the Lease is checked locally *before* any network call; every response is an Envelope with `untrusted:true`
  and secrets redacted.
- **Envelope** (spec section 3, one shape for every event): `{envelope_version:1, run_id, step, kind, capability,
  tier, verdict, ok, untrusted:true, redacted, audit_id, duration_ms, needs, how, data}`.
- **Proof**: `{run_id, lease_id, events_sha256, actions[], artifacts[], receipts[], verdict}` with
  `PASS` (final within budget), `FAIL` (denied / provider unavailable / budget), `UNKNOWN` (gated, stopped,
  out of steps). The ledger is append-only JSONL under the state dir.

## Models (R1, R2)

| Lane model id | Path | Credentials |
|---|---|---|
| `qwen-local` | `LoopbackModel`: OpenAI-compatible `/v1/chat/completions` on a **loopback origin only** (`http://127.0.0.1:11441` by default), 1 MiB response cap | none, by construction |
| `claude`, `codex` (`gpt`), `grok`, `argon` (`agy`) | `CliModel`: the vendor CLI as a headless subprocess (ported from Prime `providers.ts`); child env allowlisted, drops `KEY\|TOKEN\|SECRET\|PASSWORD\|CREDENTIAL` | the CLI finds its own login; the agent never sees it |

A provider that is missing, signed out, timed out or unreadable raises `ProviderUnavailable(provider)` with the
message "...; no fallback was used". Nothing is retried on another provider. Models config refuses any
`api_key`-shaped field and any non-loopback endpoint.

## The contract (`docs/CONTRACT.md`)

`echo-adk serve` implements the frozen `echo-adk-api <-> Echo Prime` contract exactly: `GET /health`
(`profile:"ECHO_ADK"`), `GET /agents[/{name}]`, `POST /agents` (201, saved as `<agents-dir>/<name>.json` with
`origin:{by:"echo"|"owner", prompt_sha256?, created_at}`), `PUT|DELETE /agents/{name}`, `POST /runs` (202), `GET /runs[/{id}]`, `POST /runs/{id}/stop`,
`GET|POST /bots`, `GET|DELETE /bots/{id}`, `POST /bots/{id}/{resume|pause|message}`, `GET /bots/{id}/messages`,
`POST /groups` (202), `GET /groups/{id}`. All ids are 32 lowercase hex, every response is
`application/json`, errors are `{error, message}` with codes `not_found | invalid | gated | budget_exceeded |
unavailable`. Budgets are clamped to the agent lane and the service caps (120 calls, 5 USD, 4800 s).
`tests/test_contract.py` asserts every route, shape and status code; Prime's stand-in mirrors it.

**echoBots** are always-on governed agents: created `paused`, one Lease per turn from the bot's own template
(exact cap ids), one memory scope `bot:<id>`, `schedule.every_s >= 60`, an inbox (one run per message),
`daily_cost_cap_usd <= 5` enforced before every turn, groups of 2..6 bots taking round-robin turns over a shared
transcript. Deliberate divergence from Grok Bots: no shared computer, no shared shell, no shared lease, no tokens;
`DELETE` leaves nothing behind.

## Layout

```
echo_adk/
  core/     types (primitives + Envelope + Proof), policy (default-deny), models (loopback + CLI),
            broker (gate), runner (the loop), skills (Agent Skills loader), ledger, redact, log
  agents/   .json loader + strict mini-YAML subset + converter
  api/      the frozen contract: service, SQLite store, loopback ThreadingHTTPServer
  bots/     echoBots manager (scheduler, inbox, groups, daily caps)
  mcp/      stdio JSON-RPC MCP server (tools = the agent's allowlist)
  a2a/      Agent2Agent card + echo-a2a/1.0 translator
  adk/      Google ADK adapter (optional extra): EchoModel, EchoToolset, EchoGovernancePlugin, app()
  cli.py    echo-adk run | validate | serve | card | mcp
agents/     knowledge-scout.json, forge-steward.json      docs/examples/knowledge-scout.yaml
deploy/     echo-adk-api.service (systemd, FORGE) + runbook
docs/       CONTRACT.md (frozen contract + implementation notes), SLICES.md (12 build slices, status)
```

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `ECHO_GATE_URL` | Echo SDK gate origin (loopback unless `ECHO_ADK_GATE_ALLOW_REMOTE=1`) | `http://127.0.0.1:8000` |
| `ECHO_ADK_GATE_KEY` | the gate API key; read at call time, never stored or logged | unset -> capability calls are `unavailable` |
| `ECHO_ADK_GATE_KEY_ENV` | name of the variable that holds the key | `ECHO_ADK_GATE_KEY` |
| `ECHO_ADK_MODEL_ENDPOINT` / `ECHO_ADK_MODEL_NAME` | loopback OpenAI-compatible endpoint and model name for `qwen-local` | `http://127.0.0.1:11441` / `qwen` |
| `ECHO_ADK_STATE_DIR` | ledger, proofs, memory, SQLite | `~/.echo/echo-adk` |
| `ECHO_ADK_CAPS_FILE` | extra `Capability` records (or `echo.caps.detail` payloads) to extend the default-deny registry | unset |
| `ECHO_ADK_DAILY_CAP_USD` | service-wide daily spend cap across all runs and bots | `200` |
| `ECHO_ADK_DEBUG` | `1` for debug-level JSON logs on stderr | off |

## Status

Version 0.1.0 bootstraps the core, the contract service, echoBots, the MCP and A2A adapters and the Google ADK
adapter with 60+ offline tests. What each of the twelve research slices delivers, and what is still open
(live gate run, memory/artifact services, eval harness, FORGE deployment, TS parity), is tracked in
`docs/SLICES.md`. License: MIT, Echo Prime Tech LLC.
