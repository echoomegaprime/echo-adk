from __future__ import annotations

import io
import json

from echo_adk.core.ledger import Ledger
from echo_adk.mcp.server import PROTOCOL_VERSION, McpServer, cap_to_tool_name
from tests.fakes import FakeBroker


def rpc(method, params=None, msg_id=1):
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}


def test_initialize_and_tools_list(scout, registry):
    server = McpServer(scout, broker=FakeBroker(), registry=registry)
    init = server.handle(
        rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
    )
    assert init["result"]["protocolVersion"] == PROTOCOL_VERSION and init["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert init["result"]["serverInfo"]["name"] == "echo-adk:knowledge-scout"
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None and server.initialized
    tools = server.handle(rpc("tools/list", msg_id=2))["result"]["tools"]
    assert [t["name"] for t in tools] == [cap_to_tool_name(c) for c in scout.capabilities]
    search = next(t for t in tools if t["name"] == "echo_knowledge_search")
    assert search["inputSchema"]["required"] == ["query"] and search["annotations"]["readOnlyHint"] is True
    assert server.handle(rpc("ping", msg_id=3))["result"] == {}
    assert server.handle(rpc("bogus/method", msg_id=4))["error"]["code"] == -32601


def test_tools_call_goes_through_policy_and_broker(scout, registry, lease_for):
    broker = FakeBroker()
    server = McpServer(scout, broker=broker, registry=registry, lease=lease_for(scout))
    ok = server.handle(rpc("tools/call", {"name": "echo_knowledge_search", "arguments": {"query": "esh"}}))["result"]
    assert ok["isError"] is False and ok["structuredContent"]["untrusted"] is True and ok["structuredContent"]["verdict"] == "allowed"
    assert json.loads(ok["content"][0]["text"])["capability"] == "echo.knowledge.search" and broker.calls == [
        ("echo.knowledge.search", {"query": "esh"})
    ]
    missing = server.handle(rpc("tools/call", {"name": "echo_knowledge_search", "arguments": {}}))["result"]
    assert missing["isError"] is True and missing["structuredContent"]["verdict"] == "denied"
    unknown = server.handle(rpc("tools/call", {"name": "echo_shell_run", "arguments": {"command": "ls"}}))["result"]
    assert unknown["isError"] is True and unknown["structuredContent"]["data"]["code"] == "unknown_capability"
    assert len(broker.calls) == 1
    bad = server.handle(rpc("tools/call", {"arguments": {}}))
    assert bad["error"]["code"] == -32602


def test_lease_scope_is_enforced_for_mcp_calls(scout, registry, lease_for):
    broker = FakeBroker()
    server = McpServer(scout, broker=broker, registry=registry, lease=lease_for(scout, scopes=["echo.caps.search"]))
    gated = server.handle(rpc("tools/call", {"name": "echo_knowledge_get", "arguments": {"doc_id": "x"}}))["result"]
    assert (
        gated["isError"]
        and gated["structuredContent"]["verdict"] == "gated"
        and gated["structuredContent"]["needs"] == "lease scope echo.knowledge.get"
    )
    assert broker.calls == []


def test_stdio_stream_emits_only_json_rpc(scout, registry, tmp_path):
    lines = [
        json.dumps(rpc("initialize", {"protocolVersion": "2024-11-05"})),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "this is not json",
        json.dumps(rpc("tools/list", msg_id=2)),
        json.dumps(rpc("tools/call", {"name": "echo_caps_search", "arguments": {"q": "x"}}, msg_id=3)),
        json.dumps([rpc("ping", msg_id=4), rpc("ping", msg_id=5)]),
    ]
    out = io.StringIO()
    server = McpServer(scout, broker=FakeBroker(), registry=registry, ledger=Ledger(tmp_path))
    server.serve(io.StringIO("\n".join(lines) + "\n"), out)
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert all(line.startswith(("{", "[")) for line in out.getvalue().splitlines())
    assert responses[0]["result"]["protocolVersion"] == "2024-11-05"
    assert responses[1]["error"]["code"] == -32700
    assert len(responses[2]["result"]["tools"]) == 3
    assert responses[3]["result"]["isError"] is False
    assert [r["id"] for r in responses[4]] == [4, 5]
    proof = Ledger(tmp_path).read_proof(server.run_id)
    assert proof is not None and proof.actions[0]["cap"] == "echo.caps.search"
