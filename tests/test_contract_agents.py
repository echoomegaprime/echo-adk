"""Contract addition 2026-11-04 20:12: POST/PUT/DELETE /agents with ``origin``; ``daily_cost_cap_usd: 0`` on bots."""

from __future__ import annotations

import hashlib
import json

import pytest

from echo_adk.api.server import BackgroundServer
from echo_adk.api.service import AdkService
from tests.conftest import http, wait_for
from tests.fakes import FakeBroker, ScriptedModel, cap_action, final_action
from tests.test_contract import assert_json_error

AGENT_KEYS = {"name", "role", "skills", "capabilities", "lane", "max_steps", "source"}


@pytest.fixture
def service(agents_copy, tmp_path):
    svc = AdkService(
        agents_copy,
        tmp_path / "state",
        model_factory=lambda model_id: ScriptedModel([cap_action("echo.caps.search", q="x"), final_action("built")]),
        broker=FakeBroker(),
        start_scheduler=False,
    )
    yield svc
    svc.close()


@pytest.fixture
def api(service):
    with BackgroundServer(service) as url:
        yield url


def definition(**overrides):
    base = {
        "name": "prompt-scout",
        "role": "researcher",
        "skills": ["echo-researcher-power"],
        "capabilities": ["echo.caps.search", "echo.knowledge.search"],
        "lane": {"node": "forge", "model": "qwen-local", "budget": {"max_calls": 20, "max_cost_usd": 0, "max_wall_s": 300}},
        "max_steps": 6,
        "instruction": "Built by Echo from a prompt.",
    }
    base.update(overrides)
    return base


def test_post_agent_creates_file_and_origin(api, service, agents_copy):
    digest = hashlib.sha256(b"make me a scout").hexdigest()
    status, headers, body = http("POST", f"{api}/agents", definition(origin={"by": "echo", "prompt_sha256": digest}))
    assert status == 201 and headers["content-type"] == "application/json"
    assert set(body) == AGENT_KEYS | {"instruction", "origin"} and body["source"] == "prompt-scout.json"
    assert body["origin"]["by"] == "echo" and body["origin"]["prompt_sha256"] == digest and body["origin"]["created_at"].endswith("+00:00")
    saved = json.loads((agents_copy / "prompt-scout.json").read_text(encoding="utf-8"))
    assert saved["name"] == "prompt-scout" and saved["origin"] == body["origin"] and "source" not in saved
    assert http("GET", f"{api}/agents/prompt-scout")[2] == body
    assert "prompt-scout" in {a["name"] for a in http("GET", f"{api}/agents")[2]["agents"]}
    assert http("GET", f"{api}/health")[2]["agents"] == 4
    # the created definition runs like any other
    _, _, run = http("POST", f"{api}/runs", {"agent": "prompt-scout", "prompt": "go"})
    done = wait_for(
        lambda: (lambda r: r if r["status"] in ("done", "failed", "gated") else None)(http("GET", f"{api}/runs/{run['run_id']}")[2])
    )
    assert done["status"] == "done" and done["budget"]["max_calls"] == 20
    # a fresh service reloads it from disk
    again = AdkService(agents_copy, service.state_dir, model_factory=service.model_factory, broker=FakeBroker(), start_scheduler=False)
    try:
        assert again.get_agent("prompt-scout").origin["by"] == "echo"
    finally:
        again.close()


def test_post_agent_defaults_origin_to_owner_and_clamps_budget(api):
    body = definition(
        name="owner-made", lane={"node": "forge", "model": "qwen-local", "budget": {"max_calls": 999, "max_cost_usd": 9, "max_wall_s": 1}}
    )
    status, _, created = http("POST", f"{api}/agents", body)
    assert status == 201 and created["origin"]["by"] == "owner" and "prompt_sha256" not in created["origin"]
    assert created["lane"]["budget"] == {"max_calls": 120, "max_cost_usd": 5.0, "max_wall_s": 1}


