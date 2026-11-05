"""Contract test for docs/CONTRACT.md: every route, shape and status code Prime's stand-in mirrors.

Runs over real loopback HTTP against ``echo_adk.api`` with a scripted model and an in-process broker.
"""

from __future__ import annotations

import re

import pytest

from echo_adk import PROFILE, __version__
from echo_adk.api.server import BackgroundServer, make_server
from echo_adk.api.service import AdkService
from echo_adk.core.models import ProviderUnavailable
from echo_adk.core.types import ValidationError
from tests.conftest import http, wait_for
from tests.fakes import FakeBroker, ScriptedModel, cap_action, final_action

HEX32 = re.compile(r"^[0-9a-f]{32}$")
ENVELOPE_KEYS = {
    "envelope_version",
    "run_id",
    "step",
    "kind",
    "capability",
    "tier",
    "verdict",
    "ok",
    "untrusted",
    "redacted",
    "audit_id",
    "duration_ms",
    "needs",
    "how",
    "data",
}
PROOF_KEYS = {"run_id", "lease_id", "events_sha256", "actions", "artifacts", "receipts", "verdict"}
BOT_KEYS = {
    "bot_id",
    "name",
    "agent",
    "mode",
    "status",
    "schedule",
    "plugins",
    "memory",
    "lease",
    "daily_cost_cap_usd",
    "spent_today_usd",
    "inbox_depth",
    "created_at",
}


@pytest.fixture
def service(agents_copy, tmp_path):
    def factory(model_id: str):
        return ScriptedModel([cap_action("echo.caps.search", q="health"), final_action("two capabilities found")])

    svc = AdkService(agents_copy, tmp_path / "state", model_factory=factory, broker=FakeBroker(), start_scheduler=False)
    yield svc
    svc.close()


@pytest.fixture
def api(service):
    with BackgroundServer(service) as url:
        yield url


def assert_json_error(status, headers, body, code, expected_status=None):
    if expected_status is not None:
        assert status == expected_status
    assert headers["content-type"] == "application/json"
    assert set(body) >= {"error", "message"} and body["error"] == code and isinstance(body["message"], str)


def finished_run(api, run_id):
    return wait_for(lambda: (lambda r: r if r["status"] in ("done", "failed", "gated") else None)(http("GET", f"{api}/runs/{run_id}")[2]))


# ----------------------------------------------------------------------------- identity


def test_health_identity(api):
    status, headers, body = http("GET", f"{api}/health")
    assert status == 200 and headers["content-type"] == "application/json"
    assert body["ok"] is True and body["service"] == "echo-adk-api" and body["profile"] == PROFILE == "ECHO_ADK"
    assert body["version"] == __version__ and re.match(r"^\d+\.\d+\.\d+", body["version"])
    assert body["agents"] == 3 and body["bots"] == 0 and body["runs_active"] == 0


def test_unknown_route_and_method(api):
    assert_json_error(*http("GET", f"{api}/nope"), "not_found", 404)
    assert_json_error(*http("DELETE", f"{api}/health"), "invalid", 405)
    assert_json_error(*http("GET", f"{api}/runs/not-an-id"), "not_found", 404)
    assert_json_error(*http("GET", f"{api}/runs/{'0' * 32}"), "not_found", 404)


def test_loopback_only_bind(service):
    with pytest.raises(ValidationError):
        make_server(service, "0.0.0.0", 0)
    with pytest.raises(ValidationError):
        make_server(service, "192.168.1.5", 0)


# ----------------------------------------------------------------------------- agents


def test_agents_routes(api):
    status, _, body = http("GET", f"{api}/agents")
    assert status == 200 and {a["name"] for a in body["agents"]} == {"knowledge-scout", "forge-steward", "paid-steward"}
    for agent in body["agents"]:
        assert set(agent) >= {"name", "role", "skills", "capabilities", "lane", "max_steps", "source"}
        assert set(agent["lane"]) == {"node", "model", "budget"} and set(agent["lane"]["budget"]) == {
            "max_calls",
            "max_cost_usd",
            "max_wall_s",
        }
        assert agent["source"].endswith(".json")
    status, _, scout = http("GET", f"{api}/agents/knowledge-scout")
    assert (
        status == 200
        and scout["capabilities"] == ["echo.knowledge.search", "echo.knowledge.get", "echo.caps.search"]
        and scout["instruction"]
    )
    assert_json_error(*http("GET", f"{api}/agents/does-not-exist"), "not_found", 404)


