# Editable bot notes

`echo_adk/bots/memory.py` provides `BotMemoryRevisions(state_dir)`. It stores owner
notes separately from the append-only runtime memory implemented by
`echo_adk/core/ledger.py`. The API integration is `echo_adk/api/workspace.py`, and
the runner receives notes through its `memory_notes` callback in
`echo_adk/api/service.py` and `echo_adk/core/runner.py`.

## Owner workflow and scope

Read notes with `GET /v2/bots/{bot_id}/memory`. Edit with `PUT` and exactly
`{"text":"...","expected_revision":0}` for a first edit. Later edits supply the
revision that was read. `GET /v2/bots/{bot_id}/memory/revisions` returns audit
metadata, using bounded `limit` and an exclusive `before` revision cursor.
Restore with `POST /v2/bots/{bot_id}/memory/restore` and
`{"revision":1,"expected_revision":3}`. Restoring revision zero clears notes
through a new audited revision.

The host verifies the owner and bot existence. Editing or restoring requires a
paused, drained bot; edits are not a way to change a running turn's instruction.
The module accepts only an exact 32-character lowercase bot identifier and
constructs `bot:<id>` itself. A caller cannot select another scope, filesystem
path, grant or actor identity through the public routes.

Each read/edit/restore result contains `bot_id`, `scope`, `text`, `revision`, the
content `sha256`, the audit `revision_sha256`, `updated_at`, a `redacted` count and
`untrusted: true`. An empty, never-edited bot has revision zero and no update
timestamp. Notes are untrusted context, not authority or a verified external
memory service. The runner's bounded local recall and proof receipts remain
distinct from Crystal/ECF integration.

## Bounds, redaction and audit

An edit is limited to 16,000 characters and 32,768 UTF-8 bytes, both before and
after redaction. Invalid UTF-8 and control characters other than newline, carriage
return and tab are refused. Existing credential-pattern redaction runs before
storage. It is a best-effort content filter; arbitrary sensitive prose should
still be reviewed before sharing. Raw pre-redaction notes are not journaled.

Each bot has at most 1,000 revisions. A full journal refuses another edit instead
of silently discarding audit history. Actor and reason fields are bounded and
redacted; the public API supplies the owner actor. Restoration rechecks current
redaction rules and appends a new revision with `restored_from` metadata.
Revision metadata contains a content digest and a digest linked to the previous
revision. This is an inspectable local audit chain, not a signed external
attestation or protection against an administrator rewriting the database.

Notes use `bot-memory.sqlite3`, SQLite WAL with FULL durability, owner-only file
permissions and an atomic compare-and-swap transaction. Two independent database
connections attempting the same `expected_revision` cannot both succeed. A
stale edit returns HTTP 409 and retains the previous text. History pagination
does not return every revision's full text.

Bot deletion removes that bot's notes and revision journal, using secure deletion
and a WAL checkpoint. Other bots' notes are retained. Existing backups or copied
state directories are outside that deletion; they must be managed separately.
An older worker does not load this new notes file and does not automatically
remove it on downgrade. Personal notes are excluded from portable exports and
imports unless the owner explicitly opts in on each side.

`tests/test_bot_memory.py` checks independent-connection contention, exact bot
scope, redaction stability through JSON serialization, audit hashes, restore,
restart, bounds and deletion. `tests/test_bot_portability.py` covers optional
note transfer and compensation/recovery across both SQLite stores.
