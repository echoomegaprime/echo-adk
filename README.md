# echo-adk

**Echo Agent Development Kit**: an Echo-native agent core (Role, Skill, Capability, Lane, Lease, Proof)
with Google ADK, MCP and A2A adapters.

One small, zero-dependency way to *define* an Echo agent and run it with exact-scope leases, capability
policy, local execution proofs, and scoped local memory. The same definition can be loaded by the core
runner and its Google ADK, MCP, and Agent2Agent adapters. Live broker/model certification, Crystal/ECF
memory, and artifact-service integration remain separate delivery gates in `docs/SLICES.md`.

- Core: Python >= 3.10, **standard library only**. FORGE runs 3.12.3.
- Optional extra `echo-adk[adk]` brings `google-adk` (needs Python >= 3.11) for the ADK adapter.
- Five invariants are enforced in the core, by construction (see `SECURITY.md`): no tokens in agents (R1),
  no silent fallback (R2), exact-scope leases (R3), a Proof for every run (R4), fail-closed default-deny (R5).

## Install

```bash
git clone https://github.com/echoomegaprime/echo-adk
cd echo-adk
python -m pip install -e ".[dev,sdk-host,calendar]" # complete offline test dependencies; Google ADK is separate
python -m pytest -q                    # offline fixtures, no external provider credentials
```