# ----------------------------------------------------------------------------- runs


def test_run_lifecycle_shapes(api):
    status, headers, body = http("POST", f"{api}/runs", {"agent": "knowledge-scout", "prompt": "what caps mention health?"})
    assert status == 202 and headers["content-type"] == "application/json"
    assert HEX32.match(body["run_id"]) and body["status"] == "queued" and set(body) == {"run_id", "status"}
    run = finished_run(api, body["run_id"])
    assert set(run) == {"run_id", "agent", "status", "started_at", "finished_at", "steps", "proof", "budget", "spent"}
    assert run["status"] == "done" and run["agent"] == "knowledge-scout"
    assert run["budget"] == {"max_calls": 40, "max_cost_usd": 0.0, "max_wall_s": 600}
    assert set(run["spent"]) == {"calls", "cost_usd", "wall_s"} and run["spent"]["calls"] == 3
    kinds = [s["kind"] for s in run["steps"]]
    assert kinds == ["model", "cap", "model", "final"]
    for step in run["steps"]:
        assert (
            set(step) == ENVELOPE_KEYS and step["envelope_version"] == 1 and step["untrusted"] is True and step["run_id"] == run["run_id"]
        )
        assert step["verdict"] in ("allowed", "gated", "denied") and isinstance(step["redacted"], int)
    model_step = run["steps"][0]
    assert (
        set(model_step["data"]["answered"]) == {"provider", "requested", "model", "reported"}
        and model_step["data"]["answered"]["reported"] is True
    )
    cap_step = run["steps"][1]
    assert cap_step["capability"] == "echo.caps.search" and cap_step["tier"] == 0 and cap_step["audit_id"]
    assert run["steps"][-1]["data"]["text"] == "two capabilities found"
    proof = run["proof"]
    assert (
        set(proof) == PROOF_KEYS
        and proof["verdict"] == "PASS"
        and HEX32.match(proof["lease_id"])
        and re.match(r"^[0-9a-f]{64}$", proof["events_sha256"])
    )
    assert proof["actions"] == [{"cap": "echo.caps.search", "tier": 0, "verdict": "allowed", "audit_id": "audit-1"}]
    assert proof["artifacts"] == [] and proof["receipts"] == []


def test_run_budget_clamped_to_lane_and_service_caps(api):
    _, _, body = http(
        "POST",
        f"{api}/runs",
        {"agent": "knowledge-scout", "prompt": "x", "budget": {"max_calls": 999, "max_cost_usd": 50, "max_wall_s": 99999}},
    )
    run = finished_run(api, body["run_id"])
    assert run["budget"] == {"max_calls": 40, "max_cost_usd": 0.0, "max_wall_s": 600}
    _, _, body = http("POST", f"{api}/runs", {"agent": "paid-steward", "prompt": "x", "budget": {"max_calls": 5}})
    run = finished_run(api, body["run_id"])
    assert run["budget"]["max_calls"] == 5 and run["budget"]["max_cost_usd"] == 2.0


def test_run_invalid_bodies(api):
    assert_json_error(*http("POST", f"{api}/runs", {"agent": "knowledge-scout"}), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/runs", {"agent": "knowledge-scout", "prompt": "x" * 12001}), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/runs", {"agent": "knowledge-scout", "prompt": "x", "extra": 1}), "invalid", 400)
    assert_json_error(
        *http("POST", f"{api}/runs", {"agent": "knowledge-scout", "prompt": "x", "budget": {"max_calls": "ten"}}), "invalid", 400
    )
    assert_json_error(*http("POST", f"{api}/runs", {"agent": "ghost", "prompt": "x"}), "not_found", 404)
    assert_json_error(*http("POST", f"{api}/runs", [1, 2]), "invalid", 400)


