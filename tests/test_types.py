from __future__ import annotations

import json

import pytest

from echo_adk.core.types import (
    BUDGET_CAPS,
    Budget,
    Capability,
    EchoAgent,
    Envelope,
    Lane,
    Lease,
    Proof,
    Role,
    ValidationError,
    new_id,
    now_iso,
    parse_iso,
)


def test_ids_are_32_lowercase_hex():
    value = new_id()
    assert len(value) == 32 and value == value.lower() and int(value, 16) >= 0


def test_timestamps_round_trip_with_explicit_offset():
    stamp = now_iso()
    assert stamp.endswith("+00:00")
    assert parse_iso(stamp).tzinfo is not None
    assert parse_iso("2026-11-04T00:00:00Z").isoformat().endswith("+00:00")


def test_budget_clamps_into_service_caps_and_refuses_unknown_keys():
    clamped = Budget.from_dict({"max_calls": 999, "max_cost_usd": 99, "max_wall_s": 999999})
    assert clamped.to_dict() == {"max_calls": BUDGET_CAPS["max_calls"], "max_cost_usd": 5.0, "max_wall_s": BUDGET_CAPS["max_wall_s"]}
    with pytest.raises(ValidationError):
        Budget.from_dict({"max_calls": 1, "bogus": 2})
    with pytest.raises(ValidationError):
        Budget(max_calls=0)
    lane = Budget(max_calls=40, max_cost_usd=0, max_wall_s=600)
    assert Budget.from_dict({"max_calls": 100}, defaults=lane).clamp_to(lane).max_calls == 40


def test_agent_from_dict_is_strict(scout):
    data = scout.to_dict()
    data.pop("source")
    data["unexpected"] = 1
    with pytest.raises(ValidationError, match="unknown field"):
        EchoAgent.from_dict(data)
    data.pop("unexpected")
    data["capabilities"] = ["not-a-cap"]
    with pytest.raises(ValidationError):
        EchoAgent.from_dict(data)
    data["capabilities"] = ["echo.caps.search", "echo.caps.search"]
    with pytest.raises(ValidationError, match="duplicate"):
        EchoAgent.from_dict(data)


def test_agent_json_round_trip(scout):
    again = EchoAgent.from_json(scout.to_json(), source=scout.source)
    assert again == scout
    assert json.loads(scout.to_json())["lane"]["budget"] == {"max_calls": 40, "max_cost_usd": 0.0, "max_wall_s": 600}


def test_lease_requires_exact_capability_ids():
    run_id = new_id()
    with pytest.raises(ValidationError):
        Lease.mint(run_id, ["echo.*"], ttl_s=60, max_uses=3)
    with pytest.raises(ValidationError):
        Lease.mint(run_id, ["*"], ttl_s=60, max_uses=3)
    lease = Lease.mint(run_id, ["echo.caps.search"], ttl_s=60, max_uses=2)
    assert lease.permits("echo.caps.search") and not lease.permits("echo.caps.list")
    lease.consume()
    lease.consume()
    assert lease.exhausted()
    with pytest.raises(ValidationError):
        lease.consume()
    assert Lease.from_dict(lease.to_dict()) == lease


def test_envelope_is_always_untrusted_and_version_1():
    env = Envelope(run_id=new_id(), step=1, kind="cap", verdict="allowed", ok=True, capability="echo.caps.list", tier=0)
    payload = env.to_dict()
    assert payload["untrusted"] is True and payload["envelope_version"] == 1
    assert set(payload) == {
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
    payload["untrusted"] = False
    assert Envelope.from_dict(payload).untrusted is True  # cannot be switched off
    with pytest.raises(ValidationError):
        Envelope(run_id=new_id(), step=1, kind="bogus", verdict="allowed", ok=True)
    with pytest.raises(ValidationError):
        Envelope.from_dict({**payload, "extra": 1})


def test_proof_hash_is_deterministic_over_envelopes():
    run_id, lease_id = new_id(), new_id()
    envs = [
        Envelope(run_id=run_id, step=1, kind="model", verdict="allowed", ok=True, data={"answered": {"provider": "x"}}),
        Envelope(run_id=run_id, step=2, kind="cap", verdict="allowed", ok=True, capability="echo.caps.list", tier=0, audit_id="a"),
        Envelope(run_id=run_id, step=3, kind="final", verdict="allowed", ok=True, data={"text": "done"}),
    ]
    proof = Proof.from_envelopes(run_id, lease_id, envs, "PASS")
    again = Proof.from_envelopes(run_id, lease_id, envs, "PASS")
    assert proof.events_sha256 == again.events_sha256 and len(proof.events_sha256) == 64
    assert proof.actions == ({"cap": "echo.caps.list", "tier": 0, "verdict": "allowed", "audit_id": "a"},)
    envs[1].data["x"] = 1
    assert Proof.from_envelopes(run_id, lease_id, envs, "PASS").events_sha256 != proof.events_sha256
    assert Proof.from_dict(proof.to_dict()) == proof
    with pytest.raises(ValidationError):
        Proof.from_envelopes(run_id, lease_id, envs, "MAYBE")


def test_capability_from_caps_detail_builds_schema():
    detail = {
        "cap": {
            "id": "echo.knowledge.search",
            "tier": 0,
            "scope": "knowledge.read",
            "health": "green",
            "verb": "search",
            "params": [{"name": "query", "type": "string", "required": True}, {"name": "limit", "type": "integer", "required": False}],
        }
    }
    cap = Capability.from_caps_detail(detail)
    assert cap.input_schema == {
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["query"],
    }
    with pytest.raises(ValidationError):
        Capability.from_caps_detail({"cap": {"id": "echo.x.y", "tier": "zero"}})
    with pytest.raises(ValidationError):
        Capability("echo.x.y", 9, "s")


def test_role_and_lane_validation():
    assert Role("researcher", tier=7).to_dict()["composed_skills"] == []
    with pytest.raises(ValidationError):
        Role("Not Kebab")
    with pytest.raises(ValidationError):
        Lane("forge", "bad model name!")
    with pytest.raises(ValidationError):
        Lane.from_dict({"node": "forge", "model": "qwen-local", "extra": 1})
