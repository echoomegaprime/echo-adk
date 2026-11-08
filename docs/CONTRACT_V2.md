# ECHO Bots API v2

This document describes the local API implemented in the current source tree. It is an additive contract, not a deployment or live-provider certification. The v1 contract remains in `echo-adk/docs/CONTRACT.md` and `echo-cli/docs/echo-code/ECHO_ADK_API_CONTRACT.md`. This document is mirrored verbatim at `echo-cli/docs/echo-code/ECHO_BOTS_API_V2.md`.

## 1. Versioning and transport

The same loopback service exposes existing routes without a prefix and v2 routes under `/v2`. There is no `/v1` prefix. Clients should request `GET /v2/health` and check `contract_version: 2` before offering calendar schedules or event delivery. An older service returning 404 has not implemented v2; a network or provider failure must not be presented as a successful downgrade.

All responses use `application/json`, `Cache-Control: no-store`, and `X-Content-Type-Options: nosniff`. IDs remain 32 lowercase hexadecimal characters. Errors retain the `{error, message}` envelope and may include structured fields. Existing v1 health and run response keys remain unchanged.

The service binds only to loopback. Its HTTP admission checks apply to both contract versions:

- Exactly one `Host` header must name `localhost` or a loopback IP, with valid optional port syntax and no user information, path, query, or fragment.
- Any `Origin` header is refused, including `Origin: null`. `Sec-Fetch-Site` must be absent or `none`.
- A nonempty mutation body must have `Content-Type: application/json`. The maximum body is 1 MiB; malformed UTF-8 or JSON is rejected.
- A rejected host or browser-origin request returns 403 `denied`; unsupported body media types return 415 `invalid`; an oversized body returns 413 `invalid`.

Prime's server-side adapter is the intended native client. It must not forward browser `Origin` or Fetch Metadata headers. These controls prevent direct browser writes and hostname rebinding; they do not authenticate individual local processes or make this worker an internet webhook receiver. Public authentication and upstream event verification belong in the calling gateway/adapter.

Implementation: `echo-adk/echo_adk/api/server.py` (`Handler`, `ROUTES`, `V2_ROUTES`, `make_server`).

## 2. Routes

V2 includes every existing route with `/v2` prepended. Unless noted below, request and response schemas match v1.

| Method and v2 path | Success | Behavior |
|---|---:|---|
| `GET /v2/health` | 200 | Existing health plus version, feature, budget, and connection information. |
| `GET /v2/agents`, `GET /v2/agents/{name}` | 200 | Existing agent listing and definition. |
| `POST /v2/agents` | 201 | Create a validated agent definition. |
| `PUT /v2/agents/{name}`, `DELETE /v2/agents/{name}` | 200 | Existing replacement/deletion rules. |
| `POST /v2/runs` | 202 | Admit and queue a run, or refuse it before work. |
| `GET /v2/runs` | 200 | Existing run summaries and pagination. |
| `GET /v2/runs/{id}` | 200 | Existing run plus `budget_accounting`. |
| `POST /v2/runs/{id}/stop` | 200 | Cancel unstarted work or request cooperative stop of active work. |
| `GET /v2/bots`, `GET /v2/bots/{id}` | 200 | Existing bot fields, scheduling timestamps when available, and `budget`. |
| `POST /v2/bots` | 201 | Create a paused bot, including interval or calendar scheduling. |
| `PUT /v2/bots/{id}` | 200 | Update future configuration on a paused bot with no unfinished work. |
| `POST /v2/bots/{id}/resume`, `POST /v2/bots/{id}/pause` | 200 | Existing explicit lifecycle controls. |
| `POST /v2/bots/{id}/stop` | 200 | Pause new dispatch and request cancellation of this bot's queued and active runs. |
| `DELETE /v2/bots/{id}` | 200 | Delete eligible bot state; active work returns 409. |
| `POST /v2/bots/{id}/message` | 202 | Queue one inbox message and one budget-admitted run. |
| `GET /v2/bots/{id}/messages` | 200 | Existing message pagination; event messages also identify their source. |
| `POST /v2/bots/{id}/events` | 202 | New durable, idempotent event admission. |
| `POST /v2/groups` | 202 | Atomically admit 2–6 distinct bots for a round-robin group. |
| `GET /v2/groups/{id}` | 200 | Existing group transcript and status. |

