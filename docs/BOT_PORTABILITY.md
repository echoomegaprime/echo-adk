# Portable bot teams and local templates

`echo_adk/bots/portability.py` provides a small bundled template catalog and a
portable JSON format. These are local definitions, not an external marketplace
or an OpenMaus workspace importer. The two actual templates are `research-team`
and `task-decomposition`: each contains two `qwen-local` bots with empty capability
allowlists, no schedule, zero dollar budget and no permission lease scopes.
They need a configured model provider before they can run.

## Format and exclusions

The format is a bounded JSON object with `format: "echo-bots-portable"`,
`version: 1`, `name`, `kind` (`team`, `department` or `template`), `agents` and `bots`. It contains
at most 32 agent definitions, 32 bots and 64 distinct capability identifiers,
within 1 MiB. Each portable bot uses a bundle-local `ref`, for example `bot-1`,
to refer to an agent definition and support explicit renaming.
Department is a descriptive collection kind; it does not create a new permission
boundary, organization hierarchy or automatically running group.

Agent fields are limited to name, role, skills, capabilities, lane, max_steps and
instruction. Bot fields are limited to ref, name, agent, mode, schedule,
instruction, plugins and daily_cost_cap_usd. Explicit note transfer adds
`memory_text`. Unknown fields, duplicate JSON fields, nonfinite numbers and
unsupported format versions are refused. Existing service validators check all
agent definitions, capabilities, skills and schedules before installation.

Exports exclude credentials and connection configuration, browser profiles and
sessions, lease/grant records, bot/run/group/message/event identities, active
state, spend history, source paths, origin stamps and runtime memory histories.
Known credential patterns in descriptive text are redacted. Instructions and
names remain descriptive content and should still be reviewed before sharing.
Personal editable notes are excluded by default. Including notes requires an
explicit export opt-in and a separate explicit import opt-in; revision histories
and original bot scopes are never transferred.

Export and template lookup return `bundle_json` (serialized JSON), its `sha256`,
format/version/name/kind, agent and bot counts, and `memory_included`. The
serialized field keeps the SDK operation schema closed. It must be passed intact
to the preview operation; a filename, URL or arbitrary archive is not accepted.

## Preview, conflicts and application

The Python facade is `PortabilityManager(service, memory=notes)`. It exposes
`list_templates()`, `get_template(template_id)`,
`export_bundle(bot_ids, name=..., kind="team", include_memory=False)`,
`preview_import(bundle_json, ...)` and `import_bundle(bundle_json, ...)`.

Preview and import accept `conflict="reject"` or explicit `reuse_identical`,
`rename_agents` (original name to new name), `rename_bots` (portable ref to new
display name), and `include_memory=False`. Unknown rename references are rejected.
There is no replace-existing option. Reusing an agent requires the same normalized
definition and never changes its existing file. Conflicting bot display names
require an explicit rename. New bots always receive fresh runtime identifiers.

Preview returns `plan_sha256`, create/reuse actions with complete normalized agent
definitions (including capabilities, skills and lane budgets), and each bot's
resulting name, agent, schedule, daily cap, plugins and instruction. Omitted bot
daily caps default to zero. Paused state, empty `lease_scopes`, note-inclusion
flags and warnings make the effective import behavior explicit.
Import requires that exact value as `expected_plan_sha256`, repeats validation
against current state, and refuses a changed plan. It returns `import_id`, the
plan digest, `status: "imported"`, created/reused agent names, new bot IDs mapped
to their portable refs, and warnings.

All imported bots start paused with an explicit empty lease. The owner's
capability allowlist in a definition is not a grant. Import does not resume a bot,
enqueue an event/group/run, connect an application, launch a browser, or invoke a
model. The owner must separately configure and approve the bot's effective
permissions through ordinary controls. Runtime gate, owner, HMAC and child-lease
checks remain applicable.

## Failure, restart and owner recovery

An import journal lives in the main Store database's `portable_imports` table.
Bot creation and the pending-note receipt share one SQLite transaction. Agent
JSON files use the existing service create/delete APIs, and optional notes live
in their separate database. Those boundaries cannot be represented as a single
crash-atomic transaction. Ordinary failures compensate newly created resources
only when their ownership and unchanged state can be verified.

The journal records `prepared` before files are created and `notes_pending` after
the bot transaction. Success records `imported`; successful compensation records
`rolled_back`. An interrupted or unsafe-to-compensate attempt remains pending or
`recovery_required`, and another import is held until review. This does not
silently resume partial work or overwrite unrelated definitions.

`list_imports()` returns up to 100 journal summaries, with pending work first.
`inspect_import(import_id)` returns current resource hashes, rollback eligibility
and `recovery_sha256`. `recover_import(import_id,
expected_recovery_sha256=..., action=...)` requires that unchanged preview:

- `rollback` removes only tracked, unchanged new agent files and unchanged paused,
  drained bots. A subsequent run, owner-edited note, shared reference, attached
  browser/OAuth/subscription state, unavailable resource inventory or unproven
  crash-window file blocks automatic deletion. Preexisting reused agents are
  never removed.
- `preserve` records `recovered_preserved` and releases the import hold while
  retaining partial resources for explicit owner review. It does not claim a
  complete import or execute any preserved bot.

Recovery holds the host's sorted per-bot resource locks before manager/service
locks. New imports acquire their new IDs' resource locks before bot rows become
visible and retain them through note writes or compensation. Browser/OAuth
inventory checks are metadata-only: inspecting recovery must not probe a browser
or call a provider.

Resolve pending imports before downgrading a worker. Older versions do not
understand this journal or new schedule forms; copied state and old notes files
remain on disk unless explicitly managed. The isolated failure, contention,
restart and conservative-recovery regressions are in
`tests/test_bot_portability.py`.
