# Per-bot OAuth connections

The optional `echo_adk.connections` package connects a bot to an account through a real OAuth authorization-code flow with S256 PKCE. Each connection has a fixed owner, bot, provider, operation allowlist, and requested scopes. Provider consent is required before it becomes usable. A host configuration alone does not establish an account connection.

The default installation exposes an unavailable catalog. It does not contain client credentials, log into accounts, select a replacement provider, or claim that SDK capabilities named `echo.connect.*` are registered. The installed SDK source and saved catalog inspected for this implementation did not establish an OAuth broker with those names. This package performs its documented OAuth exchanges directly with configured providers.

## Included provider templates

| Provider | Operation IDs | Account access | Refresh and disconnect |
| --- | --- | --- | --- |
| GitHub | `profile`, `repositories` | `read:user`; repository requests are fixed to public visibility. The connector also requests `offline_access` for expiring tokens. | Refreshes when GitHub issues a refresh token. Disconnect uses the documented single-access-token deletion endpoint and erases local credentials. Other app grants/token pairs may remain; GitHub's Authorized OAuth Apps settings can revoke the whole app. |
| Google Drive | `files`, `file_metadata` | `https://www.googleapis.com/auth/drive.metadata.readonly`; no file contents or write operation. | Requests offline consent, refreshes issued refresh tokens, and calls Google's revocation endpoint on disconnect. Google revocation can invalidate the same app/project grant used by other bots connected to that account. |

These templates use fixed HTTPS authorization, token, API, and revocation URLs. The owner API cannot override any of them, add an HTTP header, supply a token, request an extra scope, or introduce an operation. A host developer can add reviewed `Provider` records in trusted code; this is not a dynamic, model-authored connector mechanism.

GitHub repository inputs are `page` and `per_page`, each bounded to 1–100. Google `files` inputs are `page_size` (1–100), `cursor` (a returned page cursor), and `q` (a bounded Drive search expression). Its response presents Google's `nextPageToken` as `next_cursor`, so the generic secret filter does not mistake a pagination cursor for an account token. `file_metadata` accepts a single bounded `file_id`. The catalog publishes each operation's exact closed input schema.

## Host setup

Install the optional dependency in the ADK environment:

```sh
python -m pip install 'echo-adk[connections]'
```

For a checkout, use `python -m pip install -e '.[connections]'`. Core imports remain standard-library-only when this extra is absent. The checked implementation has been tested with `cryptography` 46.0.7; use the repository's declared compatible version range.

Register an OAuth application with each selected provider. Register the **exact** owner UI callback URL, such as `https://your-prime-host.example/bots-oauth-callback.html`. Do not enable wildcard callbacks. A loopback HTTP callback is supported for a local owner UI; public callbacks require HTTPS. The configured URL cannot contain a query, fragment, or embedded credentials.

Provide client IDs and client secrets through the host's existing secret/configuration facility. Supply a separate, cryptographically random 32-byte vault key, encoded as standard Base64, through that facility. The key must remain available across service restarts and must not be written beside the database, committed, printed into build logs, or put in a bot export. Changing the key without an explicit migration makes existing ciphertext unreadable; the code does not silently replace it or generate a local fallback key.

The provider configuration file contains **references only**:

```json
{
  "providers": {
    "github": {
      "client_id_env": "PRIME_GITHUB_CLIENT_ID",
      "client_secret_env": "PRIME_GITHUB_CLIENT_SECRET"
    },
    "google_drive": {
      "client_id_env": "PRIME_GOOGLE_CLIENT_ID",
      "client_secret_env": "PRIME_GOOGLE_CLIENT_SECRET"
    }
  }
}
```

Configure these host variables:

| Variable | Meaning |
| --- | --- |
| `ECHO_ADK_CONNECTIONS_CONFIG` | Path to the reference-only JSON above, at most 32 KiB. Remove entries for providers the host does not enable. |
| `ECHO_ADK_CONNECTIONS_REDIRECT_URI` | One fixed, provider-registered callback URL for the owner UI. |
| `ECHO_ADK_CONNECTIONS_KEY_ENV` | Name of the environment variable containing the Base64 vault key; defaults to `ECHO_ADK_CONNECTIONS_KEY`. |

The referenced client-secret values are read only inside the host. No API accepts literal secrets or environment-variable names from a model. Invalid configuration, absent secrets, an absent optional dependency, or a missing key leaves connectors unavailable without preventing unrelated ADK functions from starting.

