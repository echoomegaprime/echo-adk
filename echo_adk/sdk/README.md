# ECHO SDK host adapter

This package exposes 53 native v2 management/resource operations through an installable adapter
inside the existing ECHO SDK host. It contains real implementations and proposed
registration specifications. Importing it does not register capabilities, start
a server, grant permissions, or modify the live SDK registry.

## Install and run in the existing SDK host

Install the `sdk-host` extra into the SDK host's Python environment:

```sh
python -m pip install 'echo-adk[sdk-host]'
```

For a reviewed source checkout, use `python -m pip install -e '.[sdk-host]'`.
The optional extra pins FastAPI 0.162.2, httpx 0.28.1 and Uvicorn 0.54.0. The core
package still has no runtime dependencies. CI installs `.[dev,sdk-host,calendar,connections]` and runs
the ASGI tests as required tests. They do not silently skip missing dependencies.

Browser, OAuth, memory, polling and portability contracts are in `workspace.py`.
`scripts/export_prime_workspace_contract.py` exports the matching Prime client schemas.
The OAuth callback is deliberately not an SDK operation: one-use authorization code/state must
not pass through a general gate that might audit application parameters. Prime sends that owner
protocol response directly to the native callback endpoint. Models receive neither code nor state.

The native worker's calendar schedules also need IANA timezone data. Install
`echo-adk[calendar]` in the worker environment on Windows or images without a
system timezone database. The optional extra supplies `tzdata==2026.6`; it is
separate from the SDK host when host and worker use different environments.

Preserve the existing SDK service's working directory and startup options, and
use the application factory target:

```sh
uvicorn echo_adk.sdk.host:create_app --factory
```

Configure these operator-owned environment variables in that service:

| Variable | Required value |
| --- | --- |
| `ECHO_ADK_OWNER` | Exact owner returned by the SDK's verified key authentication helper. |
| `ECHO_ADK_WORKER_ORIGIN` | Native HTTP loopback origin of the ADK worker, including its actual port. |

The factory imports the SDK's existing `main.app`, installs its middleware and
router before serving, registers a pre-dispatch hook, and returns that same app.
It fails if the app has already started or required SDK helpers are unavailable.
Using the same `main.app` matters: the SDK's verified local router dispatcher
calls this app in process through `httpx.ASGITransport`. A separate remote target
does not inherit the admission proof and is refused.

This package does not include the ECHO SDK server, its credential store, or its
deployment configuration. Run the factory in the existing, configured SDK host.

## Admission boundary

Every proposed operation remains classified Tier 2, including the control
plane's private reads. Required scopes are limited to the relevant control plane:

| Proposed namespace | Fixed required scope |
| --- | --- |
| `echo.adk.*` — health, agents and runs | `adk.manage` |
| `echo.echobots.*` — bots and groups | `echobots.manage` |

The caller's authenticated key must cover the applicable named scope through the
SDK's real scope matcher. A broad static `tier:2` grant is not required by this
adapter. The SDK independently requires HMAC for
`danger_tier >= 2`; these named scopes do not lower that risk classification.
Creation and execution controls remain outside automatic tier 0/1 child-worker
approval, and the child runner still requires an exact capability allowlist,
lease and Tier 2 `grant_ref`. Host-owned chat management tools retain their
separate owner admission and bounded delegation rules.

The SDK gate continues to perform its actual key, scope, HMAC, policy and other
required checks. Only its pre-dispatch hook, which runs after those checks, can
arm the adapter's per-invocation proof. The hook also checks that registration
retains exactly Tier 2 and the fixed scope above. The proof is bound to that
admitted scope, the exact capability, projected application parameters, and
forwarded key. The target
consumes it once before awaiting authentication or native worker I/O. The outer
invocation revokes unused proofs in `finally`, including a copy inherited by a
background task. Nested and concurrent invocations keep separate frames.

The target then calls the real SDK `_authenticate` helper again and checks the
exact configured owner, active key state, and the actual key's coverage of the
fixed named scope. Both the single-use post-gate proof and that authenticated
scope are required. The adapter does not create grants or reinterpret claimed
permissions in a request.

Task-context-only grants are currently refused, even when the SDK pre-dispatch
hook ran. This adapter has no verified contract for binding a task's authority
to the authenticated caller. Context identifiers, a matching grant record, or
an asserted owner equality cannot replace the key's named scope. Supporting
task grants requires that binding contract from the SDK owner and additional
authorization checks; this package does not invent such a contract.
`X-Commander-Email`, `X-Echo-Capability`, an owner API key on a direct target call,
or a model-supplied `owner`/`scopes` field cannot substitute for gate admission.
Credentials and authorization context are never application parameters.

Admitted operations use the actual native v2 HTTP API. They preserve its current
registry, lease, spending, paused-queue, lifecycle and event-idempotency behavior.
There is no direct Store/Runner access, configurable destination per call,
redirect following, environment proxy use, or automatic mutation retry. An
uncertain transport result is reported as `outcome: unknown`; callers must read
the existing receipt or recover an event by its original ID before retrying work.

