#!/usr/bin/env python3
"""echo_adk_bridge — stdio MCP server for the Echo ADK contract (echoBots).

Speaks MCP JSON-RPC over stdio and forwards tool calls to the echo-adk-api
loopback contract service (frozen contract, repo docs/CONTRACT.md).

Run it where the contract is reachable:
  - On FORGE directly:  python3 echo_adk_bridge.py
  - From another fleet host over SSH (stdio over ssh):
        ssh forge python3 /home/forge/echo-adk/plugins/shared/echo_adk_bridge.py

Config (environment):
  ECHO_ADK_BASE          Contract base URL (default http://127.0.0.1:8378).
  ECHO_ADK_ALLOW_RESUME  Resume gate. Bot resume is refused unless this is
                         exactly "1". Bob's rule: bots resume only on his
                         explicit word — set it only for an authorized
                         session, never as a standing default.

Safety rules baked in (see the connector doc, custom.echo-adk):
  - Bots are created paused by the service; nothing here resumes one
    without the gate above.
  - Capability ids and lease scopes are exact ids, never wildcards.
  - Test objects are named with the lyra-test- prefix.
  - A bot's daily_cost_cap_usd is at most 5 (the service refuses more).

Stdlib only. No credential is read, held, or forwarded by this bridge:
the contract service is loopback-only and reads its own gate key from
its own environment on FORGE at call time.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("ECHO_ADK_BASE", "http://127.0.0.1:8378").rstrip("/")
ALLOW_RESUME = os.environ.get("ECHO_ADK_ALLOW_RESUME", "") == "1"
PROTOCOL = "2024-11-05"


def http(method, path, body=None, params=None):
    url = BASE + path
    if params:
        qs = "&".join(
            f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items() if v is not None
        )
        if qs:
            url += "?" + qs
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:  # connection refused, timeout, DNS
        return 0, json.dumps({"error": "unreachable", "message": str(e)})


import urllib.parse  # noqa: E402  (used by http(); kept near use for readability)


def result_text(status, raw):
    try:
        parsed = json.loads(raw)
        text = json.dumps(parsed, indent=1)
    except Exception:
        text = raw
    return {
        "content": [{"type": "text", "text": f"HTTP {status}\n{text}"}],
        "isError": not (200 <= status < 300),
    }


def gate_refusal(tool):
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"{tool} refused by the bridge resume gate: bots resume only "
                    "on Bob's explicit word. If Bob has authorized this resume, "
                    "restart the MCP server with ECHO_ADK_ALLOW_RESUME=1 for "
                    "this session and retry. Nothing was sent to the service."
                ),
            }
        ],
        "isError": True,
    }


TOOLS = [
    {
        "name": "adk_health",
        "description": "Echo ADK service health: profile, version, live counts of agents, bots and active runs.",
        "inputSchema": {"type": "object", "properties": {}},
        "call": lambda a: http("GET", "/health"),
    },
    {
        "name": "list_agents",
        "description": "List all Echo ADK agent definitions known to the service.",
        "inputSchema": {"type": "object", "properties": {}},
        "call": lambda a: http("GET", "/agents"),
    },
    {
        "name": "get_agent",
        "description": "Read one agent definition by name.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        "call": lambda a: http("GET", "/agents/" + a["name"]),
    },
    {
        "name": "create_agent",
        "description": (
            "Create an agent definition (saved by the service as agents/<name>.json). "
            "Definition: {name (lowercase kebab), role, skills[], capabilities[] (exact "
            "capability ids, never wildcards), lane {node, model, budget {max_calls, "
            "max_cost_usd, max_wall_s}}, max_steps, instruction}. Name test agents "
            "with the lyra-test- prefix."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"definition": {"type": "object"}},
            "required": ["definition"],
        },
        "call": lambda a: http("POST", "/agents", a["definition"]),
    },
    {
        "name": "delete_agent",
        "description": "Delete an agent definition by name. Refused by the service while a bot references it.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        "call": lambda a: http("DELETE", "/agents/" + a["name"]),
    },
    {
        "name": "list_bots",
        "description": "List all echoBots with status, lease, memory scope and spend.",
        "inputSchema": {"type": "object", "properties": {}},
        "call": lambda a: http("GET", "/bots"),
    },
    {
        "name": "get_bot",
        "description": "Read one echoBot by its 32-hex bot_id.",
        "inputSchema": {
            "type": "object",
            "properties": {"bot_id": {"type": "string"}},
            "required": ["bot_id"],
        },
        "call": lambda a: http("GET", "/bots/" + a["bot_id"]),
    },
    {
        "name": "create_bot",
        "description": (
            "Create an echoBot. Bots always start PAUSED. Definition: {name, agent, "
            "mode? (personal|team), schedule? {every_s >= 60}, instruction?, "
            "plugins? (skill names), lease? {scopes (exact cap ids), max_uses, ttl_s}, "
            "daily_cost_cap_usd? (at most 5)}. Name test bots with the lyra-test- prefix. "
            "Never connect a bot to a social account or external audience without "
            "Bob's explicit order."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"definition": {"type": "object"}},
            "required": ["definition"],
        },
        "call": lambda a: http("POST", "/bots", a["definition"]),
    },
    {
        "name": "delete_bot",
        "description": "Delete an echoBot by bot_id.",
        "inputSchema": {
            "type": "object",
            "properties": {"bot_id": {"type": "string"}},
            "required": ["bot_id"],
        },
        "call": lambda a: http("DELETE", "/bots/" + a["bot_id"]),
    },
    {
        "name": "pause_bot",
        "description": "Pause an echoBot by bot_id.",
        "inputSchema": {
            "type": "object",
            "properties": {"bot_id": {"type": "string"}},
            "required": ["bot_id"],
        },
        "call": lambda a: http("POST", "/bots/" + a["bot_id"] + "/pause", {}),
    },
    {
        "name": "resume_bot",
        "description": (
            "Resume an echoBot by bot_id. GATED: Bob's rule is that bots resume only "
            "on his explicit word. This bridge refuses the call unless the server "
            "was started with ECHO_ADK_ALLOW_RESUME=1 for an authorized session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"bot_id": {"type": "string"}},
            "required": ["bot_id"],
        },
        "call": lambda a: http("POST", "/bots/" + a["bot_id"] + "/resume", {}),
        "gated": True,
    },
    {
        "name": "message_bot",
        "description": "Queue one message turn to an echoBot. A paused bot accepts the message and answers when resumed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bot_id": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["bot_id", "text"],
        },
        "call": lambda a: http(
            "POST", "/bots/" + a["bot_id"] + "/message", {"text": a["text"]}
        ),
    },
    {
        "name": "bot_messages",
        "description": "Read an echoBot's message history (optionally since a message_id).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bot_id": {"type": "string"},
                "since": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["bot_id"],
        },
        "call": lambda a: http(
            "GET",
            "/bots/" + a["bot_id"] + "/messages",
            params={"since": a.get("since"), "limit": a.get("limit")},
        ),
    },
    {
        "name": "list_runs",
        "description": "List agent runs (newest first), optionally filtered by agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
        "call": lambda a: http(
            "GET", "/runs", params={"agent": a.get("agent"), "limit": a.get("limit")}
        ),
    },
    {
        "name": "start_run",
        "description": (
            "Run an agent once with a prompt. Returns 202 {run_id}; read the run "
            "back with get_run and report the Proof verdict exactly as returned "
            "(PASS, FAIL or UNKNOWN). Optional budget {max_calls, max_cost_usd, "
            "max_wall_s} and lease {scopes (exact cap ids), ttl_s, max_uses} are "
            "clamped server-side."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string"},
                "prompt": {"type": "string"},
                "budget": {"type": "object"},
                "lease": {"type": "object"},
            },
            "required": ["agent", "prompt"],
        },
        "call": lambda a: http(
            "POST",
            "/runs",
            {
                "agent": a["agent"],
                "prompt": a["prompt"],
                **({"budget": a["budget"]} if a.get("budget") else {}),
                **({"lease": a["lease"]} if a.get("lease") else {}),
            },
        ),
    },
    {
        "name": "get_run",
        "description": "Read one run by run_id, including steps and its Proof when finished.",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
        "call": lambda a: http("GET", "/runs/" + a["run_id"]),
    },
    {
        "name": "stop_run",
        "description": "Stop a run after its current step.",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
        "call": lambda a: http("POST", "/runs/" + a["run_id"] + "/stop", {}),
    },
    {
        "name": "create_group",
        "description": "Create an echoBot group chat (2 to 6 bots; groups exchange messages only, never state). Definition per contract docs/CONTRACT.md.",
        "inputSchema": {
            "type": "object",
            "properties": {"definition": {"type": "object"}},
            "required": ["definition"],
        },
        "call": lambda a: http("POST", "/groups", a["definition"]),
    },
    {
        "name": "get_group",
        "description": "Read one bot group by group_id.",
        "inputSchema": {
            "type": "object",
            "properties": {"group_id": {"type": "string"}},
            "required": ["group_id"],
        },
        "call": lambda a: http("GET", "/groups/" + a["group_id"]),
    },
]

TOOL_MAP = {t["name"]: t for t in TOOLS}


def tool_list():
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "inputSchema": t["inputSchema"],
        }
        for t in TOOLS
    ]


def handle(msg):
    method = msg.get("method", "")
    mid = msg.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "echo-adk-bridge", "version": "1.0.0"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tool_list()}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        tool = TOOL_MAP.get(name)
        if tool is None:
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "content": [{"type": "text", "text": f"unknown tool: {name}"}],
                    "isError": True,
                },
            }
        if tool.get("gated") and not ALLOW_RESUME:
            return {"jsonrpc": "2.0", "id": mid, "result": gate_refusal(name)}
        try:
            status, raw = tool["call"](args)
            return {"jsonrpc": "2.0", "id": mid, "result": result_text(status, raw)}
        except KeyError as e:
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "content": [
                        {"type": "text", "text": f"missing argument: {e}"}
                    ],
                    "isError": True,
                },
            }
    if mid is not None:
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    return None  # notification: no response


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