Google may require consent-screen configuration, permitted test users, or verification for the chosen app and scope. GitHub's actual token response determines whether refresh is available. A successful local fixture test does not replace provider app registration or a real owner's consent.

## Owner flow and callable facade

The API integration constructs `from_env(state_dir, require_bot, owner_id="prime-owner")`. `owner_id` is a server-side identity, not a request argument. `require_bot(bot_id)` must raise for an absent or inaccessible bot. The outer Prime owner session or an authenticated SDK host remains responsible for owner authentication; this library is not a login system.

| Method | Result / behavior |
| --- | --- |
| `catalog()` | `{configured, providers}` with operation schemas and unavailable reasons. `configured` means host configuration is available, not that an account has consented. |
| `list(bot_id)` | Public connection records for that bot. |
| `get(bot_id, connection_id)` | One public record; cross-bot access returns not found. |
| `start(bot_id, provider_id, operation_ids)` | `{connection, authorization_url, expires_at}` for owner consent. Scopes are derived from selected operations, and the host callback is fixed. |
| `complete_callback(state, code)` | Resolves the bot and connection from owner-bound server state, consumes it once, exchanges the code, and returns a connected public record. |
| `complete(bot_id, connection_id, state, code, redirect_uri=None)` | Equivalent explicit adapter; every supplied identity/redirect must match the original consent. |
| `invoke(bot_id, connection_id, operation_id, params)` | Performs the real configured HTTP request and returns `{ok, connection_id, operation_id, data, untrusted:true}`. |
| `refresh(bot_id, connection_id)` | Refreshes a supported grant, persists rotated credentials, and returns public metadata. |
| `disconnect(bot_id, connection_id)` | Immediately disables local access/state and reports the actual provider-revocation outcome. A provably unstarted revocation retains encrypted, unusable credentials for explicit cleanup. |
| `delete_bot(bot_id)` | Disables all local connections, attempts remote cleanup within one cooperative 20-second allowance, and removes records only when no unattempted revocation remains. A `deleted:false` receipt includes `pending_connection_ids`; retain the bot identity for an explicit retry. Other bots' local records remain. |
| `close()` | Closes the vault database. |

Public records include `id`, `bot_id`, `provider_id`, `provider_name`, `status`, `operation_ids`, `scopes`, `created_at`, `updated_at`, and `busy`. Expiring access tokens expose only `expires_at`; successful consent reports `refresh_supported`. Errors use fixed codes and credential-free messages. No record returns an access token, refresh token, PKCE verifier, client secret, or encryption key.

If a provider issues parseable credentials with an unacceptable scope receipt or other invalid grant metadata, they are encrypted in a **revocation-only quarantine**. The public record stays `error` or `reauth_required` and reports `cleanup_required:true`; invocation and refresh remain forbidden even if its plaintext status is altered. The owner can explicitly disconnect to request provider cleanup. The service does not automatically revoke an entire Google app grant on this error, because doing so could disrupt another healthy connection. Quarantined records count against capacity and are not pruned as harmless failed consents.

The authorization URL and callback code/state belong only in the owner consent flow. The callback page should capture the provider's response, immediately remove its query string with `history.replaceState`, wait for authenticated owner access, and submit code/state in a dedicated JSON POST. It must not log these fields or place them in model context, analytics, chat history, or general SDK audit records. The callback uses server-bound state, so it needs no browser-selected bot ID or connection ID. An unsuccessful/denied provider login is not a connected account.

Before production consent, configure every edge, reverse proxy, access logger, and error collector on the callback path to omit its query string and JSON body. Browser query removal happens after the initial HTTP request and cannot erase an upstream log entry. Use a callback-page content policy that excludes third-party scripts/analytics. This implementation has not verified a deployment's edge logging configuration or run live account consent; PKCE and expiring one-use state do not justify logging authorization codes.

The native callback route is separate from the general SDK operation catalog. Owner SDK operations may inspect the marketplace, start consent, inspect connections, invoke approved operations, refresh, and disconnect. Authorization codes are not registered as a general SDK capability input. Worker tools never receive OAuth authorization URLs or callback fields.

## Isolation, storage, and concurrency

Consent state is random, expires after at most ten minutes, and is stored only as a SHA-256 lookup hash. The PKCE verifier is encrypted. An atomic SQLite claim binds a callback to the original owner, bot, connection, provider configuration, operation allowlist, and exact redirect URI. Reuse, expiry, identity mismatch, scope expansion, and configuration drift fail before an API invocation.

