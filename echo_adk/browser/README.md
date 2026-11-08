# Per-bot browser runtime

This optional provider gives each bot a **separate Chromium process and durable
browser profile**. The Python/Playwright driver also runs in a separate worker
process with an allowlisted environment. It does not attach to the owner's
browser or reuse a personal profile.

The boundary is `process_and_profile`, **not an operating-system or container
sandbox**. Workers use the same OS account as the ADK service. A compromised
browser is outside the protection provided by the application proxy and profile
layout. Run the service under an appropriately restricted host account and
network policy. Chromium's sandbox is requested explicitly by default; failure
to launch does not trigger a retry with the sandbox disabled.

The browser feature is the scoped, explicitly requested exception to the older
contributing guidance excluding a browser. It does not change R1–R5, owner
authorization, capability leases, budgets, or the production release gates.

## Install and enable

Core imports remain standard-library only. Install the optional, tested provider
version in the actual ADK runtime environment:

```sh
python -m pip install 'echo-adk[browser]'
python -m playwright install chromium
```

The extra pins `playwright==1.62.0`. An installed compatible Chrome/Chromium may
be selected with the absolute executable setting below. Package installation,
browser installation and a successful inert launch are distinct checks. A
package or executable existing does not by itself produce `available: true`.

The host constructs `BrowserConfig.from_env()` and passes it to
`BrowserManager(state_dir, config)`. Calling `BrowserManager(state_dir)` directly
is disabled and deny-all, regardless of the process environment.

| Host variable | Default | Meaning |
| --- | --- | --- |
| `ECHO_ADK_BROWSER_ENABLED` | `0` | Exact `0` or `1`; opt in to the provider. |
| `ECHO_ADK_BROWSER_ALLOWED_ORIGINS` | `[]` | JSON array of at most 64 exact HTTP/HTTPS origins. No wildcards, credentials, paths, queries or fragments. |
| `ECHO_ADK_BROWSER_PRIVATE_ORIGINS` | `[]` | JSON subset of the allowed origins for which the host explicitly permits private/local addresses. |
| `ECHO_ADK_BROWSER_EXECUTABLE` | unset | Absolute host-owned Chrome/Chromium executable; otherwise use Playwright's installed Chromium. |
| `ECHO_ADK_BROWSER_CHROMIUM_SANDBOX` | `1` | Exact `0` or `1`; actual setting appears in status. Never changed by a browser request or fallback. |
| `PLAYWRIGHT_BROWSERS_PATH` | Playwright host cache | Optional standard Playwright package-cache path; absolute host path or `0`. This does not choose a bot profile. |

For example, a host may permit `https://example.org` while retaining an empty
private-origin list. Local integration tests explicitly allow their own temporary
`http://127.0.0.1:<port>` origin in both lists. Merely listing a hostname in the
ordinary allowlist does not permit it to resolve to loopback, private, link-local,
carrier-grade NAT, multicast, or metadata addresses.

No caller can pass a proxy, permission, executable, browser flag, profile path,
credential, origin exception, file upload, download destination or JavaScript.
The service's normal owner and exact capability/lease checks must precede this
facade. The facade itself does not authenticate a user or mint a grant.

## Facade

```python
from echo_adk.browser import BrowserConfig, BrowserError, BrowserManager

browser = BrowserManager(state_dir, BrowserConfig.from_env())
browser.health()  # cached only; never launches Chromium
browser.probe()  # real inert launch, cached for 60 seconds
browser.status(bot_id)  # cached/bounded probe plus this bot's state
browser.has_resources(bot_id)  # metadata only; no launch, network or mkdir
browser.execute(bot_id, command, cancel=cancel_event)
browser.close(bot_id)  # retain profile and screenshot artifacts
browser.reset(bot_id)  # close, then remove this bot's profile/artifacts
browser.read_artifact(bot_id, artifact_id)  # owner transport only
browser.shutdown()  # close all owned workers
```

