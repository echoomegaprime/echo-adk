# Echo Bots (Echo ADK) for Claude Code + Claude Desktop

A Claude plugin in the same format as the Lyra plugin (`~/workspace/lyra-plugin/claude-code`): `.claude-plugin/plugin.json` + `.mcp.json` + a skill + a slash command.

## What it gives Claude

The `echo-adk` MCP server (19 tools: adk_health, list_agents, get_agent, create_agent, delete_agent, list_bots, get_bot, create_bot, delete_bot, pause_bot, resume_bot, message_bot, bot_messages, list_runs, start_run, get_run, stop_run, create_group, get_group), the `echo-bots` skill with the working and safety rules, and a read-only `/echo-bots` command that reports health plus the agent and bot lists.

## How it connects

The contract service (`echo-adk-api`) is loopback-only on FORGE by design, so the MCP server is the shared bridge (`../shared/echo_adk_bridge.py`) running **on FORGE**, reached over SSH stdio:

```json
{ "mcpServers": { "echo-adk": {
  "command": "ssh",
  "args": ["forge", "python3", "/home/forge/echo-adk/plugins/shared/echo_adk_bridge.py"]
} } }
```

That requires the host running Claude to have SSH access to FORGE (the fleet `forge` alias / tailnet 100.113.87.107). If Claude runs on FORGE itself, replace the command with `python3` and the bridge path directly. Set `ECHO_ADK_BASE` only if the contract ever moves off `http://127.0.0.1:8378`.

## Install

1. Merge this branch so `plugins/` exists in the FORGE checkout of echo-adk (the bridge path above assumes it).
2. Claude Code: `claude plugin` install from this directory, or copy `plugins/claude/` into the plugins folder and enable it. Claude Desktop: add the `.mcp.json` server entry to the Desktop MCP config (or install the plugin where Desktop plugin support is enabled).
3. First use: ask Claude to run `adk_health` — expect profile ECHO_ADK with live agent/bot counts.

## Safety rules (also in the skill)

Bots start paused; **resume only on Bob's explicit word** — the bridge refuses `resume_bot` unless the server is started with `ECHO_ADK_ALLOW_RESUME=1` for an authorized session (add it under `env` in `.mcp.json` only for such a session). Test objects use the lyra-test- prefix; capability ids are exact, never wildcards; a bot's daily cost cap is at most $5. Never connect a bot to a social account or external audience without Bob's explicit order.

## Verification status

Structural-verified 2026-11-18 (JSON parses, files complete, bridge protocol-tested live against the running service from FORGE). Live install inside Bob's Claude host is **UNTESTED-live** — it needs Bob's Claude Code/Desktop host, where his sign-in lives.
