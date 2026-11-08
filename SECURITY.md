# Security

echo-adk binds an agent loop to Echo's existing governance. Five rules are enforced in the core, by
construction, and adapters can only narrow them.

| # | Rule | Where it lives |
|---|---|---|
| R1 | **Agents never hold tokens.** Model paths are a credential-free loopback origin (`LoopbackModel`, `validate_endpoint`) or a vendor CLI whose child environment is allowlisted and drops anything matching `KEY\|TOKEN\|SECRET\|PASSWORD\|CREDENTIAL` (`child_env`). Models config refuses `api_key`-shaped fields. The gate key is read from one environment variable at call time (`ECHO_ADK_GATE_KEY`, name configurable via `ECHO_ADK_GATE_KEY_ENV`), never stored, logged or written. | `echo_adk/core/models.py`, `echo_adk/core/broker.py` |
| R2 | **No silent fallback.** A missing, signed-out, timed-out or unreadable provider raises `ProviderUnavailable(provider)` ("...; no fallback was used") and ends the step. Nothing retries against another model or provider; the ADK adapter never enables model routing or `RetryConfig`. | `echo_adk/core/models.py`, `echo_adk/core/runner.py`, `echo_adk/adk/model.py` |
| R3 | **Scoped broker.** A Lease lists exact capability ids (no `*`), has a TTL and `max_uses`; the local lease check runs before any network call. Tier >= 2 additionally needs a `grant_ref` (ESH signed grant / SOL broker grant). The gate's own tier/HMAC checks remain the backstop. | `echo_adk/core/types.py` (`Lease`), `echo_adk/core/policy.py`, `echo_adk/core/broker.py` |
| R4 | **Proof gates.** Every run emits a `Proof` (`events_sha256` over the envelopes, actions, verdict). Ledger lines are append-only JSONL. | `echo_adk/core/runner.py`, `echo_adk/core/ledger.py` |
| R5 | **Fail-closed.** The capability registry is default-deny: unknown capability, unknown tier, missing schema, missing lease, secret-shaped parameter, out-of-budget call -> `denied`/`gated` envelope, never execution. Every envelope is `untrusted: true`; capability output never drives a later command on its own. | `echo_adk/core/policy.py`, `echo_adk/adk/toolset.py`, `echo_adk/adk/plugin.py` |

Additional posture:

- The API (`echo_adk.api`) binds to loopback only and refuses any other host at both the CLI and the socket.
- Every string that leaves the process (ledger, API, MCP, logs) passes `redact()` (key/token/secret/password assignments, `Bearer` tokens, common token shapes, private key blocks). Secret-named fields are wiped outright.
- Each bot has one Lease per turn and its own memory scope (`bot:<id>`). The optional browser uses a
  separate subprocess and persistent Chromium profile per bot, a host-owned filtering proxy and exact
  origins. This is process/profile isolation under the same OS user, not a container or VM boundary.
  Browser workers receive an allowlisted environment with no SDK or OAuth credentials; caller input
  cannot select executable paths, browser flags, JavaScript, file uploads, downloads or provider hosts.
- OAuth credentials live only in the optional encrypted host vault. Owner/bot/connection bindings,
  one-use state, S256 PKCE, strict redirect matching and durable token-rotation claims constrain each
  grant. The OAuth callback is an owner-only protocol endpoint excluded from the general SDK capability
  gate because that gate may audit application params. Provider consent is never fabricated.
- Provider polling subscriptions may use only already-connected, host-declared read-only operations.
  Deterministic prechecks and durable, bounded event receipts do not change a bot's task or authority.
  Stop disables its subscriptions; unknown delivery outcomes never mint another event identity.
- Bot deletion removes its local browser profile, artifacts, connections, subscriptions, memory and run
  records, while preserving aggregate spend. V2 returns provider revocation receipts separately: local
  disconnection does not claim that an unavailable provider successfully revoked a grant.
- Team imports validate definitions before writes, start bots paused with empty leases, and exclude
  credentials, sessions and running work. Interrupted imports have reviewable resource hashes and
  conservative recovery; changed or newly attached resources are not silently deleted.
- Explicit ECF summaries use an owner/bot-bound durable outbox, host-fixed existing project IDs and
  exact capture/recall leases through the actual gate. Unknown write outcomes retain the original
  request identity; only explicit reconciliation may retry it. A persistence receipt is distinct from
  exact readback and factual verification. Shared canonical ECF is not a private tenant for each bot.
- Cost: per-run budget clamped to the lane and the service caps (120 calls, 5 USD, 4800 s), per-bot `daily_cost_cap_usd <= 5` (turn refused with `budget_exceeded`), service-wide daily cap `ECHO_ADK_DAILY_CAP_USD` (default 200 USD).
- Headless everywhere. Structured JSON logs on stderr; `ECHO_ADK_DEBUG=1` adds debug records (never credentials).

## Reporting

Report vulnerabilities privately to Echo Prime Tech LLC through the repository's GitHub security advisories
(Security tab -> Report a vulnerability). Do not open a public issue for a secret leak or a policy bypass.
Include the envelope(s) or ledger lines that demonstrate the problem; they are already redacted.
