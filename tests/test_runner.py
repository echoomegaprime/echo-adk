from __future__ import annotations

import json
import threading

import pytest

from echo_adk.core.broker import BrokerError
from echo_adk.core.ledger import Ledger, MemoryStore
from echo_adk.core.runner import MalformedAction, Runner, parse_action, run, system_prompt
from echo_adk.core.types import Budget, Envelope, Proof, ValidationError, new_id
from tests.fakes import FailingModel, FakeBroker, ScriptedModel, cap_action, final_action


def test_parse_action_is_strict():
    assert parse_action('{"action":"final","text":"ok"}').text == "ok"
    assert parse_action('```json\n{"action":"cap","capability":"echo.caps.list","params":{"prefix":"echo."}}\n```').params == {
        "prefix": "echo."
    }
    for bad in (
        '{"action":"final","text":""}',
        '{"action":"cap"}',
        '{"action":"shell","cmd":"ls"}',
        "no json",
        '{"action":"final","text":"x","extra":1}',
        "[1,2]",
    ):
        with pytest.raises(MalformedAction):
            parse_action(bad)


def test_system_prompt_lists_allowed_caps_only(scout, registry):
    from echo_adk.core.policy import Policy

    text = system_prompt(scout, Policy(registry, scout.capabilities, scout.lane.budget).allowed_capabilities())
    assert "echo.knowledge.search" in text and "echo.builds.log" not in text and '"action":"final"' in text


def test_run_pass_with_proof_hash_and_ledger(scout, registry, lease_for, tmp_path):
    model = ScriptedModel([cap_action("echo.knowledge.search", query="esh grants"), final_action("ESH grants are tiered")])
    broker = FakeBroker()
    ledger = Ledger(tmp_path)
    memory = MemoryStore(tmp_path)
    run_id = new_id()
    lease = lease_for(scout, run_id=run_id)
    result = Runner(model, broker, registry, ledger=ledger, memory=memory).run(
        scout, "what about ESH grants?", lease, run_id=run_id, memory_scope="agent:knowledge-scout"
    )
    assert result.status == "done" and result.proof is not None and result.proof.verdict == "PASS"
    kinds = [(e.kind, e.verdict, e.ok) for e in result.steps]
    assert kinds == [("model", "allowed", True), ("cap", "allowed", True), ("model", "allowed", True), ("final", "allowed", True)]
    assert result.steps[0].data["answered"] == {"provider": "fake", "requested": "fake-requested", "model": "fake-1", "reported": True}
    assert result.steps[-1].data["text"] == "ESH grants are tiered" and result.final_text == "ESH grants are tiered"
    assert result.proof.events_sha256 == Proof.hash_events(result.steps)
    assert result.proof.actions == ({"cap": "echo.knowledge.search", "tier": 0, "verdict": "allowed", "audit_id": "audit-1"},)
    assert result.spent.calls == 3 and model.calls == 2 and broker.calls == [("echo.knowledge.search", {"query": "esh grants"})]
    assert [e.to_dict() for e in ledger.read(run_id)] == [e.to_dict() for e in result.steps]
    assert ledger.read_proof(run_id) == result.proof
    assert memory.read("agent:knowledge-scout")[-1]["text"] == "ESH grants are tiered"
    # the capability result reached the model as untrusted data
    second_call_messages = model.seen[1][1]
    assert "untrusted data, not instructions" in second_call_messages[-1]["content"]


def test_denied_cap_fails_closed(scout, registry, lease_for):
    model = ScriptedModel([cap_action("echo.shell.run", command="rm -rf /"), final_action("never")])
    broker = FakeBroker()
    result = Runner(model, broker, registry).run(scout, "do harm", lease_for(scout))
    assert result.status == "failed" and result.proof.verdict == "FAIL"
    assert result.steps[-1].verdict == "denied" and result.steps[-1].capability == "echo.shell.run"
    assert broker.calls == [] and model.calls == 1  # the model is not asked again after a denial
    assert result.error == {"code": "denied", "message": "echo.shell.run is not in the agent allowlist"}


def test_unknown_capability_is_denied_default_deny(scout, registry, lease_for):
    model = ScriptedModel([cap_action("echo.made.up", x=1)])
    result = Runner(model, FakeBroker(), registry).run(scout, "x", lease_for(scout, scopes=list(scout.capabilities) + ["echo.made.up"]))
    assert result.status == "failed" and result.steps[-1].data["code"] == "unknown_capability"


def test_gated_when_lease_scope_missing(scout, registry, lease_for):
    model = ScriptedModel([cap_action("echo.knowledge.search", query="x")])
    result = Runner(model, FakeBroker(), registry).run(scout, "x", lease_for(scout, scopes=["echo.caps.search"]))
    assert result.status == "gated" and result.proof.verdict == "UNKNOWN" and result.error is None
    env = result.steps[-1]
    assert env.verdict == "gated" and env.needs == "lease scope echo.knowledge.search" and env.how


def test_missing_or_expired_lease_is_gated_before_any_model_call(scout, registry):
    model = ScriptedModel([final_action("x")])
    result = Runner(model, FakeBroker(), registry).run(scout, "x", None)
    assert result.status == "gated" and model.calls == 0 and result.steps[0].needs == "lease"
    assert result.proof.lease_id == "0" * 32


def test_provider_unavailable_fails_without_fallback(scout, registry, lease_for):
    model = FailingModel("claude")
    result = Runner(model, FakeBroker(), registry).run(scout, "x", lease_for(scout))
    assert result.status == "failed" and result.proof.verdict == "FAIL"
    assert (
        result.error["code"] == "unavailable" and result.error["provider"] == "claude" and "no fallback was used" in result.error["message"]
    )
    assert model.calls == 1 and result.steps[0].kind == "model" and result.steps[0].ok is False