The event route does not exist in v1. Common error codes are `invalid`, `not_found`, `gated`, `budget_exceeded`, `unavailable`, `denied`, `conflict`, and `queue_full`. Clients should render the returned error and never infer permission from a retry.

### V1 behavior for schedules and event messages

`POST /bots` accepts the existing interval shape `{"every_s": 60}`; calendar fields or the new `missed` field require `POST /v2/bots` and return 400 through v1. V1 bot responses omit the new scheduling timestamps.

A v1 request that would return a bot whose schedule contains any key beyond `every_s` returns 409 `invalid` with an explicit upgrade message. This includes calendar schedules and interval schedules with an explicit `missed` policy, even `missed: "once"`. The check applies to `GET /bots` when any listed bot has such a schedule, `GET /bots/{id}`, and that bot's pause/resume responses. Pause/resume checks the version before mutating the bot. The bot is not hidden, flattened into a legacy interval, or silently changed. Other unchanged routes, such as inbox message admission and deletion, keep their existing semantics.

V1 `GET /bots/{id}/messages` returns the existing message fields and omits the new `event` metadata, including for messages admitted through v2. `GET /v2/bots/{id}/messages` retains that metadata. The underlying message and run IDs are the same in both versions.

## 3. Health and readiness information

`GET /v2/health` adds these fields to the existing service health:

```json
{
  "contract_version": 2,
  "features": {
    "calendar_schedules": true,
    "idempotent_events": true,
    "memory_recall": true,
    "bot_editing": true,
    "bot_stopping": true,
    "extended_schedules": true,
    "bot_workspaces": true,
    "oauth_marketplace": true,
    "editable_memory": true,
    "portable_teams": true,
    "connection_events": true
  },
  "connections": {
    "memory": {"provider": "local", "recall": true, "durable": true, "shared": false},
    "browser": {"available": false, "isolation": "process_and_profile", "os_isolation": false},
    "oauth": {"configured": false, "per_bot": true},
    "skills": {"configured": false, "provider": "installed", "scripts_execute_automatically": false}
  }
}
```

The response also contains `budgets`, described in section 6. These fields identify implemented local capabilities. `ok: true` means the API is responding; it is not evidence that a provider is logged in, the SDK gate is reachable, Crystal/ECF acknowledged a write, or deployment certification passed. Browser health is a nonblocking cached runtime observation. The per-bot browser status route performs a bounded real probe when required; dependency presence alone does not establish availability. Browser profiles/processes share the service OS account.

The skill connection reports whether `ECHO_ADK_SKILLS_DIR` points to an installed skill library. Explicit nonempty bot `plugins` require that library and validated names; an unconfigured library returns 503 before bot creation or editing. Existing agent `skills` labels remain descriptive metadata when no library is configured, and the first model envelope reports `skill_context.provider: "unconfigured"` with `applied: []`. When configured, the runner loads the agent's skills plus the bot's plugins from that host-owned root, checks names and path containment, redacts the guidance, bounds the combined context to 64,000 characters, and records the applied names and SHA-256 in `data.skill_context`. Missing or oversized installed guidance fails before model invocation. Queued bot work retains its selected skill names. Guidance does not grant tools or automatically execute bundled scripts.

Implementation: `echo-adk/echo_adk/api/server.py` and `echo-adk/echo_adk/api/service.py` (`health`, `budget_health`).

## 4. Bots and schedules

Existing bot creation fields remain available: `name`, `agent`, `mode`, `schedule`, `instruction`, `plugins`, `lease`, and `daily_cost_cap_usd`. Unknown fields are rejected. Bots start `paused`. `mode` is `personal` or `team`; it does not create a separate authentication boundary. A bot's daily cap is between 0 and 5 USD, inclusive.

### Editing and stopping