def test_run_lease_must_be_exact_ids_inside_allowlist(api):
    base = {"agent": "knowledge-scout", "prompt": "x"}
    assert_json_error(*http("POST", f"{api}/runs", {**base, "lease": {"scopes": ["*"], "ttl_s": 60, "max_uses": 5}}), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/runs", {**base, "lease": {"scopes": ["echo.*"], "ttl_s": 60, "max_uses": 5}}), "invalid", 400)
    assert_json_error(
        *http("POST", f"{api}/runs", {**base, "lease": {"scopes": ["echo.shell.run"], "ttl_s": 60, "max_uses": 5}}), "invalid", 400
    )
    assert_json_error(
        *http("POST", f"{api}/runs", {**base, "lease": {"scopes": ["echo.caps.search"], "ttl_s": 0, "max_uses": 5}}), "invalid", 400
    )
    # a narrower lease is honoured: the model's first call (echo.caps.search) is allowed here ...
    status, _, body = http("POST", f"{api}/runs", {**base, "lease": {"scopes": ["echo.caps.search"], "ttl_s": 60, "max_uses": 5}})
    assert status == 202 and finished_run(api, body["run_id"])["status"] == "done"
    # ... and gated when the lease excludes it
    status, _, body = http("POST", f"{api}/runs", {**base, "lease": {"scopes": ["echo.knowledge.get"], "ttl_s": 60, "max_uses": 5}})
    run = finished_run(api, body["run_id"])
    assert run["status"] == "gated" and run["proof"]["verdict"] == "UNKNOWN" and "error" not in run
    gated = run["steps"][-1]
    assert gated["verdict"] == "gated" and gated["needs"] == "lease scope echo.caps.search" and gated["how"]


def test_run_stop_and_list(api):
    created = [http("POST", f"{api}/runs", {"agent": "forge-steward", "prompt": f"n{i}"})[2]["run_id"] for i in range(3)]
    status, _, body = http("POST", f"{api}/runs/{created[0]}/stop")
    assert status == 200 and set(body) == {"run_id", "status"} and body["run_id"] == created[0]
    assert_json_error(*http("POST", f"{api}/runs/{'f' * 32}/stop"), "not_found", 404)
    for run_id in created:
        finished_run(api, run_id)
    status, _, body = http("GET", f"{api}/runs?agent=forge-steward&limit=2")
    assert status == 200 and len(body["runs"]) == 2
    assert all(set(r) == {"run_id", "agent", "status", "started_at"} for r in body["runs"])
    assert body["runs"][0]["run_id"] == created[-1]  # newest first
    status, _, body = http("GET", f"{api}/runs?limit=500")
    assert status == 200 and len(body["runs"]) <= 50
    assert_json_error(*http("GET", f"{api}/runs?limit=abc"), "invalid", 400)
    assert http("GET", f"{api}/runs?agent=unknown-agent")[2] == {"runs": []}


def test_run_unavailable_provider_names_provider(agents_copy, tmp_path):
    def factory(model_id: str):
        raise ProviderUnavailable("claude", "not signed in")

    svc = AdkService(agents_copy, tmp_path / "state2", model_factory=factory, broker=FakeBroker(), start_scheduler=False)
    try:
        with BackgroundServer(svc) as api:
            _, _, body = http("POST", f"{api}/runs", {"agent": "knowledge-scout", "prompt": "x"})
            run = finished_run(api, body["run_id"])
            assert run["status"] == "failed" and run["proof"]["verdict"] == "FAIL"
            assert (
                run["error"]["code"] == "unavailable"
                and run["error"]["provider"] == "claude"
                and "no fallback was used" in run["error"]["message"]
            )
    finally:
        svc.close()


def test_service_daily_cap_refuses_before_queueing(agents_copy, tmp_path):
    svc = AdkService(
        agents_copy,
        tmp_path / "state3",
        model_factory=lambda m: ScriptedModel([final_action("x")]),
        broker=FakeBroker(),
        daily_cap_usd=0.3,
        start_scheduler=False,
    )
    try:
        with BackgroundServer(svc) as api:
            status, headers, body = http("POST", f"{api}/runs", {"agent": "paid-steward", "prompt": "x", "budget": {"max_cost_usd": 0.5}})
            assert_json_error(status, headers, body, "budget_exceeded", 429)
            assert http("GET", f"{api}/health")[2]["runs_active"] == 0
    finally:
        svc.close()


# ----------------------------------------------------------------------------- bots


def test_bot_crud_shapes(api):
    status, headers, bot = http(
        "POST",
        f"{api}/bots",
        {"name": "scout-bot", "agent": "knowledge-scout", "schedule": {"every_s": 3600}, "instruction": "watch the forge"},
    )
    assert status == 201 and headers["content-type"] == "application/json"
    assert set(bot) >= BOT_KEYS and bot["status"] == "paused" and bot["mode"] == "personal"
    assert HEX32.match(bot["bot_id"]) and bot["memory"] == {"scope": f"bot:{bot['bot_id']}"}
    assert bot["lease"] == {"scopes": ["echo.knowledge.search", "echo.knowledge.get", "echo.caps.search"], "max_uses": 40, "ttl_s": 600}
    assert (
        bot["daily_cost_cap_usd"] <= 5
        and bot["spent_today_usd"] == 0
        and bot["inbox_depth"] == 0
        and bot["instruction"] == "watch the forge"
    )
    assert bot["schedule"] == {"every_s": 3600} and bot["plugins"] == []
    assert http("GET", f"{api}/bots")[2] == {"bots": [bot]}
    assert http("GET", f"{api}/bots/{bot['bot_id']}")[2] == bot
    assert_json_error(*http("GET", f"{api}/bots/{'a' * 32}"), "not_found", 404)
    status, _, resumed = http("POST", f"{api}/bots/{bot['bot_id']}/resume")
    assert status == 200 and resumed["status"] == "idle"
    status, _, paused = http("POST", f"{api}/bots/{bot['bot_id']}/pause")
    assert status == 200 and paused["status"] == "paused"
    status, _, gone = http("DELETE", f"{api}/bots/{bot['bot_id']}")
    assert status == 200 and gone == {"bot_id": bot["bot_id"], "deleted": True}
    assert_json_error(*http("GET", f"{api}/bots/{bot['bot_id']}"), "not_found", 404)
    assert_json_error(*http("DELETE", f"{api}/bots/{bot['bot_id']}"), "not_found", 404)


def test_bot_create_validation(api):
    base = {"name": "b", "agent": "forge-steward"}
    assert_json_error(*http("POST", f"{api}/bots", {**base, "schedule": {"every_s": 30}}), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/bots", {**base, "daily_cost_cap_usd": 6}), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/bots", {**base, "name": "x" * 65}), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/bots", {**base, "mode": "shared-computer"}), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/bots", {**base, "lease": {"scopes": ["*"]}}), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/bots", {**base, "lease": {"scopes": ["echo.shell.run"]}}), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/bots", {**base, "surprise": 1}), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/bots", {"name": "b", "agent": "ghost"}), "not_found", 404)
    assert (
        http("POST", f"{api}/bots", {**base, "mode": "team", "lease": {"scopes": ["echo.caps.search"], "max_uses": 3, "ttl_s": 120}})[0]
        == 201
    )


