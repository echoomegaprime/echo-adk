from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from echo_adk.api.server import BackgroundServer
from echo_adk.api.service import AdkService
from echo_adk.sdk.operations import OPERATIONS, InvalidParams, SdkOperations, operation_catalog, validate
from tests.fakes import FakeBroker, ScriptedModel, final_action


@pytest.fixture
def adapter(agents_copy, tmp_path, monkeypatch):
    service = AdkService(
        agents_copy,
        tmp_path / "sdk-state",
        model_factory=lambda _: ScriptedModel([final_action("fixture result")]),
        broker=FakeBroker(),
        start_scheduler=False,
    )
    monkeypatch.setattr(service, "submit_run", lambda _: None)
    monkeypatch.setattr(service, "submit_group", lambda _: None)
    admitted = []

    def authorize(operation, context):
        admitted.append(operation.name)
        return context is trusted

    trusted = object()  # stand-in for a host-authenticated, scope-checked admission
    with BackgroundServer(service) as origin:
        yield SdkOperations(origin, authorize=authorize), trusted, service, admitted
    service.close()


def definition(name="sdk-worker"):
    return {"name": name, "role": "researcher", "capabilities": [], "lane": {"node": "forge", "model": "qwen-local"}}


def call(adapter, operation, params):
    client, trusted, _, _ = adapter
    result = client.invoke(operation, params, context=trusted)
    assert result.body["ok"] is True, result
    assert result.body["untrusted"] is True
    return result.body["data"]


def test_every_operation_projects_to_the_real_v2_surface(adapter):
    seen = set()

    def invoke(operation, params):
        seen.add(operation)
        return call(adapter, operation, params)

    assert invoke("health", {})["contract_version"] == 2
    assert "agents" in invoke("agent.list", {})
    created = invoke("agent.create", definition())
    assert created["capabilities"] == []
    assert invoke("agent.get", {"name": created["name"]})["name"] == created["name"]
    assert invoke("agent.replace", {**definition(), "instruction": "Stay within the supplied task."})["instruction"]
    run = invoke("run.create", {"agent": created["name"], "prompt": "A bounded reasoning task"})
    assert invoke("run.get", {"run_id": run["run_id"]})["status"] == "queued"
    assert invoke("run.list", {"agent": created["name"], "limit": 1})["runs"][0]["run_id"] == run["run_id"]
    assert invoke("run.stop", {"run_id": run["run_id"]})["status"] == "done"

    bot = invoke("bot.create", {"name": "SDK worker", "agent": created["name"], "lease": {"scopes": []}})
    bot_id = bot["bot_id"]
    assert invoke("bot.list", {})["bots"][0]["bot_id"] == bot_id
    assert invoke("bot.get", {"bot_id": bot_id})["status"] == "paused"
    changed = invoke("bot.update", {"bot_id": bot_id, "schedule": {"at": "09:00", "tz": "UTC", "weekdays": [1, 5]}})
    assert changed["schedule"]["missed"] == "skip"
    assert invoke("bot.resume", {"bot_id": bot_id})["status"] == "idle"
    assert invoke("bot.pause", {"bot_id": bot_id})["status"] == "paused"
    message = invoke("bot.message", {"bot_id": bot_id, "text": "First task"})
    event = invoke("bot.event", {"bot_id": bot_id, "event_id": "sdk-1", "source": "owner", "text": "Untrusted event task"})
    messages = invoke("bot.messages", {"bot_id": bot_id, "since": message["message_id"], "limit": 10})
    assert messages["messages"][0]["event"] == {"event_id": "sdk-1", "source": "owner", "untrusted": True}
    stopped = invoke("bot.stop", {"bot_id": bot_id})
    assert stopped["stop_requested"] is True
    assert set(stopped["run_ids"]) == {message["run_id"], event["run_id"]}
    assert invoke("bot.delete", {"bot_id": bot_id})["deleted"] is True
    assert invoke("agent.delete", {"name": created["name"]})["deleted"] is True

    invoke("agent.create", definition("sdk-group-worker"))
    members = [invoke("bot.create", {"name": name, "agent": "sdk-group-worker"})["bot_id"] for name in ("One", "Two")]
    group = invoke("group.create", {"name": "Review", "bots": members, "prompt": "Compare the supplied evidence"})
    assert len(group["run_ids"]) == 2
    assert invoke("group.get", {"group_id": group["group_id"]})["status"] == "queued"
    assert seen == set(OPERATIONS)