Bot IDs, element refs and artifact IDs are 32 lowercase hexadecimal characters.
The service additionally verifies that the bot exists and belongs to the admitted
owner. `reset` works when the provider is disabled or absent; it does not launch
a browser to remove stored state. `has_resources` returns true conservatively on
uncertain filesystem state so an import recovery cannot erase owner-added data.
The caller coordinates it with its workspace mutation lock.

`execute` accepts a closed object with `action` and optional integer
`timeout_ms` from 1 through 30000 (default 30000):

| Action | Additional fields | Result |
| --- | --- | --- |
| `navigate` | `url` | Bounded visible snapshot of the resulting page. |
| `snapshot` | none | Current visible text and fresh opaque control refs. |
| `click` | `ref` | Click the referenced element and return a fresh snapshot. |
| `fill` | `ref`, `value` | Fill an eligible text field and return a fresh snapshot. |
| `screenshot` | none | Screenshot artifact metadata, not image bytes. |

The deadline includes lock waiting, startup and the action; the worker also
observes an enclosing runner I/O deadline. Cancellation or a deadline after
dispatch terminates that bot's worker tree and reports an **unknown outcome**.
An action may already have affected a remote site. The runtime never replays it.
Normal close uses a bounded graceful shutdown to persist the profile, followed
by owned-process cleanup. A forced stop may leave the browser's most recent
unsaved profile changes unflushed.

On Windows an owned Job Object is established before the worker can launch the
driver or browser. Closing it kills the owned descendant tree. On Linux cleanup
freezes verified descendants and checks process start identities before killing
them, including browsers in a different process group. Unsupported platforms
are unavailable rather than pretending equivalent cleanup.

A snapshot has this shape:

```json
{
  "bot_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "action": "snapshot",
  "status": "ready",
  "url": "https://example.org/page",
  "title": "Page title",
  "text": "Visible page text",
  "elements": [{"ref": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "role": "textbox", "name": "Search"}],
  "truncated": false,
  "untrusted": true
}
```

Refs bind to this bot's actual element handles and current snapshot/document.
Every new snapshot invalidates prior refs. Cross-bot, unknown and detached refs
are refused. There is no caller-supplied selector language. Snapshot text is
limited to 12000 characters and 100 eligible controls. Input/textarea/editable
values are excluded; credential and upload controls are omitted. `fill` is
limited to 4096 characters and rejects credential fields and recognizable secret
values. Returned URLs omit query/fragment/userinfo. Core redaction and internal
session-cookie-value redaction apply before text leaves the worker. These rules
do not promise perfect identification of arbitrary sensitive text rendered by
an untrusted website.

There is no cookie, password, local/session-storage or credential export method.
OAuth marketplace grants are separate host-held resources; they are not injected
into the browser. Browser cookies remain in that bot's private durable profile.
Profiles are not encrypted by this ADK module and are not portable exports.

## Egress and resource bounds

Every browser has a private loopback filtering proxy. It checks the exact origin
and all DNS answers, then connects to a validated numeric address without a
second hostname resolution. Mixed public/private DNS answers are refused unless
the exact origin has the host's private exception. HTTP redirects and each new
HTTPS CONNECT target are checked at the proxy. Chromium cannot bypass the proxy
implicitly for loopback. Direct browser DNS resolution/prefetch is disabled;
the numeric proxy endpoint is the sole resolver-rule exception. HTTP/2 and QUIC
are disabled to prevent origin coalescing or transport bypass of CONNECT checks.
TLS certificate validation remains enabled.

Network is enabled only for an active bounded browser command. Existing proxy
connections are closed at its end. An idle open page therefore has no continuing
proxy traffic. Each operation permits at most 64 proxy connections and 16 MiB
of transferred bytes. Proxy headers are capped at 16 KiB; plaintext HTTP request
bodies are capped at 256 KiB. Encrypted HTTPS contents are not inspected; the
combined encrypted transfer cap still applies. There are at most 16 concurrent
proxy handlers per browser. Neither request headers, cookies, bodies nor raw
provider stderr are logged or returned.

