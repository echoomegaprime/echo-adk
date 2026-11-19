# Echo Bots (Echo ADK) for ChatGPT

Two artifacts, honestly labeled:

1. **`plugin.json`** — a ChatGPT app manifest in the agent-plugins schema, mirroring the Lyra ChatGPT plugin (`~/workspace/lyra-plugin/chatgpt/plugin.json`). It declares the app's identity, prompts, and capabilities. **Manifest only**: a ChatGPT app needs a publicly reachable MCP endpoint, and the ADK contract service is loopback-only on FORGE by design. There is no honest live connection until an Echo-gateway MCP endpoint fronts the ADK contract — that exposure decision is Bob's.
2. **`CUSTOM-GPT-INSTRUCTIONS.md`** — a ready-to-paste instruction block for a Custom GPT. It carries the full safety rules and an explicit instruction to say plainly when no live ADK connection exists in the surface, instead of inventing state. This works today, with no endpoint.

## Safety rules (in both artifacts)

Bots start paused and resume only on Bob's explicit word; test objects use the lyra-test- prefix; capability ids are exact, never wildcards; daily cost cap at most $5 per bot; Proof verdicts reported exactly as returned; never connect a bot to a social account or external audience without Bob's explicit order; never confuse echoBots with the SDK caps echo.bot.* / echo.agent.*.

## Verification status

Structural-verified 2026-11-18 (manifest parses against the same schema shape as the live Lyra ChatGPT plugin). Live behavior in ChatGPT is **UNTESTED-live** and, for the app, blocked on the gateway-endpoint decision above.