def test_bot_message_flow(api, service):
    bot = http("POST", f"{api}/bots", {"name": "steward-bot", "agent": "forge-steward"})[2]
    bot_id = bot["bot_id"]
    status, _, queued = http("POST", f"{api}/bots/{bot_id}/message", {"text": "how is forge?"})
    assert status == 202 and set(queued) == {"message_id", "run_id"} and HEX32.match(queued["message_id"]) and HEX32.match(queued["run_id"])
    assert_json_error(*http("POST", f"{api}/bots/{bot_id}/message", {"text": ""}), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/bots/{bot_id}/message", {"text": "x" * 12001}), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/bots/{'b' * 32}/message", {"text": "x"}), "not_found", 404)
    # paused: accepted, queued, not run
    assert http("GET", f"{api}/bots/{bot_id}")[2]["inbox_depth"] == 1
    assert http("GET", f"{api}/runs/{queued['run_id']}")[2]["status"] == "queued"
    assert service.bots.tick() == 0
    http("POST", f"{api}/bots/{bot_id}/resume")
    assert service.bots.tick() == 1
    run = finished_run(api, queued["run_id"])
    assert run["status"] == "done" and run["proof"]["verdict"] == "PASS"
    messages = wait_for(lambda: (lambda m: m if len(m["messages"]) == 2 else None)(http("GET", f"{api}/bots/{bot_id}/messages")[2]))[
        "messages"
    ]
    assert [m["role"] for m in messages] == ["user", "bot"] and messages[0]["message_id"] == queued["message_id"]
    assert all(set(m) >= {"message_id", "role", "text", "created_at"} for m in messages) and messages[1]["run_id"] == queued["run_id"]
    assert messages[1]["text"] == "two capabilities found"
    status, _, since = http("GET", f"{api}/bots/{bot_id}/messages?since={messages[0]['message_id']}&limit=5")
    assert status == 200 and [m["message_id"] for m in since["messages"]] == [messages[1]["message_id"]]
    assert_json_error(*http("GET", f"{api}/bots/{bot_id}/messages?since=nope"), "invalid", 400)
    final = http("GET", f"{api}/bots/{bot_id}")[2]
    assert final["status"] == "idle" and final["inbox_depth"] == 0 and final["last_run_at"]
    assert service.memory.read(f"bot:{bot_id}")[-1]["text"] == "two capabilities found"