Calendar schedules need an IANA timezone database. On Windows and minimal images without system
timezone data, install the worker with `python -m pip install -e ".[calendar]"`. This optional extra
provides the first-party `tzdata` package; the core package still has no runtime dependencies. Unknown
zones or missing data produce an explicit error rather than silently scheduling in another timezone.
See [Python's timezone data requirements](https://docs.python.org/3/library/zoneinfo.html#data-sources).

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

# 4. serve v1 and the additive v2 HTTP API (native loopback clients only)
echo-adk serve --port 8378 --state-dir ~/.echo/echo-adk --agents-dir agents
curl -s http://127.0.0.1:8378/v2/health
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

The runner checks wall time before dispatch and after synchronous model, SDK, and durable final writes.
A result arriving after `max_wall_s` fails with `budget_exceeded`; it cannot become a successful final or
start another capability call. Completed receipts and costs remain recorded, and `spent.wall_s` reports
actual elapsed time. Unknown in-flight model costs retain their conservative budget reservation.

Built-in HTTP and CLI waits use the smaller of their configured timeout and the run's remaining wall
allowance. This is cooperative cancellation: a custom synchronous provider may ignore that limit, and
remote work already dispatched may continue after a local socket timeout. Late results are refused;
the runner does not automatically replay work or change providers.

## Models (R1, R2)

| Lane model id | Path | Credentials |
|---|---|---|
| `qwen-local` | `LoopbackModel`: OpenAI-compatible `/v1/chat/completions` on a **loopback origin only** (`http://127.0.0.1:11441` by default), 1 MiB response cap | none, by construction |
| `claude`, `codex` (`gpt`), `grok`, `argon` (`agy`) | `CliModel`: the vendor CLI as a headless subprocess (ported from Prime `providers.ts`); child env allowlisted, drops `KEY\|TOKEN\|SECRET\|PASSWORD\|CREDENTIAL` | the CLI finds its own login; the agent never sees it |

A provider that is missing, signed out, timed out or unreadable raises `ProviderUnavailable(provider)` with the
message "...; no fallback was used". Nothing is retried on another provider. Models config refuses any
`api_key`-shaped field and any non-loopback endpoint.

## HTTP contracts and echoBots

The existing v1 wire shapes are documented in [`docs/CONTRACT.md`](docs/CONTRACT.md): `GET /health`
(`profile:"ECHO_ADK"`), `GET /agents[/{name}]`, `POST /agents` (201, saved as `<agents-dir>/<name>.json` with
`origin:{by:"echo"|"owner", prompt_sha256?, created_at}`), `PUT|DELETE /agents/{name}`, `POST /runs` (202), `GET /runs[/{id}]`, `POST /runs/{id}/stop`,
`GET|POST /bots`, `GET|DELETE /bots/{id}`, `POST /bots/{id}/{resume|pause|message}`, `GET /bots/{id}/messages`,
`POST /groups` (202), `GET /groups/{id}`. IDs are 32 lowercase hex, JSON errors retain `{error, message}`,
and run budgets are clamped to the agent lane and service limits (120 calls, 5 USD, 4800 s).

[`docs/CONTRACT_V2.md`](docs/CONTRACT_V2.md) documents the same routes under `/v2`, calendar schedules,
`POST /v2/bots/{id}/events`, paused bot editing, cooperative stop, budget accounting, and truthful local connection status. V1 clients receive
an explicit 409 upgrade response when a bot's schedule has any key beyond `every_s`, including calendar
schedules or an explicit `missed` policy. V1 messages omit event metadata; v2 messages retain it. Both
versions require a valid loopback Host, reject browser Origin headers, and require JSON for nonempty
mutation bodies.

**echoBots** start paused. Each turn gets a fresh lease at dispatch from its admitted immutable template
and uses the exact memory scope `bot:<id>`. Interval and local-calendar schedules have explicit missed-run
and DST behavior. Inbox events have bounded durable idempotency; groups of 2–6 bots reserve their members
and execute sequential turns without overlapping those bots' normal inbox/schedule work.

Every service run atomically reserves against the service allowance and applicable bot allowance before
work. Rejection is immediate with 429 `budget_exceeded`; completion releases unused capacity. Outstanding
holds survive midnight, and uncertain interrupted costs are reported separately. Bot deletion removes
its local state but retains aggregate service charges and previously shared group transcript records.

Local memory recall is bounded, redacted, and treated as untrusted history. Successful final envelopes
include a durable local memory receipt. This is not a Crystal/ECF acknowledgment. No isolated browser
provider or per-bot OS sandbox is configured; v2 health reports the browser as unavailable. Implementations
are in `echo_adk/api/{server,service,store,budgets}.py`, `echo_adk/bots/{manager,schedules,triggers}.py`, and
`echo_adk/core/{runner,ledger}.py`.

## SDK integration

The outbound broker follows the actual Echo SDK envelope: flat application fields with a verified
`command`, exact active-catalog membership, current tier/scope metadata, and successful outer HTTP,
SDK status, target HTTP status, and target success flags. Detail lookup alone does not establish active
registration. Newly requested IDs can be hydrated from the live catalog instead of requiring a hand-edited
seed file; the gate still validates its authoritative schema and enforces the credential's scopes.
The catalog's help parameters are a partial description, so they are not represented as a complete
JSON schema. Gate run IDs and traces remain routing evidence in the capability envelope, not signed
certification. Redirects are refused and the broker never retries an uncertain operation.

For an already admitted privileged lease, the broker supports the gate's exact pre-dispatch HMAC
challenge using an explicitly configured per-key signer. The trusted host supplies the secret through
its existing secret store; ADK does not retrieve credential tables, mint keys, grant scopes, or answer
TOTP/approval challenges. A missing signer is reported as unavailable.

[`echo_adk/sdk/README.md`](echo_adk/sdk/README.md) describes the concrete inbound SDK host adapter for
all 23 v2 operations, its strict schemas, single-use gate admission, and proposed registrations.
The registration manifest is reviewable source data. Installing this package does not register or
activate it in a live SDK catalog. Existing legacy `echo.bot.*` notification capabilities are separate.

Installed skill guidance can be enabled with `ECHO_ADK_SKILLS_DIR`. Selected bot plugins must resolve
there; their redacted guidance and digest enter the actual model run, without adding capability authority.
Health and run receipts distinguish configured/applied guidance from legacy descriptive skill names.

## Layout

```
echo_adk/
  core/     types (primitives + Envelope + Proof), policy (default-deny), models (loopback + CLI),
            broker (gate), runner (the loop), skills (Agent Skills loader), ledger, redact, log
  agents/   .json loader + strict mini-YAML subset + converter
  api/      v1/v2 service, SQLite store and reservations, loopback ThreadingHTTPServer
  bots/     echoBots manager, interval/calendar schedules, idempotent event inbox, groups
  mcp/      stdio JSON-RPC MCP server (tools = the agent's allowlist)
  a2a/      Agent2Agent card + echo-a2a/1.0 translator
  adk/      Google ADK adapter (optional extra): EchoModel, EchoToolset, EchoGovernancePlugin, app()
  sdk/      all v2 operation adapters, optional real SDK host factory, proposed registration manifest
  cli.py    echo-adk run | validate | serve | card | mcp
agents/     knowledge-scout.json, forge-steward.json      docs/examples/knowledge-scout.yaml
deploy/     echo-adk-api.service (systemd, FORGE) + runbook
docs/       CONTRACT.md (v1), CONTRACT_V2.md (additive API + migration), SLICES.md (build status)
```

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `ECHO_GATE_URL` | Echo SDK gate origin (loopback unless `ECHO_ADK_GATE_ALLOW_REMOTE=1`) | `http://127.0.0.1:8000` |
| `ECHO_ADK_GATE_KEY` | the gate API key; read at call time, never stored or logged | unset -> capability calls are `unavailable` |
| `ECHO_ADK_GATE_KEY_ENV` | name of the variable that holds the key | `ECHO_ADK_GATE_KEY` |
| `ECHO_ADK_GATE_HMAC_SECRET` | admitted per-key 64-hex signer supplied by the host secret store; used only for an exact privileged HMAC challenge | unset |
| `ECHO_ADK_GATE_HMAC_SECRET_ENV` | name of the variable containing that signer | `ECHO_ADK_GATE_HMAC_SECRET` |
| `ECHO_ADK_MODEL_ENDPOINT` / `ECHO_ADK_MODEL_NAME` | loopback OpenAI-compatible endpoint and model name for `qwen-local` | `http://127.0.0.1:11441` / `qwen` |
| `ECHO_ADK_STATE_DIR` | ledger, proofs, memory, SQLite | `~/.echo/echo-adk` |
| `ECHO_ADK_CAPS_FILE` | extra `Capability` records (or `echo.caps.detail` payloads) to extend the default-deny registry | unset |
| `ECHO_ADK_SKILLS_DIR` | host-owned directory of installed named skill folders | unset; skill labels are metadata and explicit bot plugins are unavailable |
| `ECHO_ADK_DAILY_CAP_USD` | service-wide UTC settlement-day cap, including all unfinished holds | `200` |
| `ECHO_ADK_DEBUG` | `1` for debug-level JSON logs on stderr | off |

## Status

Version 0.1.0 provides the core and adapters; the unreleased v2 work adds durable local automation,
budget admission, and memory recall. Focused coverage lives in `tests/test_contract_v2.py`,
`tests/test_budget_reservations.py`, `tests/test_bot_automation.py`, `tests/test_bot_schedules.py`,
`tests/test_store_inbox.py`, and `tests/test_memory_recall.py`.

The live gate/model path, Crystal/ECF and artifact services, browser isolation, evaluation harness,
FORGE deployment certification, and rollback drill remain separate work in [`docs/SLICES.md`](docs/SLICES.md).
Before downgrading, settle held reservations and use a consistent backup or reviewed state migration;
older binaries do not maintain the new accounting/inbox projections or discover new memory filenames.
See the migration and rollback section of [`docs/CONTRACT_V2.md`](docs/CONTRACT_V2.md).

License: MIT, Echo Prime Tech LLC.
