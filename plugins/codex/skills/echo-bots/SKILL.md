# Echo Bots (Echo ADK)

## When to use

Use this skill whenever Bob asks to make, list, inspect, pause, resume, or message an Echo bot (echoBot), or to create or run an Echo ADK agent. The tools come from the `echo-adk` MCP server (bridge: plugins/shared/echo_adk_bridge.py), which talks to the echo-adk-api contract service on FORGE (loopback http://127.0.0.1:8378, frozen contract in the echo-adk repo at docs/CONTRACT.md).

## How to work with bots and agents

- To see what exists, call `adk_health`, `list_agents`, and `list_bots`. Health reports the live counts of agents and bots in the service.
- To create an agent, call `create_agent` with a definition: name (lowercase kebab), role, skills, capabilities (exact capability ids), lane {node, model, budget}, max_steps, instruction. The service saves it as agents/<name>.json on FORGE.
- To create a bot, call `create_bot` with a definition: name, agent, mode, schedule, instruction, plugins, lease {scopes (exact cap ids), max_uses, ttl_s}, daily_cost_cap_usd at most 5. Bots always start paused.
- To run an agent once, call `start_run` with the agent name and a prompt, then read the run back with `get_run` and report the Proof verdict exactly as returned (PASS, FAIL, or UNKNOWN). Never upgrade a verdict in the telling.
- To talk to a bot, call `message_bot`; read the exchange with `bot_messages`. A paused bot accepts the message and answers when resumed.

## Rules

- Resume a bot only on Bob's explicit word. The bridge enforces this: `resume_bot` is refused unless the MCP server was started with ECHO_ADK_ALLOW_RESUME=1 for an authorized session. A paused bot is the safe default state.
- Never connect a bot or agent to a social account, a public page, or any external audience without Bob's explicit order.
- Name test objects with the lyra-test- prefix so they are unmistakable in the site UI and the service state.
- Leases and capability lists use exact capability ids only, never wildcards. Tier 2 and above capabilities need a grant reference in the run policy.
- Do not confuse these bots with the SDK caps echo.bot.* and echo.agent.* in the caps catalog. Those are a separate FORGE system (a Postgres registry of notification bots and agents in the worker server). Different machinery, same neighborhood of names. When in doubt, say which system you are about to touch.
