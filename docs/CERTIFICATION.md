# Exact-commit certification

The repository opts in to the installed ECHO Certification Forge GitHub App in
`.echo/apps.json`. `.echo/certification.json` selects the committed argv:

```json
["python3", "-B", "scripts/certforge_journey.py"]
```

The App reads both files and the referenced script from the exact GitHub target
commit. Its supported event families include pull requests, pushes, check runs,
releases and deployment status; the deployed App decides which events/actions
qualify. Repository opt-in does not install the App or grant credentials.

## What the offline journey proves

The journey uses only Python's standard library and the acquired ADK source. It
works without installing the package or optional dependencies, performs five
bounded checks, and writes only disposable synthetic state in a temporary directory:

| Check | Observed requirement |
| --- | --- |
| Local capability policy | Missing, empty and exhausted leases are refused; unknown capabilities and exceeded call budgets are denied; proposed Tier 2 management still requires a grant. |
| SDK operation contract | All 53 operations retain closed inputs, Tier 2 classification, fixed `adk.manage` or `echobots.manage` scope and enforced outputs; a caller cannot provide owner authority or bypass the host authorization callback. |
| Durable event and budget behavior | A paused bot queues one event once, rejects changed replays and budget over-admission, retains its receipt and reservation across restart, and cancels unstarted work without charge or implicit resume. |
| Local memory | Scope identities remain separate, metadata cannot be replaced by payloads, redacted receipts survive reopening, and digest tampering is refused. |
| Interval scheduling | Late interval work coalesces, `skip` ignores missed occurrences, and invalid bounds/authority fields are refused. |

All model and SDK execution dependencies fail if called. A process-local Python
audit hook rejects network and subprocess operations, and a 20-second watchdog
exits nonzero on a hung check. Explicit checks also work with Python assertions
disabled. The surrounding CertForge sandbox remains the isolation boundary;
the audit hook is a guard against accidental side effects, not an OS sandbox.

Run it from a source checkout:

```sh
python3 -I -S -B -O scripts/certforge_journey.py
```

The JSON result identifies itself as `mode: offline`, with
`production_e2e: not_performed` and `issuer_verdict: not_issued`.
It is evidence for the issuer to evaluate, not a certificate.

The focused CI tests execute the script without site-packages and deliberately
break authority checking and duplicate receipts to verify that the journey fails.
They also prove its model-execution and network/subprocess guards fail closed.

## Separate release requirements

The full CI suite remains responsible for the complete HTTP contracts, optional
FastAPI/httpx SDK host, timezone/DST behavior, and broader regressions. Live
providers, actual SDK HMAC admission/registration, the deployed Prime path, and
production recovery require their own observed evidence.

The active CertForge policy may additionally require a trusted, signed production
E2E attestation. This journey does not create one and must not relabel its local
checks to satisfy that gate. Submit the actual target/environment identities and
the required runner evidence through the issuer's supported interfaces. Read back
the signed verdict and its verification for the exact candidate SHA and artifact;
a successful local journey or neutral/staging GitHub check is insufficient.

Any commit change invalidates evidence bound to the prior SHA. Preserve the
existing exact-commit dual-certification, deployment identity, authorization and
rollback requirements in `AGENTS.md`, `deploy/README.md`, and the global policy.
