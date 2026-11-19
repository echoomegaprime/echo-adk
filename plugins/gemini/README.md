# Echo Bots (Echo ADK) for Gemini CLI + Antigravity

## Gemini CLI

A Gemini CLI extension in Google's documented format: `gemini-extension.json` (name, version, `mcpServers`, `contextFileName`) plus the `GEMINI.md` context file carrying the working and safety rules.

Install:

1. Merge this branch so `plugins/` exists in the FORGE checkout of echo-adk.
2. `gemini extensions install` this directory (`plugins/gemini/`), or copy it into the Gemini CLI extensions folder.
3. First use: ask Gemini for the Echo ADK health — expect profile ECHO_ADK with live agent/bot counts.

## Antigravity

Antigravity consumes plain MCP server config rather than Gemini CLI extensions. Copy `antigravity/mcp_config.json` into the Antigravity MCP config (user `~/.config/antigravity/mcp_config.json` or the workspace MCP config) — it registers the same `echo-adk` server with the same bridge route. The rules in `GEMINI.md` apply verbatim; paste them into the Antigravity rules/instructions surface if one is in use.

## How it connects

The `echo-adk` MCP server is the shared bridge (`../shared/echo_adk_bridge.py`) running on FORGE over SSH stdio, because the contract service is loopback-only on FORGE. The host needs SSH access to FORGE (fleet `forge` alias / tailnet 100.113.87.107). On FORGE itself, use `python3` + the bridge path directly.

## Safety rules (also in GEMINI.md)

Bots start paused; **resume only on Bob's explicit word** — the bridge refuses `resume_bot` unless started with `ECHO_ADK_ALLOW_RESUME=1` for an authorized session. Test objects use the lyra-test- prefix; capability ids are exact, never wildcards; daily cost cap at most $5 per bot; Proof verdicts reported exactly as returned; never connect a bot to a social account or external audience without Bob's explicit order.

## Verification status

Structural-verified 2026-11-18 (extension JSON parses and follows the documented Gemini CLI extension shape; bridge protocol-tested live from FORGE). Live install inside Bob's Gemini/Antigravity host is **UNTESTED-live** — those hosts sign in as Bob, on his machines.
