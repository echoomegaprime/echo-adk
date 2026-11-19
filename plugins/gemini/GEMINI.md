# Echo Bots (Echo ADK) — Gemini context

This extension exposes the Echo ADK contract (echoBots) as MCP tools: adk_health, list_agents, get_agent, create_agent, delete_agent, list_bots, get_bot, create_bot, delete_bot, pause_bot, resume_bot, message_bot, bot_messages, list_runs, start_run, get_run, stop_run, create_group, get_group. The tools reach the echo-adk-api service on FORGE (loopback http://127.0.0.1:8378) through the shared stdio bridge (plugins/shared/echo_adk_bridge.py), run on FORGE over SSH.

Working rules:

- To see what exists, call adk_health, list_agents, and list_bots first.
- Agent definitions: name (lowercase kebab), role, skills, capabilities (exact capability ids), lane {node, model, budget}, max_steps, instruction.
- Bot definitions: name, agent, mode, schedule, instruction, plugins, lease {scopes (exact cap ids), max_uses, ttl_s}, daily_cost_cap_usd at most 5. Bots always start paused.
- Runs end in a Proof. Report the verdict exactly as returned (PASS, FAIL, or UNKNOWN); never upgrade it in the telling.

Safety rules (never break):

- Resume a bot only on Bob's explicit word. The bridge refuses resume_bot unless the MCP server was started with ECHO_ADK_ALLOW_RESUME=1 for an authorized session.
- Never connect a bot or agent to a social account, public page, or external audience without Bob's explicit order.
- Name test objects with the lyra-test- prefix.
- Exact capability ids only, never wildcards.
- The SDK caps echo.bot.* and echo.agent.* are a different system (worker-server notification registry). Do not use them when Bob means echoBots.