The provider blocks service workers, downloads, permissions, popups and normal
page WebSockets. WebSocket routing never connects upstream; a fixed pre-page
script explicitly refuses the page constructor. This avoids a Playwright 1.62
synchronous-close deadlock and does not grant access to caller JavaScript.
Non-proxied WebRTC UDP is disabled. The finite host policy is deliberate; some
sites requiring those features or additional origins will not work. It is still
application-level egress control, not a network namespace or host firewall.

At most four bot sessions may be open. Profiles are checked before/after actions
against 128 MiB and 20000 file entries. This is a **checkpoint limit**, not a hard
filesystem quota or a RAM/CPU container limit. Exceeding it closes the session and
requires owner reset. Host-level disk/process limits remain useful.

## Screenshot artifacts

Screenshots use a fixed 1280×720 viewport with inputs, editable fields and frames
masked. Full-page capture and caller-selected dimensions are unavailable. Each
PNG must be at most **512 KiB**; an oversized image returns 413 and is never
truncated. At most eight PNGs are retained per bot.

`screenshot` returns `{status:"ready",artifact:{artifact_id,mime_type,sha256,
bytes,width,height}}` plus the normal bot/action/untrusted fields.
`read_artifact` verifies ownership-by-directory, regular-file status, size and
SHA-256, and returns that metadata plus `base64`. It never returns a path.
The API integration reserves bytes for the owner UI; worker results contain
metadata only. The encoded response stays below the SDK/HTTP 1 MiB envelope cap.

Screenshots may still contain sensitive visible page content outside the masked
controls. Treat them as private owner artifacts, not ordinary model output or
public logs. Reset removes the stored files and profile from this provider's
directories; it is not forensic disk erasure or remote-provider revocation.

## Status and failures

Health is a nonblocking cached snapshot. Before a real launch it reports
`available:false`, a safe reason, and `verified_at:null`. A successful probe or
action sets `verified_at`. After 60 seconds the cached verification becomes stale
and a status/probe request can refresh it. Health never repeatedly launches
Chromium. The probe uses its own temporary inert profile and closes it.

`BrowserError` exposes `code`, HTTP-shaped `status`, `message` and
`unknown_outcome`. Invalid commands are400; policy/credential refusals403;
missing refs/artifacts404; stale/locked/corrupt state409; resource limits413;
cancellation499; provider failures503; deadlines504. Messages are deliberately
generic and do not reflect raw Playwright errors or secret-bearing URLs.

## Verification

Provider-independent tests exercise origin/address checks, the real local HTTP
proxy, environment filtering, closed command schemas, storage boundaries and
artifact integrity without importing Playwright:

```sh
python -m pytest tests/test_browser_policy.py tests/test_browser_runtime.py tests/test_browser_proxy.py
```

Real Chromium tests are opt-in and use only controlled local HTTP pages:

```sh
ECHO_ADK_BROWSER_TEST_EXECUTABLE=/absolute/path/to/chromium \
  python -m pytest tests/test_browser_chromium.py
```

They exercise separate real profiles/processes, cookie/local-storage persistence,
cross-bot refs/artifacts, snapshot filtering, redirected/private egress refusals,
blocked WebSockets, DNS through the proxy, direct private WebRTC TCP refusal,
cancellation, deadline refusal,
screenshot caps, cached probing, and SDK→actual HTTP→Chromium owner controls.
The SDK authorization context in that test is an explicit local test fixture;
it is not a production credential or public Workspace authentication claim.
A skipped browser suite is not evidence that Chromium works on the host.

Official implementation references:

- [Playwright persistent contexts and launch options](https://playwright.dev/python/docs/api/class-browsertype)
- [Playwright network routing, redirects and service workers](https://playwright.dev/python/docs/network)
- [Playwright WebSocketRoute](https://playwright.dev/python/docs/api/class-websocketroute)
- [Chromium network switches](https://chromium.googlesource.com/chromium/src/+/HEAD/components/network_session_configurator/common/network_switch_list.h)
- [Chromium proxy DNS behavior](https://www.chromium.org/developers/design-documents/network-stack/socks-proxy/)