`PUT /v2/bots/{id}` accepts a nonempty partial object containing `name`, `mode`, `schedule`, `instruction`, `plugins`, `lease`, or `daily_cost_cap_usd`. The bot ID, agent, memory scope, and creation time are immutable. Editing requires the bot to be paused, its inbox to have no pending work, and any active or reserved group work to have finished; otherwise the service returns 409 `conflict`. The update does not resume the bot.

Omitted fields retain their values. `schedule: null` clears scheduling, `instruction: null` or an empty string clears the instruction, and `plugins: []` clears selected skills. A partial lease retains omitted limits and scopes; `scopes: []` revokes all capability use. Scopes must remain inside the agent's allowlist. A changed schedule gets a newly computed deadline. The update and stop routes are v2-only.

`POST /v2/bots/{id}/stop` accepts only `{}` and returns `{bot_id, status, stop_requested: true, run_ids: [...]}`. It pauses future dispatch and requests cancellation of the queued and active runs captured for that bot. Unstarted work is cancelled without replay; in-flight provider calls can finish before the cooperative stop takes effect. The runner will not start a newly proposed capability after observing cancellation. A returned `status: "running"` is an acknowledgement of the request, not a claim that the worker has already stopped. Read the bot and run states to confirm completion. Stopping one group member does not cancel the other members' work, and a still-active group reservation prevents configuration changes. New inbox items can still be admitted while the bot is paused.

An interval schedule accepts an integer `every_s` from 60 through 604800 and optional `missed`:

```json
{"every_s": 300, "missed": "once"}
```

The legacy interval default is `missed: "once"`. Intervals are measured from dispatch, including an inbox dispatch, so delayed ticks do not create bursts of closely spaced interval runs. An interval bot without a previous deadline can run on its first eligible tick after resume.

A calendar schedule accepts a local wall time, IANA timezone, selected weekdays, and optional missed-run policy:

```json
{
  "name": "Morning review",
  "agent": "knowledge-scout",
  "schedule": {
    "at": "09:00",
    "tz": "America/Chicago",
    "weekdays": [1, 2, 3, 4, 5],
    "missed": "skip"
  }
}
```

`at` is zero-padded 24-hour `HH:MM`. Weekdays use ISO numbering, Monday = 1 through Sunday = 7, with 1–7 distinct values. An unavailable or unknown timezone is rejected without a timezone fallback. Calendar schedules default to `missed: "skip"`.

| Scheduling condition | Behavior |
|---|---|
| Nonexistent local time during a spring DST transition | Skip that occurrence. |
| Repeated local time during an autumn transition | Use only its first occurrence. |
| Tick up to 60 seconds after a deadline | Permit normal scheduler jitter. |
| Older missed deadline with `skip` | Advance to the next occurrence without a catch-up run. |
| Older missed deadline with `once` | Coalesce missed work into at most one run, then advance. |
| Pause followed by resume with `skip` | Advance an elapsed deadline; do not replay paused work. |
| Pending inbox work | Dispatch it before creating a new scheduled turn. |

`next_run_at` and `last_scheduled_at`, when present, are UTC ISO timestamps. The scheduled run ID and next deadline are persisted atomically with budget admission. If the process stops before the worker begins, recovery keeps that exact queued run instead of generating the occurrence again. This prevents duplicate local admission; it does not claim exactly-once execution at an external service.

Each bot has one active turn. A group reserves all selected bots before starting and prevents their inbox/schedule turns from overlapping it. Explicit group creation may select paused bots for that requested group; successful completion restores their previous paused state. A failed turn may leave a bot in `error`, which also requires explicit resume. Creating a group does not permanently resume those bots.

Implementation: `echo-adk/echo_adk/bots/schedules.py`, `echo-adk/echo_adk/bots/manager.py`; focused coverage is in `echo-adk/tests/test_bot_schedules.py` and `echo-adk/tests/test_bot_automation.py`.

## 5. Idempotent event inbox

`POST /v2/bots/{id}/events` accepts exactly these three fields:

```json
{"event_id": "push-123", "source": "github", "text": "A review is ready."}
```

