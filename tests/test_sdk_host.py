"""Real ASGI transport tests for the installable adapter's admission boundary.

The SDK helper fixture models the verified host hook/projection signatures; it
does not claim to implement or verify the production gate's HMAC policy. These
tests exercise FastAPI + httpx ASGITransport + the actual native v2 HTTP service.
Optional host dependencies are required in CI, never silently skipped.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from echo_adk.sdk.host import _FRAME, create_app, install
from echo_adk.sdk.manifest import OUTPUT_SCHEMA, PROPOSED_IDS, ROUTE_PREFIX, proposed_manifest
from echo_adk.sdk.operations import OPERATIONS
from tests.test_sdk_operations import adapter as adapter  # shared actual native HTTP service fixture
from tests.test_sdk_operations import definition


def project(envelope, cap):
    """The verified closed-schema command projection; no options flattening."""
    params = dict(envelope.params)
    schema = cap["input_schema_json"]
    if schema.get("additionalProperties") is False and "command" not in schema["properties"]:
        params.pop("command", None)
    return params


class HostHelpers:
    def __init__(self):
        self.hooks = []
        self.auth_calls = []
        self.hook_calls = 0
        self.late_task = None
        self.release_late = None
        self.grants = []
        # Models the result of the real gate's independent HMAC verifier. This
        # fixture does not implement or claim live cryptographic verification.
        self.hmac_decision = "valid"
        self.capabilities = {cap["id"]: cap for cap in proposed_manifest("http://127.0.0.1:8000")["capabilities"]}

    def register_pre_dispatch_hook(self, hook):
        self.hooks.append(hook)

    @staticmethod
    def key_scopes(key):
        if key in ("weak-key", "task-key", "other-task-key"):
            return ["tier:1"]
        if key == "adk-key":
            return ["adk.manage"]
        if key == "bots-key":
            return ["echobots.manage"]
        if key == "tier2-only-key":
            return ["tier:2"]
        return ["tier:1", "adk.manage", "echobots.manage"]

    async def _authenticate(self, key):
        self.auth_calls.append(key)
        await asyncio.sleep(0)  # authenticate is genuinely asynchronous in the host
        if key == "revoked-key":
            raise HTTPException(401, "revoked")
        if key == "unavailable-key":
            raise RuntimeError("private authentication database details")
        return {
            "owner": "different-owner" if key in ("other-key", "other-task-key") else "owner@example.test",
            "active": key != "inactive-key",
            "scopes": self.key_scopes(key),
        }


def create_host(native):
    app, helpers = FastAPI(), HostHelpers()

    async def target(operation, params, headers):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sdk.test") as client:
            return await client.post(ROUTE_PREFIX + "/" + operation, json=params, headers=headers)

    @app.post("/sdk/invoke")
    async def gate(request: Request):
        data = await request.json()
        # The production gate performs HMAC/TOTP/scope/policy admission before
        # its registered pre-hooks. The fixture exposes a refusal for that phase.
        if data.get("admitted") is False:
            return JSONResponse({"error": "gate_refused"}, status_code=403)
        operation = data["operation"]
        cap = helpers.capabilities[PROPOSED_IDS[operation]]
        # Source-backed ordering: effective static OR server-side task scope,
        # then independent Tier2 HMAC, then registered pre-dispatch hooks.
        # The request's context names a grant; it cannot supply its contents.
        context = data.get("context", {})
        granted = cap["required_scope"] in helpers.key_scopes(request.headers.get("X-Echo-API-Key", ""))
        if not granted and isinstance(context, dict):
            granted = any(
                grant["active"] is True
                and grant["expires"] > time.monotonic()
                and grant["actor"] == context.get("session_actor_id")
                and grant["task"] == context.get("task_id")
                and grant["scope"] == cap["required_scope"]
                for grant in helpers.grants
            )
        if not granted:
            return JSONResponse({"ok": False, "error": "scope_forbidden", "outcome": "not_started"}, status_code=403)
        if cap["danger_tier"] >= 2 and helpers.hmac_decision != "valid":
            return JSONResponse(
                {"error": "hmac_" + helpers.hmac_decision}, status_code=423 if helpers.hmac_decision == "missing" else 401
            )
        envelope = SimpleNamespace(params=data.get("params", {}))
        for hook in helpers.hooks:
            helpers.hook_calls += 1
            await hook(envelope, cap, "fixture-audit-label")
        params = project(envelope, cap)
        headers = [
            ("X-Echo-API-Key", request.headers.get("X-Echo-API-Key", "")),
            ("X-Echo-Capability", cap["id"]),
        ]
        if data.get("nested"):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sdk.test") as client:
                nested = await client.post(
                    "/sdk/invoke",
                    json={"operation": "health", "params": {"command": "read"}},
                    headers={"X-Echo-API-Key": request.headers["X-Echo-API-Key"]},
                )
            assert nested.status_code == 200 and nested.json()["ok"] is True
        if "replace_params" in data:
            params = data["replace_params"]
        if "replace_key" in data:
            headers[0] = ("X-Echo-API-Key", data["replace_key"])
        if "replace_capability" in data:
            headers[1] = ("X-Echo-Capability", data["replace_capability"])
        if data.get("duplicate_key"):
            headers.append(headers[0])
        if data.get("expire") and _FRAME.get():
            _FRAME.get().armed_at -= 31
        if "replace_admitted_scope" in data and _FRAME.get():
            _FRAME.get().admitted_scope = data["replace_admitted_scope"]
        if data.get("later_refusal"):
            if data.get("late_task"):
                helpers.release_late = asyncio.Event()

                async def late_target():
                    await helpers.release_late.wait()
                    return await target(operation, params, headers)

                # Copies the current ContextVar as a real background task would.
                helpers.late_task = asyncio.create_task(late_target())
            return JSONResponse({"error": "later_hook_refused"}, status_code=403)
        first = await target(operation, params, headers)
        if data.get("replay"):
            second = await target(operation, params, headers)
            return {"first": first.json(), "second": second.json(), "statuses": [first.status_code, second.status_code]}
        return JSONResponse(first.json(), status_code=first.status_code)

    install(
        app,
        owner="owner@example.test",
        worker_origin=native.origin,
        sdk=helpers,
        projector=project,
    )
    return app, helpers


def invoke(app, operation, params, *, key="owner-key", **flags):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sdk.test") as client:
            return await client.post(
                "/sdk/invoke", headers={"X-Echo-API-Key": key}, json={"operation": operation, "params": params, **flags}
            )

    return asyncio.run(run())


def test_proposed_manifest_is_complete_tier_two_and_uses_real_flat_host_contract():
    manifest = proposed_manifest("http://127.0.0.1:8000")
    committed = Path(__file__).resolve().parents[1] / "echo_adk" / "sdk" / "capabilities.proposed.json"
    assert json.loads(committed.read_text()) == manifest
    assert manifest["registration_status"] == "proposed_not_registered"
    caps = manifest["capabilities"]
    assert len(caps) == len(OPERATIONS) == 23
    assert len({cap["id"] for cap in caps}) == 23
    assert all(cap["danger_tier"] == 2 for cap in caps)
    assert all(cap["required_scope"] == ("echobots.manage" if cap["id"].startswith("echo.echobots.") else "adk.manage") for cap in caps)
    assert all(cap["handler_kind"] == "router" and cap["target_method"] == "POST" for cap in caps)
    assert all(cap["output_schema_json"]["x-echo-enforce-output-contract"] is True for cap in caps)
    assert all(cap["input_schema_json"]["additionalProperties"] is False for cap in caps)
    assert all("command" not in cap["input_schema_json"]["properties"] for cap in caps)
    assert all("options" not in cap["input_schema_json"]["properties"] for cap in caps)
    caps[0]["output_schema_json"]["required"] = []
    assert OUTPUT_SCHEMA["required"] == ["ok", "operation", "untrusted"]
    with pytest.raises(ValueError):
        proposed_manifest("http://remote.example")


def test_installed_host_accepts_only_actual_post_gate_asgi_admission(adapter):
    app, helpers = create_host(adapter[0])
    response = invoke(app, "agent.create", {"command": "create", **definition()})
    assert response.status_code == 201 and response.json()["ok"] is True
    assert response.json()["data"]["name"] == "sdk-worker"
    assert response.json()["untrusted"] is True
    assert adapter[2].get_agent("sdk-worker").name == "sdk-worker"
    assert helpers.hook_calls == 1 and helpers.auth_calls == ["owner-key"]


def task_grant(helpers, scope="adk.manage", **changes):
    grant = {"actor": "approved-worker", "task": "approved-task", "scope": scope, "active": True, "expires": time.monotonic() + 60}
    grant.update(changes)
    helpers.grants.append(grant)
    return {"session_actor_id": "approved-worker", "task_id": "approved-task"}


def test_legitimate_gate_task_grant_does_not_require_a_permanent_key_scope(adapter):
    app, helpers = create_host(adapter[0])
    context = task_grant(helpers)
    response = invoke(app, "agent.create", definition(), key="task-key", context=context)
    assert response.status_code == 201 and response.json()["ok"] is True
    assert helpers.key_scopes("task-key") == ["tier:1"]
    assert helpers.hook_calls == 1 and helpers.auth_calls == ["task-key"]
    assert "sdk-worker" in adapter[2].agents


@pytest.mark.parametrize(
    ("key", "allowed", "refused"),
    [("adk-key", "agent.list", "bot.list"), ("bots-key", "bot.list", "agent.list")],
)
def test_dedicated_static_scope_is_limited_to_its_own_namespace(adapter, key, allowed, refused):
    app, helpers = create_host(adapter[0])
    rejected = invoke(app, refused, {}, key=key)
    assert rejected.status_code == 403 and helpers.hook_calls == 0
    accepted = invoke(app, allowed, {}, key=key)
    assert accepted.status_code == 200 and accepted.json()["ok"] is True
    assert helpers.hook_calls == 1 and helpers.auth_calls == [key]


def test_task_grant_cannot_cross_namespace_or_owner(adapter):
    app, helpers = create_host(adapter[0])
    context = task_grant(helpers, scope="echobots.manage")
    refused = invoke(app, "agent.create", definition(), key="task-key", context=context)
    assert refused.status_code == 403 and helpers.hook_calls == 0
    wrong_owner = invoke(app, "bot.list", {}, key="other-task-key", context=context)
    assert wrong_owner.status_code == 403 and wrong_owner.json()["error"] == "denied"
    accepted = invoke(app, "bot.list", {}, key="task-key", context=context)
    assert accepted.status_code == 200 and accepted.json()["ok"] is True
    assert "sdk-worker" not in adapter[2].agents


@pytest.mark.parametrize(
    "change",
    [{"active": False}, {"expires": 0}, {"actor": "different-worker"}, {"task": "different-task"}, {"scope": "echobots.manage"}],
)
def test_revoked_expired_or_mismatched_task_grant_never_arms_admission(adapter, change):
    app, helpers = create_host(adapter[0])
    context = task_grant(helpers, **change)
    response = invoke(app, "agent.create", definition(), key="task-key", context=context)
    assert response.status_code == 403 and response.json()["error"] == "scope_forbidden"
    assert helpers.hook_calls == 0 and not helpers.auth_calls
    assert "sdk-worker" not in adapter[2].agents


def test_request_context_cannot_supply_its_own_grant_or_admitted_scope(adapter):
    app, helpers = create_host(adapter[0])
    response = invoke(
        app,
        "agent.create",
        definition(),
        key="weak-key",
        context={
            "session_actor_id": "approved-worker",
            "task_id": "approved-task",
            "scopes": ["adk.manage", "tier:2"],
            "admitted_scope": "adk.manage",
            "grant": {"active": True, "scope": "adk.manage", "owner": "owner@example.test"},
        },
    )
    assert response.status_code == 403 and helpers.hook_calls == 0
    assert not helpers.auth_calls and "sdk-worker" not in adapter[2].agents


def test_global_tier_label_alone_does_not_cover_named_management_scope(adapter):
    app, helpers = create_host(adapter[0])
    response = invoke(app, "agent.create", definition(), key="tier2-only-key")
    assert response.status_code == 403 and helpers.hook_calls == 0
    assert not helpers.auth_calls and "sdk-worker" not in adapter[2].agents


@pytest.mark.parametrize(("decision", "status"), [("missing", 423), ("invalid", 401)])
def test_valid_task_scope_does_not_bypass_independent_gate_hmac_refusal(adapter, decision, status):
    app, helpers = create_host(adapter[0])
    context = task_grant(helpers)
    helpers.hmac_decision = decision
    response = invoke(app, "agent.create", definition(), key="task-key", context=context)
    assert response.status_code == status and response.json()["error"] == "hmac_" + decision
    assert helpers.hook_calls == 0 and not helpers.auth_calls
    assert "sdk-worker" not in adapter[2].agents


def test_direct_target_with_owner_key_and_forged_identity_headers_cannot_bypass_gate(adapter):
    app, helpers = create_host(adapter[0])

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sdk.test") as client:
            return await client.post(
                "/adk-sdk/agent.create",
                json=definition(),
                headers={
                    "X-Echo-API-Key": "owner-key",
                    "X-Echo-Capability": PROPOSED_IDS["agent.create"],
                    "X-Commander-Email": "owner@example.test",
                },
            )

    response = asyncio.run(run())
    assert response.status_code == 403 and response.json()["error"] == "denied"
    assert "sdk-worker" not in adapter[2].agents and not helpers.auth_calls
    refused = invoke(app, "agent.create", definition(), admitted=False)
    assert refused.status_code == 403 and helpers.hook_calls == 0


@pytest.mark.parametrize(
    ("key", "status"),
    [("other-key", 403), ("inactive-key", 403), ("revoked-key", 401), ("unavailable-key", 503)],
)
def test_host_reauthenticates_owner_and_key_activity_after_gate(adapter, key, status):
    app, _ = create_host(adapter[0])
    response = invoke(app, "agent.create", definition(), key=key)
    assert response.status_code == status and response.json()["ok"] is False
    assert response.json()["outcome"] == "not_started" and "sdk-worker" not in adapter[2].agents
    assert "private authentication" not in response.text


@pytest.mark.parametrize(
    "change",
    [
        {"danger_tier": 1},
        {"required_scope": "tier:1"},
        {"required_scope": "echobots.manage"},
        {"handler_kind": "http"},
        {"target_method": "GET"},
        {"args_mode": "query"},
        {"target_url": "http://remote.example/adk-sdk/agent.create"},
        {"target_url": "http://127.0.0.1:8000/other"},
        {"target_url": "http://127.0.0.1:8000/adk-sdk/agent.create?override=yes"},
    ],
)
def test_registration_mismatch_or_remote_transport_never_arms_proof(adapter, change):
    app, helpers = create_host(adapter[0])
    helpers.capabilities[PROPOSED_IDS["agent.create"]].update(change)
    response = invoke(app, "agent.create", definition())
    assert response.status_code == 403 and response.json()["error"] == "denied"
    assert not helpers.auth_calls and "sdk-worker" not in adapter[2].agents


@pytest.mark.parametrize(
    "flags",
    [
        {"replace_params": definition("different-worker")},
        {"replace_key": "other-key"},
        {"replace_capability": PROPOSED_IDS["agent.replace"]},
        {"duplicate_key": True},
        {"expire": True},
        {"replace_admitted_scope": "echobots.manage"},
    ],
)
def test_admission_is_bound_to_exact_operation_params_key_and_time(adapter, flags):
    app, helpers = create_host(adapter[0])
    response = invoke(app, "agent.create", definition(), **flags)
    assert response.status_code == 403 and response.json()["error"] == "denied"
    assert not helpers.auth_calls and "sdk-worker" not in adapter[2].agents


def test_empty_or_repeated_outer_key_cannot_establish_a_trusted_frame(adapter):
    app, _ = create_host(adapter[0])

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sdk.test") as client:
            for headers in ([], [("X-Echo-API-Key", "owner-key"), ("X-Echo-API-Key", "owner-key")]):
                response = await client.post("/sdk/invoke", json={"operation": "agent.create", "params": definition()}, headers=headers)
                assert response.status_code == 403 and response.json()["error"] == "denied"

    asyncio.run(run())
    assert "sdk-worker" not in adapter[2].agents


def test_pre_hook_validation_never_treats_model_claims_as_authorization(adapter):
    app, helpers = create_host(adapter[0])
    response = invoke(app, "agent.create", {**definition(), "owner": True, "scopes": ["*"]})
    assert response.status_code == 403 and not helpers.auth_calls
    assert "sdk-worker" not in adapter[2].agents


def test_admission_is_consumed_once_before_authentication_can_yield(adapter):
    app, helpers = create_host(adapter[0])
    response = invoke(app, "agent.create", definition(), replay=True)
    assert response.json()["statuses"] == [201, 403]
    assert response.json()["first"]["ok"] is True and response.json()["second"]["ok"] is False
    assert helpers.auth_calls == ["owner-key"]


def test_later_refusal_revokes_unused_proof_in_inherited_background_context(adapter):
    app, helpers = create_host(adapter[0])

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sdk.test") as client:
            response = await client.post(
                "/sdk/invoke",
                headers={"X-Echo-API-Key": "owner-key"},
                json={"operation": "agent.create", "params": definition(), "later_refusal": True, "late_task": True},
            )
            assert response.status_code == 403 and _FRAME.get() is None
            helpers.release_late.set()
            inherited = await helpers.late_task
            assert inherited.status_code == 403 and inherited.json()["error"] == "denied"

    asyncio.run(run())
    assert not helpers.auth_calls and "sdk-worker" not in adapter[2].agents


def test_nested_and_concurrent_sdk_invocations_keep_separate_single_use_proofs(adapter):
    app, helpers = create_host(adapter[0])
    nested = invoke(app, "agent.create", definition(), nested=True)
    assert nested.status_code == 201

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sdk.test") as client:
            results = await asyncio.gather(
                *(
                    client.post(
                        "/sdk/invoke",
                        headers={"X-Echo-API-Key": "owner-key"},
                        json={"operation": "agent.create", "params": definition(name)},
                    )
                    for name in ("first-worker", "second-worker")
                )
            )
        assert [response.status_code for response in results] == [201, 201]
        assert _FRAME.get() is None

    asyncio.run(run())
    assert all(name in adapter[2].agents for name in ("sdk-worker", "first-worker", "second-worker"))
    assert helpers.auth_calls == ["owner-key"] * 4  # nested health + three separate creations


def test_factory_mounts_the_existing_sdk_main_app_using_actual_helper_import_contract(adapter, monkeypatch):
    app, sdk = FastAPI(), HostHelpers()
    routers = ModuleType("routers")
    routers.__path__ = []
    monkeypatch.setitem(sys.modules, "main", SimpleNamespace(app=app))
    monkeypatch.setitem(sys.modules, "routers", routers)
    monkeypatch.setitem(sys.modules, "routers.sdk_invoke", sdk)
    monkeypatch.setitem(sys.modules, "routers._dispatcher_projection", SimpleNamespace(project_envelope=project))
    monkeypatch.setenv("ECHO_ADK_OWNER", "owner@example.test")
    monkeypatch.setenv("ECHO_ADK_WORKER_ORIGIN", adapter[0].origin)
    assert create_app() is app and app.state.echo_adk_sdk_installed is True
    assert len(sdk.hooks) == 1

    async def verify_route():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sdk.test") as client:
            response = await client.post("/adk-sdk/health", json={})
            assert response.status_code == 403 and response.json()["error"] == "denied"

    asyncio.run(verify_route())
    with pytest.raises(RuntimeError, match="already installed"):
        create_app()
    monkeypatch.delenv("ECHO_ADK_OWNER")
    with pytest.raises(RuntimeError, match="Set ECHO_ADK_OWNER"):
        create_app()


def test_install_refuses_started_app_instead_of_silently_losing_the_middleware(adapter):
    app = FastAPI()

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sdk.test") as client:
            await client.get("/missing")

    asyncio.run(run())
    with pytest.raises(RuntimeError, match="before the SDK host starts"):
        install(app, owner="owner@example.test", worker_origin=adapter[0].origin)


def test_manifest_generation_does_not_mutate_operation_schemas():
    before = copy.deepcopy(OPERATIONS["agent.create"].schema)
    generated = proposed_manifest("http://127.0.0.1:8000")
    created = next(cap for cap in generated["capabilities"] if cap["id"] == PROPOSED_IDS["agent.create"])
    created["input_schema_json"]["properties"]["owner"] = {"type": "boolean"}
    assert OPERATIONS["agent.create"].schema == before
