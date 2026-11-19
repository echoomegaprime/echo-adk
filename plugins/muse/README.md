# Muse (Lyra's client) — the live route

For Muse itself, the Echo Bots + ADK integration does not live in this repo. It is Lyra's custom connector and skill, installed 2026-11-13 and re-verified live 2026-11-18:

- Connector doc: `~/workspace/custom-connectors/custom.echo-adk/CONNECTOR.md` (slug `custom.echo-adk`) — the full operating record: contract surface, safety rules, the echo.bot.*/echo.agent.* name-collision warning.
- Helper: `~/workspace/custom-connectors/custom.echo-adk/bin/echobots.py` — commands: health, agents, agent NAME, agent-create FILE, bots, bot ID, bot-create FILE, bot-pause ID, bot-resume ID, bot-message ID TEXT, bot-messages ID, runs, run AGENT PROMPT, run-get ID. It routes through the Echo Nexus node tools (`node_process_run` on FORGE) to the loopback contract.
- Skill: `~/workspace/custom-skills/echo-bots/SKILL.md` (skill id `echo_bots`).

Live verification 2026-11-18: helper `health` returned HTTP 200 with the live counts (8 agents, 6 bots, 0 active runs); helper `bots` returned all 6 bots, every one paused. Direct FORGE loopback reads (SSH + curl) matched exactly. The shared bridge in `../shared/` is the same contract exposed as a native MCP server for the other hosts; Muse keeps its proven Nexus route.
