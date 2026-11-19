# Contributing

## Ground rules

1. **The core stays standard-library only** (Python >= 3.10). New runtime dependencies go behind an extra
   (`adk`, later `a2a`), never into `dependencies`.
2. **No stubs, no TODOs, no placeholders.** A module ships working with tests or it does not ship.
3. **The five rules in `SECURITY.md` are not negotiable.** A change that weakens R1-R5 needs a Commander
   decision recorded in `docs/SLICES.md`, not a code review.
4. **The HTTP contract is frozen** (`docs/CONTRACT.md`). Echo Prime builds against it. Any change is a
   new contract version negotiated with the Prime side; `tests/test_contract.py` must keep passing.
5. **Headless and structured.** No browser, no popups, logs are JSON on stderr, debug behind
   `ECHO_ADK_DEBUG=1`. Never log a credential, a request header or a raw body.

## Workflow

```bash
git clone https://github.com/echoomegaprime/echo-adk
cd echo-adk
python -m pip install -e ".[dev]"
ruff check . && ruff format --check .
python -m pytest -q
```

- Branch from `main`: `agent/<topic>-<yyyymmdd>` for fleet builders, `feat/`, `fix/` for humans.
- Keep tests stdlib-only. Fakes live in `tests/fakes.py` (model, broker, gate server, loopback model server)
  and `tests/fake_google_adk.py` (the ADK surface the adapter touches).
- Google ADK facts come from the Knowledge Forge (`echo.knowledge.search`, docs `google-adk-*-20261104`)
  or the installed wheel, never from memory. Record the version you verified against.
- Capability tiers in `BUILTIN_CAPABILITIES` are **data verified live** with `echo.caps.detail`. Add a
  capability only with its verified tier/scope/params and the verification date.
- Every PR: tests green on 3.10 and 3.12, ruff clean, `CHANGELOG.md` entry, `docs/SLICES.md` status
  updated when a slice moves.

## Commit trailer

Fleet commits end with the attribution lines the session provides (`Co-Authored-By` / `Claude-Session`).
