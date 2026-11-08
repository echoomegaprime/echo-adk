# Explicit per-bot ECF summaries

The optional continuity outbox sends an explicit, bounded summary to the existing
Echo Continuity Fabric owner through `GateBroker`. It uses the real
`echo.continuity.capture` and `echo.continuity.recall` contracts verified on
2026-11-08. It neither replaces local bot memory nor captures a transcript in the
background. Nothing is sent on startup, a scheduled tick, or bot completion.

## Host setup and authority

Set `ECHO_ADK_CONTINUITY_CONFIG` to an absolute path to a host-owned UTF-8 JSON
file. The closed configuration has exactly two fields:

```json
{
  "enabled": true,
  "projects": {
    "22222222222222222222222222222222": "existing-project-id"
  }
}
```

The example identifiers must be replaced with the actual local bot ID and an
existing canonical ECF project ID. This file is configuration, not a credential
or a remote permission grant. Keep it outside model-editable workspaces and
include its version in the reviewed deployment configuration. Without the file,
capture is disabled. Invalid files fail closed. Requests cannot choose a project,
owner, endpoint, credential, scope or lease.

The bot's agent definition and its saved owner-approved lease must both include
the exact capability IDs `echo.continuity.capture` and `echo.continuity.recall`.
The template must permit at least two capability uses and a positive TTL. Each
explicit attempt narrows that template to those two IDs, at most two uses, and at
most 45 seconds. It does not add a privileged grant or invoke a model. Existing
GateBroker catalog verification and the remote gate's actual authentication,
scope and policy checks remain required. The reviewed capture capability is
Tier 1 with `continuity.write`; recall is Tier 0 with `continuity.read`.

The native host has one configured owner. Prime must authenticate that owner;
the proposed SDK management operations additionally retain Tier 2 HMAC,
`echobots.manage`, exact owner binding and the existing single-use post-gate
admission proof. A local lease or a successful read does not prove remote write
authorization. Status explicitly reports `remote_authorization_verified: false`.

ECF is a shared canonical ledger. The fixed project and bot attribution provide
logical attribution, not a separate remote ACL or private ECF tenant per bot.
Different bots may be mapped to the same existing project by the host owner.
Do not claim isolation from other operators already admitted to that shared ECF.

## Owner API and SDK contracts

All bodies are closed objects and all routes are v2 only. Native bot IDs and
capture IDs are 32 lowercase hexadecimal characters.

| Native route | Proposed SDK operation | Parameters after `bot_id` |
| --- | --- | --- |
| `GET /v2/bots/{bot_id}/continuity` | `bot.continuity.status` | None. |
| `POST /v2/bots/{bot_id}/continuity/capture` | `bot.continuity.capture` | `request_ref`, `title`, `detail`. |
| `POST /v2/bots/{bot_id}/continuity/reconcile` | `bot.continuity.reconcile` | `capture_id`. |

`request_ref` is a stable caller correlation key matching
`[A-Za-z0-9_.:-]{1,100}`. `title` is nonempty text up to 200 characters; `detail`
is the explicit summary, up to 4,000 characters. Known secret-like patterns,
invalid control characters and invalid Unicode are refused before storage.
This filter does not establish that arbitrary prose is appropriate to share.
The canonical kind is always `note`; the adapter cannot create reminders, tasks,
messages or arbitrary ECF record kinds. The detail receives a fixed source line
identifying the bot. The host supplies the project and durable request ID.

The proposed registered IDs are `echo.echobots.continuity.status`,
`echo.echobots.continuity.capture` and `echo.echobots.continuity.reconcile`.
They are proposals until the real registrar admits them and exact active
readback verifies them. This extension expands the proposed manifest to 56
operations; an older 53-operation activation packet does not cover it.

Status returns `bot_id`, `configured`, `lease_permits_capture`,
`shared_remote_ledger: true`, `remote_authorization_verified: false`,
`captures`, and `untrusted: true`. It makes no network call. `captures` contains
all retained records for this bot, newest first, with a maximum of 128; capacity
is enforced on creation rather than silently dropping old identities.

