# Echo Bots (Echo ADK) for Codex (GPT)

A Codex plugin in the `.agents/plugins` format used by the Telegram Workspace plugin on QUENCH (`.codex-plugin/plugin.json` + `.mcp.json` + `skills/`).

## What it gives Codex

The `echo-adk` MCP server (19 tools over the frozen ADK contract: health, agents, runs with Proof verdicts, echoBots) and the `echo-bots` skill with the working and safety rules.

## How it connects

Same route as every host package: the shared bridge (`../shared/echo_adk_bridge.py`) runs on FORGE, where the loopback contract service lives, and Codex reaches it over SSH stdio (`ssh forge python3 <bridge path>`). The host running Codex needs SSH access to FORGE (fleet `forge` alias / tailnet 100.113.87.107). If Codex runs on FORGE itself, use `python3` + the bridge path directly.

## Install

1. Merge this branch so `plugins/` exists in the FORGE checkout of echo-adk.
2. Copy `plugins/codex/` into `~/.agents/plugins/plugins/echo-adk/` on the Codex host (QUENCH: `C:\Users\Sovereign\.agents\plugins\plugins\echo-adk`), or point the Codex plugin config at this directory.
3. First use: ask Codex for the Echo ADK health — expect profile ECHO_ADK with live agent/bot counts.

## Safety rules (also in the skill)

Bots start paused; **resume only on Bob's explicit word** — the bridge refuses `resume_bot` unless started with `ECHO_ADK_ALLOW_RESUME=1` for an authorized session (add under `env` in `.mcp.json` only then). Test objects use the lyra-test- prefix; capability ids are exact, never wildcards; daily cost cap at most $5 per bot. Never connect a bot to a social account or external audience without Bob's explicit order.

## Verification status

Structural-verified 2026-11-18 (JSON parses, layout mirrors the live Telegram Workspace plugin, bridge protocol-tested live from FORGE). Live install inside Bob's Codex host is **UNTESTED-live** — Codex plugin enablement and any Echo OAuth prompts on QUENCH are Bob's in-person steps.