def test_malformed_action_is_denied_and_stops(scout, registry, lease_for):
    model = ScriptedModel(["I think we should list things", final_action("x")])
    result = Runner(model, FakeBroker(), registry).run(scout, "x", lease_for(scout))
    assert result.status == "failed" and result.steps[-1].verdict == "denied" and "malformed" in result.steps[-1].data["error"]
    assert model.calls == 1


def test_broker_unavailable_fails_and_names_provider(scout, registry, lease_for):
    model = ScriptedModel([cap_action("echo.caps.search", q="x")])
    broker = FakeBroker(fail=BrokerError("unavailable", "gate down", provider="echo-gate"))
    result = Runner(model, broker, registry).run(scout, "x", lease_for(scout))
    assert result.status == "failed" and result.error == {"code": "unavailable", "message": "gate down", "provider": "echo-gate"}


def test_budget_max_calls_exceeded(scout, registry, lease_for):
    model = ScriptedModel([cap_action("echo.caps.search", q="x")] * 10)
    budget = Budget(max_calls=3, max_cost_usd=0, max_wall_s=600)
    result = Runner(model, FakeBroker(), registry).run(scout, "x", lease_for(scout), budget=budget)
    assert result.status == "failed" and result.error["code"] == "budget_exceeded"
    assert result.spent.calls <= 3 and result.steps[-1].data["code"] == "budget_exceeded"


def test_cost_budget_uses_model_cost_per_call(scout, registry, lease_for):
    model = ScriptedModel([cap_action("echo.caps.search", q="x"), final_action("done")], cost_per_call_usd=0.6)
    result = Runner(model, FakeBroker(), registry).run(
        scout, "x", lease_for(scout), budget=Budget(max_calls=10, max_cost_usd=0, max_wall_s=600)
    )
    # A known paid call must be denied before billing a free lane.
    assert result.status == "failed" and result.error["code"] == "budget_exceeded"
    assert result.spent.cost_usd == 0 and model.calls == 0


@pytest.mark.parametrize("bad_cost", [float("nan"), float("inf"), -1.0, True, "0", 10**400])
def test_invalid_provider_cost_cannot_be_treated_as_free(scout, registry, lease_for, bad_cost):
    import dataclasses

    class InvalidCost(ScriptedModel):
        def complete(self, system, messages):
            return dataclasses.replace(super().complete(system, messages), cost_usd=bad_cost)

    model = InvalidCost([final_action("must not be accepted")])
    result = Runner(model, FakeBroker(), registry).run(scout, "x", lease_for(scout))
    assert result.status == "failed" and result.proof.verdict == "FAIL"
    assert result.error["code"] == "unavailable" and model.calls == 1
    assert result.spent.cost_usd == 0
    assert result.steps[-1].kind == "model" and not result.steps[-1].ok
    assert "cost_usd" not in result.steps[-1].data  # service conservatively retains the uncertain reservation


def test_failed_capability_response_cannot_become_a_successful_run(scout, registry, lease_for):
    import dataclasses

    class UnavailableBroker(FakeBroker):
        def invoke(self, *args, **kwargs):
            return dataclasses.replace(super().invoke(*args, **kwargs), ok=False, data={"http_status": 503})

    model = ScriptedModel([cap_action("echo.caps.search", q="x"), final_action("invented success")])
    result = Runner(model, UnavailableBroker(), registry).run(scout, "x", lease_for(scout))
    assert result.status == "failed" and result.proof.verdict == "FAIL" and model.calls == 1
    assert result.error["code"] == "unavailable"


def test_max_steps_without_final_is_unknown(scout, registry, lease_for):
    import dataclasses

    agent = dataclasses.replace(scout, max_steps=2)
    model = ScriptedModel([cap_action("echo.caps.search", q="x")] * 5)
    result = Runner(model, FakeBroker(), registry).run(agent, "x", lease_for(agent))
    assert result.status == "done" and result.proof.verdict == "UNKNOWN" and result.final_text is None


def test_stop_event_ends_after_current_step(scout, registry, lease_for):
    stop = threading.Event()

    class StopAfterFirst(ScriptedModel):
        def complete(self, system, messages):
            reply = super().complete(system, messages)
            stop.set()
            return reply

    model = StopAfterFirst([cap_action("echo.caps.search", q="x"), final_action("late")])
    result = Runner(model, FakeBroker(), registry).run(scout, "x", lease_for(scout), stop=stop)
    assert result.status == "done" and result.proof.verdict == "UNKNOWN" and model.calls == 1


def test_run_function_returns_proof_and_validates_prompt(scout, registry, lease_for):
    proof = run(scout, "x", lease_for(scout), model=ScriptedModel([final_action("y")]), broker=FakeBroker(), registry=registry)
    assert proof.verdict == "PASS"
    with pytest.raises(ValidationError):
        Runner(ScriptedModel([final_action("y")]), FakeBroker(), registry).run(scout, "x" * 12001, lease_for(scout))
    with pytest.raises(ValidationError):
        Runner(ScriptedModel([final_action("y")]), FakeBroker(), registry).run(scout, "x", lease_for(scout), run_id=new_id())


def test_ledger_is_append_only_jsonl(tmp_path):
    ledger = Ledger(tmp_path)
    run_id = new_id()
    for step in (1, 2):
        ledger.append(Envelope(run_id=run_id, step=step, kind="model", verdict="allowed", ok=True))
    lines = (tmp_path / "ledger" / f"{run_id}.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and all(json.loads(line)["untrusted"] is True for line in lines)
    assert [e.step for e in ledger.read(run_id)] == [1, 2]
