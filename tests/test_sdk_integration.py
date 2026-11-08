"""Exercise the actual SDK envelope contract over HTTP, including false success.

The contract was checked against the installed SOL client and owner gate source
on 2026-11-07. Fixtures are explicitly offline; they are not live certification.
"""

import pytest

from echo_adk.core.broker import BrokerError, GateBroker
from echo_adk.core.policy import CapabilityRegistry
from echo_adk.core.runner import Runner
from echo_adk.core.types import EchoAgent, Lease, new_id
from tests.fakes import FakeHttpServer, ScriptedModel, cap_action, final_action

CAP = "echo.fixture.record"
DETAIL = {
    "id": CAP,
    "tier": 1,
    "scope": "fixture.write",
    "verb": "record",
    "health": "green",
    "params": [{"name": "text", "type": "string", "required": True}],
}


def gate_envelope(body, *, status="ok", target_status=200):
    return {
        "run_id": "gate-routing-evidence-1",
        "status": status,
        "result": {"status_code": target_status, "body": body},
        "trace": {"handler_kind": "router", "envelope_version": 1},
        "warnings": [],
    }


def responder(record):
    req = record["body"]
    assert "options" not in req["params"]
    if req["capability"] == "echo.caps.detail":
        assert req["params"] == {"command": "detail", "id": CAP}
        return 200, gate_envelope({"ok": True, "cap": DETAIL})
    if req["capability"] == "echo.caps.search":
        assert req["params"] == {"command": "search", "q": CAP, "limit": 100}
        return 200, gate_envelope({"ok": True, "caps": [DETAIL]})
    assert req["capability"] == CAP
    assert req["params"] == {"command": "record", "text": "real body"}
    return 200, gate_envelope({"ok": True, "record_id": "returned-record"})


def invoke(broker):
    rid = new_id()
    return broker.invoke(CAP, {"text": "real body"}, Lease.mint(rid, [CAP], ttl_s=60, max_uses=5), run_id=rid, step=1, tier=1)


def test_live_contract_hydrates_unknown_capability_and_keeps_routing_evidence(monkeypatch):
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", "offline-test-key")
    with FakeHttpServer() as gate:
        gate.state["responder"] = responder
        registry = CapabilityRegistry()
        broker = GateBroker(gate.url, registry=registry)
        env = invoke(broker)
        assert env.ok and env.data["result"]["body"]["record_id"] == "returned-record"
        assert env.data["run_id"] == "gate-routing-evidence-1" and env.audit_id is None
        assert registry.get(CAP).scope == "fixture.write"
        assert len(gate.requests) == 3
        assert invoke(broker).ok
        assert len(gate.requests) == 4  # metadata cached; the actual operation is never cached/replayed


@pytest.mark.parametrize(
    "bad",
    [
        {"ok": True},
        gate_envelope({"ok": False, "error": "id required"}),
        gate_envelope({"success": False, "message": "refused"}),
        gate_envelope({"ok": "false"}),
        {**gate_envelope({"ok": True}), "success": "false"},
        gate_envelope({"ok": True}, status="error"),
        gate_envelope({"error": "denied"}, target_status=403),
        gate_envelope({"ok": True}, target_status="200"),
        gate_envelope("not JSON"),
    ],
)
def test_transport_and_domain_failures_never_become_success(monkeypatch, bad):
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", "offline-test-key")
    with FakeHttpServer() as gate:
        gate.state["responder"] = lambda record: (200, bad) if record["body"]["capability"] == CAP else responder(record)
        env = invoke(GateBroker(gate.url))
        assert env.ok is False


def test_detail_without_exact_active_membership_never_invokes_target(monkeypatch):
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", "offline-test-key")
    with FakeHttpServer() as gate:
        gate.state["responder"] = lambda record: (
            (200, gate_envelope({"ok": True, "caps": []})) if record["body"]["capability"] == "echo.caps.search" else responder(record)
        )
        with pytest.raises(BrokerError, match="not verified active"):
            invoke(GateBroker(gate.url))
        assert [r["body"]["capability"] for r in gate.requests] == ["echo.caps.detail", "echo.caps.search"]


def test_new_capability_runs_through_policy_real_http_and_proof(monkeypatch):
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", "offline-test-key")
    agent = EchoAgent.from_dict(
        {
            "name": "sdk-worker",
            "role": "builder",
            "skills": [],
            "capabilities": [CAP],
            "lane": {"node": "local", "model": "ollama:fixture", "budget": {"max_calls": 5, "max_cost_usd": 0, "max_wall_s": 60}},
            "max_steps": 3,
        }
    )
    model = ScriptedModel([cap_action(CAP, text="real body"), final_action("Recorded returned-record")])
    with FakeHttpServer() as gate:
        gate.state["responder"] = responder
        broker = GateBroker(gate.url, registry=CapabilityRegistry())
        rid = new_id()
        result = Runner(model, broker).run(agent, "record this", Lease.mint(rid, [CAP], ttl_s=60, max_uses=5), run_id=rid)
        assert result.proof.verdict == "PASS"
        assert result.spent.calls == 3
        assert "returned-record" in model.seen[-1][1][-1]["content"]
        assert result.steps[1].data["run_id"] == "gate-routing-evidence-1"


def test_redirect_is_not_followed_with_a_gate_credential(monkeypatch):
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", "offline-test-key")
    with FakeHttpServer() as other, FakeHttpServer() as gate:
        gate.state.update(status=307, payload={"error": "redirect"}, headers={"Location": other.url + "/sdk/invoke"})
        broker = GateBroker(gate.url)
        with pytest.raises(BrokerError):
            invoke(broker)
        assert other.requests == []


def test_model_cannot_override_the_verified_sdk_verb(monkeypatch):
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", "offline-test-key")
    with FakeHttpServer() as gate:
        gate.state["responder"] = responder
        broker = GateBroker(gate.url)
        rid = new_id()
        with pytest.raises(BrokerError, match="override"):
            broker.invoke(
                CAP, {"command": "erase", "text": "real body"}, Lease.mint(rid, [CAP], ttl_s=60, max_uses=5), run_id=rid, step=1, tier=1
            )
        assert len(gate.requests) == 2