- `event_id`: 1–128 ASCII characters, starting with a letter or digit; subsequent characters may also be `.`, `_`, `:`, `/`, or `-`.
- `source`: 1–64 lowercase ASCII letters, digits, `.`, `_`, or `-`, starting with a letter or digit.
- `text`: nonempty valid Unicode, at most 10,000 characters. Credentials are redacted before run/message persistence. Credential-shaped identifiers are rejected.

The response is HTTP 202 for both new and duplicate delivery:

```json
{
  "event_id": "push-123",
  "message_id": "11111111111111111111111111111111",
  "run_id": "22222222222222222222222222222222",
  "duplicate": false,
  "status": "queued"
}
```

Idempotency is scoped to `(bot_id, source, event_id)`. A repeated delivery with the same canonical payload hash returns the original message/run IDs and current run status, with `duplicate: true`. The same identity with different content returns 409 `conflict`. A replay does not allocate a second run or reserve additional budget. If its run was removed, the retained receipt reports `status: "unavailable"`.

Receipts are retained for at least 24 hours and for as long as their run remains unfinished. Older completed receipts may be pruned when admitting a new event. Each bot has a maximum of 1,024 retained receipts and 256 pending user inbox messages; full capacity returns 429 `queue_full`, while duplicates can still resolve. This is a bounded deduplication window, not permanent global exactly-once delivery.

Message, queued run, budget holds, and receipt commit together. Events delivered while paused remain queued. A valid event cannot provide a lease, grant, model, instruction override, or permission expansion. Its content is labeled untrusted and processed only within the bot's existing task, lease, policy, and budget. The native caller must verify any real webhook sender before forwarding the envelope; this endpoint does not perform GitHub signatures or other upstream authentication.

Event messages returned by `GET /v2/bots/{id}/messages` include `event: {event_id, source, untrusted: true}`. V1 `GET /bots/{id}/messages` omits `event` to preserve its existing message shape. Ordinary message fields are unchanged.

Implementation: `echo-adk/echo_adk/bots/triggers.py`, `echo-adk/echo_adk/bots/manager.py`, and the indexed inbox projections in `echo-adk/echo_adk/api/store.py`.

## 6. Budget admission and accounting

Every service-created run reserves its full per-run `max_cost_usd` against the service daily cap and, for a bot run, that bot's cap. Direct, scheduled, inbox, event, and group turns use the same path. Group admission is all-or-nothing. The service cap defaults to 200 USD through `ECHO_ADK_DAILY_CAP_USD`; bot caps remain at most 5 USD. Invalid nonfinite or negative amounts are rejected.

Admission stores the run and all holds in one SQLite transaction. Accounting uses integer microdollars, conservatively rounding upward, with WAL and `synchronous=FULL`. Concurrent admission cannot allocate the same remaining allowance twice. Capacity is rechecked at dispatch, including current cap reductions. Unstarted or paused work retains its hold until it finishes, is cancelled, or is deleted through the supported lifecycle.

An admission failure returns HTTP 429 `budget_exceeded` before a new run or inbox message is created. Its structured fields are `scope` (`service` or `bot`), `daily_cap_usd`, `spent_today_usd`, `reserved_usd`, and `requested_usd`. This corrects the older behavior in which a message could be accepted and refused only when it ran.

V2 bot responses add `budget`. V2 health adds the same shape as `budgets`, with a service-only `recovery` object:

| Field | Meaning |
|---|---|
| `day`, `timezone` | Current accounting date and `UTC`. |
| `daily_cap_usd` | Configured cap for this scope. |
| `spent_today_usd` | Total charged on this UTC settlement day, including conservative uncertain charges. |
| `reserved_usd` | All unfinished holds for this scope, including those admitted on earlier days. |
| `available_usd` | `max(0, cap - spent - reserved)`. |
| `uncertain_charged_today_usd` | Portion of today's charges whose final provider cost was not known. |
| `accounting` | `utc_settlement_day_with_all_unfinished_holds`. |
| `recovery` | Startup counts: `interrupted`, `cancelled`, and `queued_preserved`; service health only. |