The separate `connections/vault.sqlite3` stores AES-256-GCM ciphertext. Associated data binds each secret record to its owner, bot, connection, and purpose. The encrypted record also authenticates the approved operations/scopes. Copying ciphertext to another bot or changing the permission metadata does not produce usable credentials. SQLite uses WAL with full synchronization. POSIX directories/files are restricted to 0700/0600; Windows deployments must retain appropriate service-account ACLs on the private state directory. Encryption protects a copied database without its key. It does not create an OS sandbox against code already able to read the service's environment and memory.

Each connection admits one in-flight exchange, refresh, or API request at a time, including callers using separate SQLite handles. A crash leaves a durable claim; it is not silently expired into a replay. Inspect or disconnect the connection and start fresh consent. Refresh failures with uncertain token rotation require reauthorization. An API failure never triggers a hidden refresh-and-retry of the operation. A 401 marks the connection as requiring consent rather than replaying its request.

Disconnect invalidates the local claim before provider revocation. A callback or refresh completing afterward cannot resurrect the connection; newly returned credentials are sent for best-effort cleanup where supported. Remote work already dispatched may finish. Provider-revocation receipts distinguish `confirmed`, `failed`, `unknown`, `unsupported`, `unavailable`, `not_needed`, and `pending`; an uncertain or failed remote response never undoes local disconnection. Use the provider's account settings when remote revocation cannot be confirmed. A repeated disconnect does not replay an attempted mutation.

Bot deletion shares a **20-second cooperative allowance across all revocations**, rather than giving each connection a fresh network timeout. It first disables local access and marks retained ciphertext as revocation-only. Built-in HTTP waits use the smaller of their configured timeout and the remaining allowance. If the allowance expires before a provider request starts, its encrypted credentials remain available only for cleanup; the receipt reports `pending`, `deleted:false`, and the affected connection IDs. The host keeps the bot paused and preserves its identity and memory so the owner can explicitly retry deletion. Retry attempts only the unstarted items and reuses completed or uncertain receipts. A provider timeout after dispatch is `unknown`, not a reason to automatically resend the request. These bounds cannot hard-cancel remote work or an injected custom transport that ignores the cooperative deadline.

There are at most 32 active connections per bot and 4,096 retained records per owner. Expired unconsumed consents and older inactive metadata are bounded. HTTP responses are limited to 512 KiB, network waits to at most 30 seconds and any active ADK deadline, and operation input schemas are closed. Redirects are refused rather than forwarding credentials. Provider failures never echo raw response bodies or URLs. Results are untrusted data with secret fields and exact/commonly encoded request-credential reflections removed.

Connection tokens are not portable team data. Export/import must not include this vault, browser sessions, client secrets, or owner grants. A complete disaster-recovery backup needs a consistent encrypted SQLite snapshot plus separately managed access to its key; ordinary bot exports do not. Local deletion does not erase historical encrypted backups or make claims about remote provider retention.

If an existing vault is unavailable because its host configuration or dependency is missing, bot deletion refuses to claim that its connections were removed. Restore the vault configuration and retry. An unavailable vault also makes portable-import rollback ineligible rather than treating hidden resources as absent.

Connecting an account does not itself grant a worker tool permission. The host still enforces the worker's connection/operation allowlist, run budget, and required lease or SDK admission. Provider output never edits those permissions. Initial templates are read-only and perform no third-party messages, repository edits, or Drive changes.

## Connected-app polling and prechecks

`SubscriptionManager` polls an already-consented, explicitly bound **read-only GET** operation and sends changes to that same bot's existing event inbox. It is not an internet webhook or an inbound provider signature verifier. No polling definition accepts a destination URL, account credential, new scope, task instruction override, or external recipient.

Create a subscription with a connection ID, operation ID, its validated parameters, an interval between 60 and 86,400 seconds, and an optional deterministic precheck. New subscriptions are **disabled**. The owner must enable each one. Manual polling processes only enabled, already-due records; it cannot force a faster interval. A paused bot can receive bounded queued events, while Stop disables its subscriptions. Resuming the bot does not silently reenable them.

A precheck is a closed object such as:

```json
{
  "path": ["files", 0, "mimeType"],
  "operator": "equals",
  "value": "application/pdf"
}
```

