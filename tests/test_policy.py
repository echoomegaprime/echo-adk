from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from echo_adk.core.policy import BUILTIN_CAPABILITIES, CapabilityRegistry, Policy, classify_tier
from echo_adk.core.types import Budget, Capability, Lease, Spent, ValidationError, new_id


def policy_for(registry, allowlist, **budget):
    return Policy(registry, allowlist, Budget(**{"max_calls": 5, "max_cost_usd": 0, "max_wall_s": 60, **budget}))


def test_registry_is_default_deny(registry, scout, lease_for):
    policy = policy_for(registry, scout.capabilities)
    lease = lease_for(scout, scopes=list(scout.capabilities) + ["echo.builds.log"])
    unknown = policy.evaluate("echo.not.registered", {}, lease)
    assert unknown.verdict == "denied" and unknown.code == "unknown_capability"
    assert classify_tier("echo.not.registered", registry) is None
    assert classify_tier("garbage", registry) is None
    # registered but outside the agent allowlist: denied, not gated
    outside = policy.evaluate("echo.builds.log", {}, lease)
    assert outside.verdict == "denied" and "allowlist" in outside.reason


def test_lease_scope_refusal_is_gated_with_needs_and_how(registry, scout, lease_for):
    policy = policy_for(registry, scout.capabilities)
    lease = lease_for(scout, scopes=["echo.caps.search"])
    decision = policy.evaluate("echo.knowledge.search", {"query": "x"}, lease)
    assert decision.verdict == "gated"
    assert decision.needs == "lease scope echo.knowledge.search"
    assert "re-mint" in (decision.how or "")
    assert policy.evaluate("echo.knowledge.search", {"query": "x"}, None).needs == "lease"


def test_lease_expiry_and_exhaustion(registry, scout, lease_for):
    policy = policy_for(registry, scout.capabilities)
    lease = lease_for(scout, ttl_s=1, max_uses=1)
    later = datetime.now(timezone.utc) + timedelta(seconds=5)
    assert policy.evaluate("echo.caps.search", {}, lease, now=later).reason == "lease expired"
    fresh = lease_for(scout, max_uses=1)
    fresh.consume()
    assert policy.evaluate("echo.caps.search", {}, fresh).reason == "lease uses exhausted"


def test_tier_two_needs_grant_ref(registry, lease_for, scout):
    policy = policy_for(registry, ["echo.test.privileged"])
    run_id = new_id()
    lease = Lease.mint(run_id, ["echo.test.privileged"], ttl_s=60, max_uses=3)
    gated = policy.evaluate("echo.test.privileged", {"x": "1"}, lease)
    assert gated.verdict == "gated" and gated.needs == "tier-2 grant" and "grant" in (gated.how or "")
    granted = Lease.mint(run_id, ["echo.test.privileged"], ttl_s=60, max_uses=3, grant_ref="esh-grant-abc")
    assert policy.evaluate("echo.test.privileged", {"x": "1"}, granted).verdict == "allowed"


def test_budget_checks_deny_with_budget_exceeded(registry, scout, lease_for):
    policy = policy_for(registry, scout.capabilities, max_calls=2, max_wall_s=10)
    lease = lease_for(scout)
    assert policy.evaluate("echo.caps.search", {}, lease, Spent(calls=2)).code == "budget_exceeded"
    assert policy.evaluate("echo.caps.search", {}, lease, Spent(calls=0, wall_s=11)).code == "budget_exceeded"
    assert policy.evaluate("echo.caps.search", {}, lease, Spent(calls=1, wall_s=1)).verdict == "allowed"


def test_params_are_validated_and_secret_fields_refused(registry, scout, lease_for):
    policy = policy_for(registry, scout.capabilities)
    lease = lease_for(scout)
    assert "missing required" in policy.evaluate("echo.knowledge.search", {}, lease).reason
    assert policy.evaluate("echo.knowledge.search", {"query": 5}, lease).reason == "param 'query' must be string"
    secret = policy.evaluate("echo.knowledge.search", {"query": "x", "api_key": "abc"}, lease)
    assert secret.verdict == "denied" and "secret-shaped" in secret.reason
    assert policy.evaluate("echo.knowledge.search", {"query": "x", "limit": 3}, lease).verdict == "allowed"


def test_builtin_registry_matches_live_tiers_recorded_2026_11_04():
    reg = CapabilityRegistry.builtin()
    assert len(reg) == len(BUILTIN_CAPABILITIES)
    assert reg.tier_of("echo.knowledge.search") == 0 and reg.tier_of("echo.monitor.health.node") == 0
    assert reg.tier_of("echo.context.remember") == 1 and reg.tier_of("echo.shell.run") == 1
    assert all(cap.health == "green" for cap in reg.capabilities())


def test_registry_load_json_accepts_records_and_caps_detail(tmp_path):
    reg = CapabilityRegistry()
    records = tmp_path / "caps.json"
    records.write_text(json.dumps({"capabilities": [Capability("echo.a.b", 0, "a.read").to_dict()]}), encoding="utf-8")
    assert reg.load_json(records) == 1 and "echo.a.b" in reg
    details = tmp_path / "details.json"
    details.write_text(json.dumps({"caps": [{"cap": {"id": "echo.c.d", "tier": 1, "scope": "c.write", "params": []}}]}), encoding="utf-8")
    assert reg.load_json(details) == 1 and reg.tier_of("echo.c.d") == 1
    with pytest.raises(ValidationError):
        Policy(reg, ["nope"], Budget())