def test_authorization_is_out_of_band_and_precedes_any_operation(adapter):
    client, _, service, _ = adapter
    before = set(service.agents)
    for context in (None, True, {"owner": True, "scopes": ["*"]}):
        result = client.invoke("agent.create", definition(), context=context)
        assert result.status_code == 403 and result.body["outcome"] == "not_started"
    assert set(service.agents) == before
    assert client.invoke("unknown", {}, context=None).status_code == 404


@pytest.mark.parametrize(
    ("operation", "params"),
    [
        ("agent.create", {**definition(), "owner": True}),
        ("agent.create", {**definition(), "source": "/tmp/overwrite"}),
        ("agent.create", {**definition(), "origin": {"by": "owner", "grant_ref": "owner"}}),
        ("run.create", {"agent": "sdk-worker", "prompt": "task", "lease": {"scopes": ["echo.*"]}}),
        ("run.create", {"agent": "sdk-worker", "prompt": "task", "lease": {"scopes": [], "grant_ref": "owner"}}),
        ("bot.create", {"name": "Unsafe", "agent": "sdk-worker", "lease": {"token": "fake"}}),
        ("bot.create", {"name": "Unsafe", "agent": "sdk-worker", "daily_cost_cap_usd": float("nan")}),
        ("bot.create", {"name": "Unsafe", "agent": "sdk-worker", "daily_cost_cap_usd": 10**400}),
        ("bot.create", {"name": "Unsafe", "agent": "sdk-worker", "schedule": {"every_s": True}}),
        ("bot.create", {"name": "Unsafe", "agent": "sdk-worker", "schedule": {"at": "09:00", "tz": "UTC", "weekdays": [1, 1]}}),
        ("bot.update", {"bot_id": "a" * 32}),
        ("bot.resume", {"bot_id": "a" * 32, "command": "resume"}),
        ("bot.event", {"bot_id": "a" * 32, "event_id": "one", "source": "owner", "text": "task", "permission": "owner"}),
        ("bot.get", {"bot_id": "../agents"}),
        ("agent.get", {"name": "sdk-worker?owner=true"}),
        ("health", {"url": "https://unrelated.example"}),
        ("health", {"options": {}}),
        ("group.create", {"name": "Dup", "bots": ["a" * 32, "a" * 32], "prompt": "task"}),
    ],
)
def test_strict_flat_params_fail_before_worker_io(adapter, operation, params):
    client, trusted, service, _ = adapter
    before = (len(service.agents), len(service.store.list_bots()), len(service.store.list_runs()))
    result = client.invoke(operation, params, context=trusted)
    assert result.status_code == 400 and result.body["error"] == "invalid"
    assert result.body["outcome"] == "not_started"
    assert before == (len(service.agents), len(service.store.list_bots()), len(service.store.list_runs()))


def test_actual_budget_and_lifecycle_errors_remain_typed_and_no_work_bypasses_service(adapter):
    client, trusted, service, _ = adapter
    call(adapter, "agent.create", {**definition(), "lane": {"node": "forge", "model": "qwen-local", "budget": {"max_cost_usd": 1}}})
    bot = call(adapter, "bot.create", {"name": "Limited allowance", "agent": "sdk-worker", "daily_cost_cap_usd": 1})
    call(adapter, "bot.event", {"bot_id": bot["bot_id"], "event_id": "held", "source": "owner", "text": "First held task"})
    refused = client.invoke(
        "bot.event", {"bot_id": bot["bot_id"], "event_id": "budget-1", "source": "owner", "text": "task"}, context=trusted
    )
    assert refused.status_code == 429 and refused.body["error"] == "budget_exceeded"
    assert refused.body["details"]["scope"] == "bot" and len(service.store.list_runs()) == 1
    call(adapter, "bot.resume", {"bot_id": bot["bot_id"]})
    conflict = client.invoke("bot.update", {"bot_id": bot["bot_id"], "name": "Too soon"}, context=trusted)
    assert conflict.status_code == 409 and conflict.body["error"] == "conflict"
    missing = client.invoke("bot.get", {"bot_id": "f" * 32}, context=trusted)
    assert missing.status_code == 404 and missing.body["error"] == "not_found"