# ----------------------------------------------------------------------------- groups


def test_group_flow_and_size_limits(api):
    bots = [http("POST", f"{api}/bots", {"name": f"g{i}", "agent": "forge-steward"})[2]["bot_id"] for i in range(3)]
    assert_json_error(*http("POST", f"{api}/groups", {"name": "solo", "bots": bots[:1], "prompt": "x"}), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/groups", {"name": "dup", "bots": [bots[0], bots[0]], "prompt": "x"}), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/groups", {"name": "ghost", "bots": [bots[0], "c" * 32], "prompt": "x"}), "invalid", 400)
    seven = bots + [http("POST", f"{api}/bots", {"name": f"g{i}", "agent": "forge-steward"})[2]["bot_id"] for i in range(3, 7)]
    assert len(seven) == 7
    assert_json_error(*http("POST", f"{api}/groups", {"name": "crowd", "bots": seven, "prompt": "x"}), "invalid", 400)
    status, _, body = http("POST", f"{api}/groups", {"name": "trio", "bots": bots, "prompt": "compare notes"})
    assert status == 202 and HEX32.match(body["group_id"]) and len(body["run_ids"]) == 3 and all(HEX32.match(r) for r in body["run_ids"])
    for run_id in body["run_ids"]:
        assert http("GET", f"{api}/runs/{run_id}")[0] == 200  # every bot turn is a normal run
    group = wait_for(
        lambda: (lambda g: g if g["status"] in ("done", "failed") else None)(http("GET", f"{api}/groups/{body['group_id']}")[2])
    )
    assert set(group) == {"group_id", "name", "bots", "status", "messages", "summary"} and group["status"] == "done"
    assert [m["bot_id"] for m in group["messages"]] == bots and all(set(m) == {"bot_id", "text", "created_at"} for m in group["messages"])
    assert group["summary"].startswith("trio (3 turns, done)")
    assert_json_error(*http("GET", f"{api}/groups/{'d' * 32}"), "not_found", 404)
    status, _, six = http("POST", f"{api}/groups", {"name": "six", "bots": seven[:6], "prompt": "x"})
    assert status == 202 and len(six["run_ids"]) == 6