On terminal completion, measured spend is charged once and unused reservation is released. Cancellation before work releases the hold without a charge. A failed or interrupted call whose final cost is unknown consumes the larger of its measured-so-far cost and reserved ceiling, marked uncertain. A day boundary never clears an unfinished hold: charges post on the UTC settlement day, and outstanding holds continue to count against the current day.

`GET /v2/runs/{id}` adds this `budget_accounting` receipt:

| Field | Meaning |
|---|---|
| `state` | `held`, `settled`, or `unreserved` for a historical run without this ledger. |
| `admission_day` | UTC date on which the hold was created. |
| `reserved_usd` | Remaining hold, zero after settlement. |
| `ceiling_usd` | Original admitted per-run maximum. |
| `charged_usd` | Amount charged once to the service and applicable bot scope. |
| `settlement_day` | UTC charge date, or `null` while held. |
| `uncertain_cost` | Whether the charge is conservative rather than a known final cost. |
| `overrun_usd` | Charge exceeding the original ceiling, if any. |

An `unreserved` historical receipt contains only `state`. The run's existing `spent` field remains the measured execution record; it must not be substituted for conservative accounting or added to the reservation a second time.

The runner refuses a model call when its declared next-call cost exceeds the remaining run allowance and fails closed on invalid cost metadata. A provider can still report a cost above its declaration after a call; the service records that overrun and blocks further positive-cost admissions as appropriate. These are local admission/accounting controls, not an independent provider invoice or a provider-enforced billing limit. Standalone use of `Runner` outside `AdkService` does not create this service-wide reservation ledger.

Implementation: `echo-adk/echo_adk/api/budgets.py`, `echo-adk/echo_adk/api/store.py`, `echo-adk/echo_adk/api/service.py`, and `echo-adk/echo_adk/core/runner.py`. Concurrency and HTTP regressions are in `echo-adk/tests/test_budget_reservations.py`.

## 7. Leases and local memory

Bot admission snapshots the validated lease template: exact capability scopes, TTL, and maximum uses. Actual dispatch mints a fresh per-turn lease from that immutable snapshot. A long pause does not consume the lease TTL; the original run ID and event receipt remain unchanged. A later bot configuration cannot widen queued authorization, and this path does not add a `grant_ref`. Direct run leases keep their original expiry semantics. Legacy queued bot rows without a snapshot keep their original expiry rather than inventing a new grant.

An explicit direct-run lease may use `scopes: []` to disable all capability calls while permitting model reasoning. This narrows authority even when the agent declares tools; it does not substitute tools when a requested external capability is missing.

Local memory is scoped to `bot:<id>` for bot turns and `agent:<name>` for direct service runs. It is stored under the service state directory, not in Crystal/ECF. Before model invocation, the runner reads up to 20 recent records from the exact scope and selects the newest history that fits a 12,000-character serialized context bound. Individual selected values are capped at 2,500 characters. The store bounds each record to 128 KiB and scans at most the final 4 MiB of each relevant memory file. History is redacted and included as an explicitly untrusted user message, never as a system instruction or permission.

When history is supplied, the first successful model envelope includes `data.memory_context` with `provider: "local"`, `scope`, `records`, and a `sha256` of the serialized supplied history. A successful final response writes a redacted local record, flushes and fsyncs it, then includes this receipt in the **final envelope's `data.memory_receipt`**:

```json
{
  "provider": "local",
  "scope": "bot:22222222222222222222222222222222",
  "record_id": "33333333333333333333333333333333",
  "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

The digest excludes its own `sha256` field. The receipt and recall metadata are covered by the run's envelope proof hash. They are not added to `Proof.receipts`, and they do not assert a Crystal/ECF acknowledgment. A memory read, integrity check, or write failure fails the run visibly; there is no silent fallback to empty memory or an unsaved successful final answer.

Scope filenames now encode `:` as `%3A` to avoid the old `:`/`__` alias. Legacy files remain readable with exact record-scope filtering; the reader does not reinterpret every record in an aliased filename as belonging to one bot.

Implementation: `echo-adk/echo_adk/core/ledger.py` (`MemoryStore`) and `echo-adk/echo_adk/core/runner.py`; coverage is in `echo-adk/tests/test_memory_recall.py`.

## 8. Restart, deletion, migration, and rollback

On startup, interrupted running turns become terminal failures, receive conservative uncertain accounting, and are not automatically replayed. Only bots whose work was interrupted are paused for review. Queued nongroup bot work and event IDs survive; idle bots may continue their already-authorized queue, while paused bots require explicit resume. Unstarted direct runs and unfinished group turns are cancelled rather than replayed. Terminal inbox delivery is repaired from the saved result without another model call; stale completed-group reservations are released.

Stopping an unstarted queued message does not resume its paused bot. Inbox delivery and the corresponding bot state transition commit together. Active bot/group turns prevent deletion with 409 until stopped and finished. Deletion removes that bot's records, inbox, run ledgers/proofs, memory scope, per-bot accounting, and per-run budget receipts; queued holds are settled first. Aggregate service spend and uncertainty remain, so deleting a bot cannot buy back the fleet allowance. Previously shared group transcript records and external provider records are not erased by deleting one bot.

Opening an existing state directory performs additive SQLite migration: the budget-reservation table, microdollar and uncertainty spend columns, and indexed message `role`/`state` projections are created/backfilled. Existing spend is retained. Old run receipts without accounting are not presented as verified new reservations. Inbox counting and dispatch use the pending-state index, so long retained message history cannot hide recent pending work.

Before an upgrade or rollback, stop admission and take a consistent backup of the **entire state directory**, including SQLite/WAL state and memory, ledger, and proof files. Before any downgrade, settle or cancel every new-format held reservation; an old binary does not understand those commitments. Settling holds alone does not make an in-place downgrade safe: older code also does not maintain the new spend and inbox projections. Use a consistent pre-upgrade backup or an explicit reviewed migration/reconciliation, rather than running old and new binaries against the same upgraded database.

Retain legacy memory files during an upgrade; new-format writes do not rewrite them. Do not blindly rename or merge old `__` files because they may contain distinct scopes. An older reader will not discover the new `%3A` files, so rolling back only the executable can make newer memory disappear from view. A rollback must reconcile that storage change as well as outstanding budgets. These procedures are documented requirements; this change does not claim a completed live rollback drill.

Implementation: `echo-adk/echo_adk/api/service.py` (`_recover_incomplete_runs`, `forget_run`), `echo-adk/echo_adk/api/store.py`, `echo-adk/echo_adk/api/budgets.py`, `echo-adk/echo_adk/bots/manager.py`, and `echo-adk/echo_adk/core/ledger.py`.

## 9. Boundaries and remaining delivery gates

This contract provides local schedules, event admission, per-turn leases, budgets, scoped recall, editable notes, browser process/profile isolation, per-bot OAuth connections, authenticated polling subscriptions and portable teams. It does not supply OS/container isolation, hosted webhook authentication, Crystal/ECF search/capture acknowledgments, or MinIO/ESH artifact services. Provider credentials and actual consent are configured separately. Resource APIs are specified in `docs/BOT_WORKSPACES.md`; OAuth callback code/state bypass general SDK logging through a dedicated owner-only protocol endpoint.

The live SDK gate/model path, installed FORGE unit, CertForge verdict, Harbormaster manifest, and rollback drill require separate deployment evidence. Evaluation against another bot product also requires a defined task set and measured results; this API document makes no comparative superiority claim. See the current implementation and open work in `echo-adk/docs/SLICES.md`, deployment instructions in `echo-adk/deploy/README.md`, and the unchanged security rules in `echo-adk/SECURITY.md`.

## 10. Per-bot resource extension

The v2 resource extension is documented in `echo-adk/docs/BOT_WORKSPACES.md` (mirrored at `echo-cli/docs/echo-code/ECHO_BOT_WORKSPACES.md`). It adds browser, OAuth, polling, editable memory and team portability routes. Core v1 shapes remain frozen; v2 deletion additionally reports resource cleanup and provider revocation receipts. Extended schedule shapes are specified in `echo-adk/docs/BOT_SCHEDULES.md`. Imported bots remain paused with empty leases. New provider watches start disabled.