## Reviewable registration manifest

Generate specs for the actual native loopback origin of the existing SDK gate:

```sh
python -m echo_adk.sdk.manifest --gate-origin http://127.0.0.1:ACTUAL_PORT
```

Replace `ACTUAL_PORT` with the verified service port. The command prints JSON and
makes no network call. Its outer `registration_status` is
`proposed_not_registered`. Each capability specification uses the observed
registrar fields, `handler_kind: router`, `target_method: POST`,
`args_mode: json_body`, a closed input schema and an explicitly enforced output
schema. Unknown registrar defaults remain the registrar's responsibility.

The manifest is an installation input, not evidence that these IDs are active.
Registration must use the real SDK registrar and its normal owner/Tier 2
admission; do not use its `gen_router_stub` option. Before and after registration,
verify each exact ID and schema using the real active catalog, then exercise the
normal SDK gate. No registration or deployment is performed by these modules.
Confirm that the registrar itself is active and admitted before submitting; a
retained detail row does not establish an active capability lifecycle.

| Operation | Proposed capability | Native route |
| --- | --- | --- |
| `health` | `echo.adk.health` | `GET /v2/health` |
| `agent.list` | `echo.adk.agent_list` | `GET /v2/agents` |
| `agent.get` | `echo.adk.agent_get` | `GET /v2/agents/{name}` |
| `agent.create` | `echo.adk.agent_create` | `POST /v2/agents` |
| `agent.replace` | `echo.adk.agent_replace` | `PUT /v2/agents/{name}` |
| `agent.delete` | `echo.adk.agent_delete` | `DELETE /v2/agents/{name}` |
| `run.create` | `echo.adk.run_create` | `POST /v2/runs` |
| `run.list` | `echo.adk.run_list` | `GET /v2/runs` |
| `run.get` | `echo.adk.run_get` | `GET /v2/runs/{run_id}` |
| `run.stop` | `echo.adk.run_stop` | `POST /v2/runs/{run_id}/stop` |
| `bot.list` | `echo.echobots.list` | `GET /v2/bots` |
| `bot.create` | `echo.echobots.create` | `POST /v2/bots` |
| `bot.get` | `echo.echobots.get` | `GET /v2/bots/{bot_id}` |
| `bot.update` | `echo.echobots.update` | `PUT /v2/bots/{bot_id}` |
| `bot.pause` | `echo.echobots.pause` | `POST /v2/bots/{bot_id}/pause` |
| `bot.resume` | `echo.echobots.resume` | `POST /v2/bots/{bot_id}/resume` |
| `bot.stop` | `echo.echobots.stop` | `POST /v2/bots/{bot_id}/stop` |
| `bot.delete` | `echo.echobots.delete` | `DELETE /v2/bots/{bot_id}` |
| `bot.message` | `echo.echobots.message` | `POST /v2/bots/{bot_id}/message` |
| `bot.messages` | `echo.echobots.messages` | `GET /v2/bots/{bot_id}/messages` |
| `bot.event` | `echo.echobots.event` | `POST /v2/bots/{bot_id}/events` |
| `group.create` | `echo.echobots.group_create` | `POST /v2/groups` |
| `group.get` | `echo.echobots.group_get` | `GET /v2/groups/{group_id}` |

Application fields are flat. For example, an event uses `bot_id`, `event_id`,
`source`, and `text` directly in SDK `params`; it never nests them under
`options`. The SDK's transport `command` is omitted from the closed application
schema so the verified dispatcher removes it before validation/target dispatch.

Successful target responses are
`{ok:true, operation, data, untrusted:true}`. Errors are
`{ok:false, operation, error, message, outcome, untrusted:true, details?}` with the
native HTTP status preserved for intentional failures. The SDK wraps the target
as `{status_code, body}`. Consumers must inspect both the SDK and application
outcomes: an outer `status: ok` with `body.ok: false` is a failure.

## Verification and limits

`tests/test_sdk_operations.py` exercises every operation against the actual
native v2 server with a fake broker/model and no execution submission. It checks
strict fields, typed budget/lifecycle errors, event receipts, redaction, bounded
responses, refusal of redirects, and false-success responses.

`tests/test_sdk_host.py` runs real FastAPI/httpx ASGI transport around the actual
native HTTP server. It checks dedicated key scopes, task-context-only grant
refusal after gate admission, revoked/expired/mismatched grant refusal, independent HMAC
refusal before hooks, direct-call and owner/key/scope rejection,
parameter/key/capability binding, one-use proofs, expiry, later-hook refusal,
inherited-context revocation, nested/concurrent isolation and the factory import
contract. The test SDK helpers model server-side grant state and verifier
decisions in the ordering verified from the real host. They do not claim live
grant issuance or acceptance, production HMAC verification, registration,
deployment or live model/capability execution.