`path` contains at most eight literal object keys or nonnegative array indices. Operators are `exists`, `equals`, `contains`, `gt`, and `lt`; values are bounded JSON scalars. There is no expression engine, wildcard query language, script, or model evaluation. A missing path fails the precheck. Booleans do not compare equal to numbers. Without a precheck, any changed observation is eligible.

The first matching observation can produce one event. Subsequent identical observations are suppressed using a canonical JSON SHA-256 cursor. A later changed observation advances a durable sequence; a return to an earlier state can therefore produce a new change event. Each event contains a bounded, redacted preview, source/connection/operation IDs, and the full observation's digest. The original provider result remains untrusted data and never grants permissions.

Before inbox admission, SQLite stores the exact event ID and text. A lost acknowledgment, a full paused inbox, or a budget refusal retains that same pending event. Recovery retries the **same** ID through the existing idempotent bot event endpoint without requerying the provider. Pending retries use 60–1,920-second backoff independently of the provider polling interval, so a daily watch can recover before the receipt horizon. Recovery stops after 86,340 seconds, conservatively within the bot endpoint's 24-hour receipt retention. An expired unacknowledged event is held with the subscription disabled for owner inspection; it is never replaced with a fresh ID automatically.

At most 32 subscriptions per bot and 4,096 per owner are retained. Each pass processes at most 16 due records with a 25-second cooperative I/O budget; the service can choose a smaller batch. The existing inbox and event-receipt caps remain authoritative. Interrupted read-only polling claims can be recovered, while OAuth refresh claims retain their stricter no-replay behavior. An already persisted outbox event survives process restart. Disabling or deleting a subscription invalidates its in-flight admission guard; a late provider response cannot enqueue new work for a stopped bot.

The host constructs:

```python
SubscriptionManager(state_dir, connections, require_bot, deliver_event)
```

`deliver_event(bot_id, envelope, still_current)` is a trusted host callback. It must acquire the bot-manager lock, check `still_current()`, then call normal bot event admission under that lock. The poller never holds its database lock across provider I/O or that callback. This preserves Stop/delete ordering without reversing the bot and subscription lock order. The host owns one scheduler and stops/joins it before `close()`; the package creates no polling thread itself. A cancellation event is checked before provider requests, before event admission, and between records. Already-dispatched remote requests cannot be forcibly undone.

The facade provides `create`, `get`, `list`, `set_enabled`, `delete`, `disable_bot`, `delete_bot`, `has_resources`, `poll_due(bot_id=None, limit=16, cancel=None)`, and `close`. Public records include `enabled`, `next_poll_at`, `cursor`, `pending_delivery`, precheck state, bounded error codes, and actual bot message/run receipts. No subscription exports credentials, browser state, or a provider response as executable authority. The `has_resources` checks include disabled subscriptions and pending OAuth consent so import rollback cannot silently erase resources an owner added afterward.

## Verification and protocol references

The automated suite runs controlled local HTTP OAuth providers with real PKCE verification, authorization-code exchange, access-token use, refresh rotation, and revocation requests. It also traverses the real ADK `BackgroundServer` and `SdkOperations` adapter while completing consent through the dedicated native callback. Negative cases cover cross-bot/owner state, replay/expiry, malformed grants, endpoint redirects, parameter injection, credential reflection, tampered vault metadata/ciphertext, concurrent refresh, restart claims, and disconnect races. These are engineering tests, not a claim of live third-party consent or production release certification.

Primary protocol/provider references:

- [OAuth 2.0 Security Best Current Practice — RFC 9700](https://www.rfc-editor.org/rfc/rfc9700.html)
- [PKCE — RFC 7636](https://www.rfc-editor.org/rfc/rfc7636.html)
- [Token revocation — RFC 7009](https://www.rfc-editor.org/rfc/rfc7009.html)
- [GitHub OAuth authorization, PKCE, and refresh](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)
- [GitHub OAuth token deletion](https://docs.github.com/en/rest/apps/oauth-applications#delete-an-app-token)
- [GitHub authenticated-user API](https://docs.github.com/en/rest/users/users#get-the-authenticated-user)
- [GitHub authenticated-user repositories](https://docs.github.com/en/rest/repos/repos#list-repositories-for-the-authenticated-user)
- [Google OAuth server flow and revocation](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Google's PKCE description](https://developers.google.com/identity/protocols/oauth2/native-app)
- [Google Drive file listing](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list)
- [AES-GCM authenticated encryption](https://cryptography.io/en/latest/hazmat/primitives/aead/)