Every capture result contains `bot_id`, `capture_id`, the original `request_ref`,
`title`, host-bound `project_id`, durable `request_id`, `content_sha256`,
`state`, `reason`, `saved`, `verified`, `receipt`, `attempts`, `routing_ids`,
`shared_remote_ledger: true`, and `untrusted: true`. The full detail is not echoed.
`request_ref` allows a client to find the original receipt after losing the
response; it is a correlation field and carries no authority.

## Acknowledgment and recovery

| State | Meaning |
| --- | --- |
| `prepared` | Durable request exists before owner dispatch. |
| `uncertain` | The owner may have committed the original request; no valid receipt has been confirmed. |
| `saved` | The owner returned a valid persistence receipt; exact readback is still unconfirmed. |
| `verified` | Matching event ID, content hash, project and full text or the owner's bounded UTF-16 excerpt were recalled. |
| `blocked` | This attempt did not establish a write, and no earlier unknown write is being discarded. |
| `conflict` | The owner reported a conflicting request identity; retain the outbox for owner reconciliation. |

The persistence receipt must contain matching `requestId`, `contentSha256`,
kind `note`, bounded `id` and `eventId`, `saved: true`, `verified: true`,
`receiptState: "capture_verified"`, and a boolean `duplicate`. The adapter
validates the actual gate envelope and target body; HTTP 200 or an outer
`status: "ok"` alone is insufficient. A persistence receipt does not certify
the factual truth of the summary or the deployment.

The local request and payload are committed before dispatch. The canonical ECF
owner's verified request-ID contract deduplicates the same ID and payload and
refuses changed payloads. A second capture request with the same `request_ref`
returns its saved local state without issuing another remote write. A changed
summary under the same key returns 409.

Only explicit reconciliation may resubmit an uncertain capture, using the exact
original request ID and payload. Once a valid receipt exists, reconciliation
only repeats readback. It never changes the summary, chooses a new destination
or silently generates a fresh key. A later scope denial cannot erase an earlier
unknown outcome. Cross-bot receipt lookup fails, and a changed host project
binding prevents replay of an old request into a different project.

Each attempt has a cooperative 45-second limit. Built-in transports narrow their
I/O waits to the remaining allowance; custom brokers may not honor that context,
and a timeout cannot cancel work already dispatched to the remote owner. Late
valid persistence receipts are retained as `saved`, without claiming timely
verified readback. The native SDK client permits a 55-second transport wait only
for capture/reconcile; all other operations keep their existing 30-second bound.
Prime's host tool budget may expire sooner and must report an unknown result,
then inspect status using the original key. There is no automatic retry.

## Local state, deletion and release evidence

The outbox is `state_dir/continuity/outbox.sqlite3`, with WAL and FULL synchronous
durability. It is owner/bot-bound, stores at most 128 records per bot and 2,048 in
total, permits at most eight explicit reconciliation attempts, and serializes
network attempts for a bot using durable claims. No database lock is held over
network I/O. Restart preserves claims and receipts without dispatching work.

Include this database, its WAL state, the host binding configuration and existing
credential references in the complete stopped-state backup and rollback review.
An uncertain write must be reconciled against canonical ECF before discarding
or replacing its request identity. Restoring an older local backup cannot undo
a canonical remote write.

Bot deletion and portable-import rollback refuse to discard pending, uncertain,
saved-but-unverified or conflicted captures. Verified and definitively blocked
local history may be deleted with the bot. Deletion does not erase canonical ECF
records and explicitly reports that retention.

Controlled tests exercise actual native HTTP and GateBroker transport with a
local owner fixture, lost acknowledgments, same-ID reconciliation, cross-bot
and cross-project failures, capacity, concurrency and response validation. They
are not live ECF write receipts, SDK registration, production certification or
proof that a particular host key currently has the required remote authority.
