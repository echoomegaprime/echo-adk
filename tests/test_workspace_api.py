from __future__ import annotations

import json
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

from echo_adk.api.server import BackgroundServer
from echo_adk.api.service import AdkService
from echo_adk.sdk.manifest import PROPOSED_IDS
from echo_adk.sdk.operations import OPERATIONS, SdkOperations
from tests.fakes import FakeBroker, ScriptedModel, final_action


@pytest.fixture
def workspace_api(agents_copy, tmp_path):
    model = ScriptedModel([final_action("bounded answer")])
    service = AdkService(agents_copy, tmp_path / "workspace", model_factory=lambda _: model, broker=FakeBroker(), start_scheduler=False)
    service.create_agent(
        {"name": "notes-worker", "role": "researcher", "capabilities": [], "lane": {"node": "forge", "model": "qwen-local"}}
    )
    bot = service.create_bot({"name": "First", "agent": "notes-worker", "lease": {"scopes": []}})
    other = service.create_bot({"name": "Second", "agent": "notes-worker", "lease": {"scopes": []}})
    admitted = object()
    with BackgroundServer(service) as origin:
        sdk = SdkOperations(origin, authorize=lambda _, context: context is admitted)
        yield service, sdk, admitted, bot["bot_id"], other["bot_id"], model
    service.close()


def invoke(fixture, name, params):
    _, sdk, admitted, *_ = fixture
    result = sdk.invoke("bot." + name, params, context=admitted)
    assert result.body["ok"] is True, result.body
    return result.body["data"]


def test_memory_http_revision_cas_isolation_and_real_runner_recall(workspace_api):
    service, sdk, admitted, bot_id, other_id, model = workspace_api
    initial = invoke(workspace_api, "memory.read", {"bot_id": bot_id})
    assert initial["revision"] == 0 and initial["text"] == ""
    first = invoke(
        workspace_api,
        "memory.edit",
        {"bot_id": bot_id, "text": "Preferred report: evidence first. password=private-test-value", "expected_revision": 0},
    )
    assert first["revision"] == 1 and "private-test-value" not in first["text"]
    stale = sdk.invoke("bot.memory.edit", {"bot_id": bot_id, "text": "stale overwrite", "expected_revision": 0}, context=admitted)
    assert stale.status_code == 409
    assert invoke(workspace_api, "memory.read", {"bot_id": other_id})["text"] == ""
    second = invoke(workspace_api, "memory.edit", {"bot_id": bot_id, "text": "changed", "expected_revision": 1})
    assert second["revision"] == 2
    restored = invoke(workspace_api, "memory.restore", {"bot_id": bot_id, "revision": 1, "expected_revision": 2})
    assert restored["text"] == first["text"] and restored["revision"] == 3
    assert len(invoke(workspace_api, "memory.revisions", {"bot_id": bot_id, "limit": 2})["revisions"]) == 2
    receipt = service.message_bot(bot_id, {"text": "Return a short report."})
    refused = sdk.invoke("bot.memory.edit", {"bot_id": bot_id, "text": "racing queued work", "expected_revision": 3}, context=admitted)
    assert refused.status_code == 409
    service.bots.resume(bot_id)
    service.execute_run(receipt["run_id"])
    assert model.calls == 1
    assert any(
        "Preferred report: evidence first" in message["content"] and "untrusted" in message["content"] for message in model.seen[0][1]
    )
    assert "private-test-value" not in json.dumps(model.seen)


def test_portable_team_export_preview_commit_and_recovery_surface(workspace_api):
    _, sdk, admitted, bot_id, _, _ = workspace_api
    templates = invoke(workspace_api, "team.templates", {})["templates"]
    assert templates
    template = invoke(workspace_api, "team.template", {"template_id": templates[0]["template_id"]})
    assert json.loads(template["bundle_json"])["format"] == "echo-bots-portable"
    exported = invoke(workspace_api, "team.export", {"bot_ids": [bot_id], "name": "Portable research", "kind": "team"})
    bundle = json.loads(exported["bundle_json"])
    assert bot_id not in exported["bundle_json"]
    assert all(word not in exported["bundle_json"] for word in ('"grant_ref"', '"active_run_id"', '"credentials"'))
    options = {
        "bundle_json": exported["bundle_json"],
        "rename_agents_json": json.dumps({"notes-worker": "imported-worker"}),
        "rename_bots_json": json.dumps({bundle["bots"][0]["ref"]: "Imported first"}),
    }
    plan = invoke(workspace_api, "team.preview", options)
    refusal = sdk.invoke("bot.team.import", {**options, "expected_plan_sha256": "0" * 64}, context=admitted)
    assert refusal.status_code == 409
    imported = invoke(workspace_api, "team.import", {**options, "expected_plan_sha256": plan["plan_sha256"]})
    assert imported["status"] == "imported" and imported["bots"][0]["status"] == "paused"
    created = workspace_api[0].bots.get_bot(imported["bots"][0]["bot_id"])
    assert created["lease"]["scopes"] == [] and created["inbox_depth"] == 0
    assert invoke(workspace_api, "team.imports", {})["imports"]
    inspected = invoke(workspace_api, "team.inspect", {"import_id": imported["import_id"]})
    assert inspected["status"] == "imported"
    assert bundle


