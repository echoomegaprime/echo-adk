# Echo Bots + Echo ADK — host plugin matrix

One system, every host. The system is **echo-adk**: Bob's Echo Agent Development Kit and its always-on governed agents (**echoBots**), served by the `echo-adk-api` contract service on FORGE (loopback `http://127.0.0.1:8378`, systemd unit `echo-adk-api`, frozen contract in `docs/CONTRACT.md` of this repo).

Every package below exposes the same contract operations — health, list/get/create/delete agents, one-shot runs with a Proof verdict, list/get/create/delete/pause/resume/message bots, bot groups — through the same shared MCP bridge:

- **`shared/echo_adk_bridge.py`** — a stdlib-only stdio MCP server that forwards tool calls to the loopback contract. It runs **on FORGE** (where the loopback service lives); hosts on other machines reach it as a stdio server over SSH: `ssh forge python3 /home/forge/echo-adk/plugins/shared/echo_adk_bridge.py`. No credential is read, held, or forwarded by the bridge; the service reads its own gate key from its own environment on FORGE.
- **`shared/SKILL-echo-bots.md`** — the working rules, vendored into the Claude and Codex packages as their skill file and summarized in the Gemini context file.

## Packages

| Host | Package | Status |
|---|---|---|
| Muse (Lyra's client) | `muse/` — the live route is the custom connector + skill outside this repo | **Live-verified 2026-11-18** (see muse/README.md) |
| Claude Code + Claude Desktop | `claude/` — Claude plugin (`.claude-plugin/plugin.json`, `.mcp.json`, skill, `/echo-bots` command) | Structural-verified; live install needs Bob's Claude host |
| Codex (GPT) | `codex/` — Codex plugin (`.codex-plugin/plugin.json`, `.mcp.json`, skill) in the `.agents/plugins` format | Structural-verified; live install needs Bob's Codex host |
| ChatGPT | `chatgpt/` — app manifest (agent-plugins schema) + Custom GPT instruction block | Manifest + instructions only: ChatGPT apps need a publicly reachable MCP endpoint and the contract is loopback-only (see chatgpt/README.md) |
| Gemini CLI | `gemini/` — extension (`gemini-extension.json` + `GEMINI.md` context) | Structural-verified; live install needs Bob's Gemini host |
| Antigravity | `gemini/antigravity/mcp_config.json` — same MCP server config, no extension wrapper | Structural-verified |

## Safety rules (baked into every package)

- Bots start **paused**. Resume only on Bob's explicit word — the bridge itself refuses `resume_bot` unless started with `ECHO_ADK_ALLOW_RESUME=1` for an authorized session.
- Never connect a bot or agent to a social account, public page, or any external audience without Bob's explicit order.
- Test objects are named with the **lyra-test-** prefix.
- Capability ids and lease scopes are **exact ids, never wildcards**. Tier 2+ capabilities need a grant reference in the run policy.
- `daily_cost_cap_usd` is at most **$5 per bot** (service refuses more) under a $200/day service ceiling.
- A run's Proof verdict is reported exactly as returned: PASS, FAIL, or UNKNOWN.
- The SDK caps `echo.bot.*` / `echo.agent.*` are a **different system** (worker-server notification registry). Not these bots.

## Live state at build time (verified 2026-11-18)

Service `active` since 2026-11-08; health ok, profile ECHO_ADK, version 0.1.0, **8 agents, 6 bots, 0 active runs**; all 6 bots paused. Details and the verification record: the connector doc in Lyra's store (`custom.echo-adk`) and `docs/CONTRACT.md`.