def test_post_agent_validation_and_conflict(api):
    assert_json_error(*http("POST", f"{api}/agents", definition(name="Bad Name")), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/agents", definition(name="a")), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/agents", definition(name="x" * 64)), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/agents", definition(capabilities=["echo.not.registered"])), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/agents", definition(surprise=1)), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/agents", definition(source="x.json")), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/agents", definition(origin={"by": "stranger"})), "invalid", 400)
    assert_json_error(*http("POST", f"{api}/agents", definition(origin={"by": "echo", "prompt_sha256": "nope"})), "invalid", 400)
    assert_json_error(
        *http("POST", f"{api}/agents", definition(origin={"by": "echo", "created_at": "2026-01-01T00:00:00+00:00"})), "invalid", 400
    )
    assert_json_error(*http("POST", f"{api}/agents", [1]), "invalid", 400)
    assert http("POST", f"{api}/agents", definition())[0] == 201
    assert_json_error(*http("POST", f"{api}/agents", definition()), "invalid", 409)
    assert_json_error(*http("POST", f"{api}/agents", definition(name="knowledge-scout")), "invalid", 409)


def test_put_agent_replaces_and_keeps_created_at(api, agents_copy):
    created = http("POST", f"{api}/agents", definition(origin={"by": "echo"}))[2]
    status, _, replaced = http("PUT", f"{api}/agents/prompt-scout", definition(instruction="Revised by the owner.", origin={"by": "owner"}))
    assert status == 200 and replaced["instruction"] == "Revised by the owner." and replaced["origin"]["by"] == "owner"
    assert replaced["origin"]["created_at"] == created["origin"]["created_at"] and replaced["origin"]["updated_at"]
    assert json.loads((agents_copy / "prompt-scout.json").read_text(encoding="utf-8"))["instruction"] == "Revised by the owner."
    assert http("GET", f"{api}/agents/prompt-scout")[2] == replaced
    assert_json_error(*http("PUT", f"{api}/agents/prompt-scout", definition(name="other-name")), "invalid", 400)
    assert_json_error(*http("PUT", f"{api}/agents/never-made", definition(name="never-made")), "not_found", 404)
    assert_json_error(
        *http("PUT", f"{api}/agents/prompt-scout", definition(capabilities=["echo.shell.run", "echo.nope.x"])), "invalid", 400
    )


def test_delete_agent_refused_while_a_bot_references_it(api, agents_copy):
    http("POST", f"{api}/agents", definition())
    bot = http("POST", f"{api}/bots", {"name": "uses-scout", "agent": "prompt-scout"})[2]
    status, headers, body = http("DELETE", f"{api}/agents/prompt-scout")
    assert_json_error(status, headers, body, "invalid", 409)
    assert bot["bot_id"] in body["message"]
    assert (agents_copy / "prompt-scout.json").exists()
    http("DELETE", f"{api}/bots/{bot['bot_id']}")
    status, _, body = http("DELETE", f"{api}/agents/prompt-scout")
    assert status == 200 and body == {"name": "prompt-scout", "deleted": True}
    assert not (agents_copy / "prompt-scout.json").exists()
    assert_json_error(*http("GET", f"{api}/agents/prompt-scout"), "not_found", 404)
    assert_json_error(*http("DELETE", f"{api}/agents/prompt-scout"), "not_found", 404)
    assert_json_error(*http("PATCH", f"{api}/agents/knowledge-scout"), "invalid", 405)


def test_bot_accepts_daily_cost_cap_zero_for_local_lane(api, service):
    status, _, bot = http("POST", f"{api}/bots", {"name": "free-bot", "agent": "forge-steward", "daily_cost_cap_usd": 0})
    assert status == 201 and bot["daily_cost_cap_usd"] == 0 and bot["spent_today_usd"] == 0
    http("POST", f"{api}/bots/{bot['bot_id']}/resume")
    queued = http("POST", f"{api}/bots/{bot['bot_id']}/message", {"text": "hello"})[2]
    assert service.bots.tick() == 1  # a zero-cost lane fits a zero cap
    run = wait_for(lambda: (lambda r: r if r["status"] in ("done", "failed") else None)(http("GET", f"{api}/runs/{queued['run_id']}")[2]))
    assert run["status"] == "done" and run["budget"]["max_cost_usd"] == 0.0