def test_unconfigured_resources_remain_unavailable_and_oauth_callback_is_not_a_capability(workspace_api):
    _, sdk, admitted, bot_id, _, _ = workspace_api
    status = invoke(workspace_api, "browser.status", {"bot_id": bot_id})
    assert status["available"] is False and status["os_isolation"] is False
    result = sdk.invoke("bot.browser.act", {"bot_id": bot_id, "action": "snapshot"}, context=admitted)
    assert result.status_code == 503 and result.body["ok"] is False
    assert invoke(workspace_api, "browser.close", {"bot_id": bot_id})["closed"] is True
    assert invoke(workspace_api, "browser.reset", {"bot_id": bot_id})["reset"] is True
    assert invoke(workspace_api, "connection.list", {"bot_id": bot_id})["connections"] == []
    assert invoke(workspace_api, "marketplace", {})["configured"] is False
    assert "bot.connection.callback" not in OPERATIONS and "bot.connection.callback" not in PROPOSED_IDS
    assert sdk.invoke("bot.connection.callback", {"state": "owner-state", "code": "one-use-code"}, context=admitted).status_code == 404
    assert sdk.invoke("bot.memory.read", {"bot_id": bot_id}, context=object()).status_code == 403


def test_shutdown_drains_real_http_callback_and_retains_storage_on_deadline(workspace_api, monkeypatch):
    service, sdk, admitted, bot_id, *_ = workspace_api
    entered, release = threading.Event(), threading.Event()

    def delayed_callback(state, code):
        entered.set()
        assert release.wait(5)
        # An already-admitted request can finish its nested resource transaction
        # after shutdown closes admission to new requests.
        with service.workspace.resource_guard([bot_id]):
            return service.workspace.memory.edit(bot_id, "persisted after provider completion", expected_revision=0)

    monkeypatch.setattr(service.workspace.connections, "complete_callback", delayed_callback)

    def request():
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            sdk.origin + "/v2/bots/connections/callback",
            data=json.dumps({"state": "one-use-state", "code": "one-use-code"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with opener.open(req, timeout=5) as response:
            return json.load(response)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(request)
        try:
            assert entered.wait(5)
            with pytest.raises(RuntimeError, match="storage remains open"):
                service.workspace.stop(timeout_s=0.02)
            assert service.workspace.memory.read(bot_id)["revision"] == 0
            refused = sdk.invoke("bot.memory.read", {"bot_id": bot_id}, context=admitted)
            assert refused.status_code == 503
        finally:
            release.set()
        assert future.result(timeout=5)["revision"] == 1
    service.workspace.stop(timeout_s=1)
    assert service.workspace.memory.read(bot_id)["text"] == "persisted after provider completion"


def test_delete_retains_paused_identity_and_memory_until_provider_cleanup_finishes(workspace_api, monkeypatch):
    service, sdk, admitted, bot_id, *_ = workspace_api
    service.workspace.memory.edit(bot_id, "keep until deletion completes", expected_revision=0)
    original = service.workspace.connections.delete_bot
    monkeypatch.setattr(
        service.workspace.connections,
        "delete_bot",
        lambda _: {"bot_id": bot_id, "deleted": False, "connections": [], "pending_connection_ids": ["c" * 32]},
    )
    pending = sdk.invoke("bot.delete", {"bot_id": bot_id}, context=admitted)
    assert pending.status_code == 409 and "cleanup_pending" in json.dumps(pending.body)
    assert service.bots.get_bot(bot_id)["status"] == "paused"
    assert service.workspace.memory.read(bot_id)["text"] == "keep until deletion completes"
    monkeypatch.setattr(service.workspace.connections, "delete_bot", original)
    done = sdk.invoke("bot.delete", {"bot_id": bot_id}, context=admitted)
    assert done.status_code == 200 and done.body["data"]["deleted"] is True
    assert service.workspace.memory.read(bot_id)["revision"] == 0


def test_busy_resource_refuses_before_dispatch_but_browser_close_can_cancel(workspace_api, monkeypatch):
    service, sdk, admitted, bot_id, *_ = workspace_api
    entered, release = threading.Event(), threading.Event()
    calls = []

    def hold():
        with service.workspace.resource_guard([bot_id]):
            entered.set()
            assert release.wait(5)

    monkeypatch.setattr(service.workspace.browser, "execute", lambda *a, **k: calls.append("unexpected dispatch"))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(hold)
        try:
            assert entered.wait(5)
            result = sdk.invoke("bot.browser.act", {"bot_id": bot_id, "action": "snapshot", "timeout_ms": 10}, context=admitted)
            assert result.status_code == 409 and "resource_busy" in json.dumps(result.body)
            assert calls == []
            assert invoke(workspace_api, "browser.close", {"bot_id": bot_id})["closed"] is True
        finally:
            release.set()
        future.result(timeout=5)
    assert calls == []  # No queued action starts after its caller receives refusal.