def test_explicit_empty_run_lease_preserves_reasoning_and_revokes_existing_tool_authority(adapter):
    call(adapter, "agent.create", definition())
    assert adapter[2].get_agent("knowledge-scout").capabilities
    for agent in ("sdk-worker", "knowledge-scout"):
        admitted = call(
            adapter,
            "run.create",
            {"agent": agent, "prompt": "Reason only from this prompt", "budget": {"max_wall_s": 1}, "lease": {"scopes": []}},
        )
        run = call(adapter, "run.get", {"run_id": admitted["run_id"]})
        assert run["budget"]["max_wall_s"] == 1
        assert adapter[2].store.get_run(admitted["run_id"])["lease"]["scopes"] == []


def test_event_idempotency_is_the_workers_durable_receipt(adapter):
    client, trusted, service, _ = adapter
    call(adapter, "agent.create", definition())
    bot = call(adapter, "bot.create", {"name": "Events", "agent": "sdk-worker"})
    params = {"bot_id": bot["bot_id"], "event_id": "one", "source": "owner", "text": "A task api_key=sk-abcdefghijklmnopqrstuvwxyz"}
    first = call(adapter, "bot.event", params)
    duplicate = call(adapter, "bot.event", params)
    assert first["run_id"] == duplicate["run_id"] and duplicate["duplicate"] is True
    service.stop_run(first["run_id"])
    assert call(adapter, "bot.event", params)["status"] == "done"
    assert len(service.store.list_runs()) == 1
    assert "sk-abcdefghijk" not in json.dumps(call(adapter, "bot.messages", {"bot_id": bot["bot_id"]}))
    changed = client.invoke("bot.event", {**params, "text": "Changed task"}, context=trusted)
    assert changed.status_code == 409 and changed.body["error"] == "conflict"


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:8378",
        "http://example.com",
        "http://127.0.0.1:8378/v2",
        "http://user:secret@127.0.0.1",
        "http://127.0.0.1:8378?token=x",
    ],
)
def test_no_remote_destination_credentials_or_path_override(origin):
    with pytest.raises(ValueError):
        SdkOperations(origin, authorize=lambda *_: True)


def test_catalog_is_complete_closed_and_detached_from_internal_schemas():
    catalog = operation_catalog()
    assert len(catalog) == 23
    assert all(row["input_schema_json"]["additionalProperties"] is False for row in catalog)
    assert all(
        "command" not in row["input_schema_json"]["properties"] and "options" not in row["input_schema_json"]["properties"]
        for row in catalog
    )
    catalog[0]["input_schema_json"]["properties"]["owner"] = {"type": "boolean"}
    with pytest.raises(InvalidParams):
        validate({"owner": True}, OPERATIONS["health"].schema)


@pytest.mark.parametrize(
    "mode", ["negative", "nested_negative", "wrong_receipt", "invalid_status", "redirect", "invalid_json", "oversized"]
)
def test_false_success_and_unsafe_transport_are_refused_without_retry(mode):
    received = []

    class Worker(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            status = 302 if mode == "redirect" else 200
            payload = (
                {"ok": False, "error": "unavailable", "message": "no operation"}
                if mode == "negative"
                else {"status": "ok", "result": {"status_code": 200, "body": {"ok": False}}}
                if mode == "nested_negative"
                else {"bot_id": "a" * 32, "status": []}
                if mode == "invalid_status"
                else {"name": "different-agent"}
            )
            raw = (
                b"not json" if mode == "invalid_json" else b"x" * (1024 * 1024 + 1) if mode == "oversized" else json.dumps(payload).encode()
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if mode == "redirect":
                self.send_header("Location", "/unexpected-followup")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Worker)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = SdkOperations(f"http://127.0.0.1:{server.server_port}", authorize=lambda *_: True)
        operation, params = (
            ("bot.create", {"name": "Bot", "agent": "sdk-worker"}) if mode == "invalid_status" else ("agent.create", definition())
        )
        result = client.invoke(operation, params, context=object())
        assert result.status_code == 502 and result.body["ok"] is False
        assert received == ["/v2/bots" if mode == "invalid_status" else "/v2/agents"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
