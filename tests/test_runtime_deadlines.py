"""Late I/O must retain its evidence without producing a successful run."""

from __future__ import annotations

import dataclasses
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from echo_adk.api import service as service_module
from echo_adk.api.service import AdkService
from echo_adk.core import deadline
from echo_adk.core.broker import GateBroker
from echo_adk.core.ledger import Ledger, MemoryStore
from echo_adk.core.models import CliModel, LoopbackModel, ProviderUnavailable
from echo_adk.core.models import RunResult as CliResult
from echo_adk.core.policy import CapabilityRegistry
from echo_adk.core.runner import Runner
from echo_adk.core.types import Budget, Proof
from tests.conftest import wait_for
from tests.fakes import FakeBroker, ScriptedModel, cap_action, final_action


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Response(io.BytesIO):
    status = 200
    headers = {}


def response(body):
    return Response(json.dumps({"status": "ok", "result": {"status_code": 200, "body": body}}).encode())


@pytest.mark.parametrize("reply", [final_action("too late"), cap_action("echo.caps.search", q="too late")])
@pytest.mark.parametrize("elapsed", [60.0, 60.01])
def test_late_model_cannot_finish_or_dispatch(reply, elapsed, scout, registry, lease_for, tmp_path):
    clock = Clock()

    class SlowModel(ScriptedModel):
        def complete(self, system, messages):
            clock.advance(elapsed)
            return super().complete(system, messages)

    model, broker = SlowModel([reply]), FakeBroker()
    memory, ledger = MemoryStore(tmp_path), Ledger(tmp_path)
    result = Runner(model, broker, registry, clock=clock, memory=memory, ledger=ledger).run(
        scout, "bounded work", lease_for(scout), budget=Budget(max_wall_s=60), memory_scope="deadline"
    )
    assert result.status == "failed" and result.error["code"] == "budget_exceeded"
    assert result.proof.verdict == "FAIL" and result.final_text is None
    assert result.spent.wall_s == elapsed and result.spent.calls == 1
    assert model.calls == 1 and broker.calls == [] and memory.read("deadline") == []
    assert [env.kind for env in result.steps] == ["model", "cap"]
    assert result.steps[0].ok and result.steps[-1].verdict == "denied"
    assert ledger.read_proof(result.run_id) == result.proof
    assert result.proof.events_sha256 == Proof.hash_events(result.steps)


def test_real_sequence_overrun_retains_actual_cost_and_capability_receipt(scout, registry, lease_for):
    clock = Clock()
    scout = dataclasses.replace(scout, lane=dataclasses.replace(scout.lane, budget=Budget(max_cost_usd=1, max_wall_s=60)))

    class TimedModel(ScriptedModel):
        def complete(self, system, messages):
            clock.advance(29.779 if self.calls == 0 else 29.429)
            return super().complete(system, messages)

    class TimedBroker(FakeBroker):
        def invoke(self, *args, **kwargs):
            # Includes catalog discovery time, as in the observed live run.
            clock.advance(20.36)
            return super().invoke(*args, **kwargs)

    model = TimedModel([cap_action("echo.caps.search", q="x"), final_action("late final")], cost_per_call_usd=0.25)
    broker = TimedBroker()
    result = Runner(model, broker, registry, clock=clock).run(scout, "x", lease_for(scout))
    assert result.status == "failed" and result.proof.verdict == "FAIL" and result.final_text is None
    assert result.spent.wall_s == pytest.approx(79.568)
    assert result.spent.calls == 3 and result.spent.cost_usd == 0.5
    assert model.calls == 2 and len(broker.calls) == 1
    assert result.steps[1].ok and result.steps[1].audit_id == "audit-1"
    assert result.proof.actions[0]["audit_id"] == "audit-1"
    assert not any(env.kind == "final" for env in result.steps)


def test_late_capability_on_last_step_is_failed_not_unknown(scout, registry, lease_for):
    clock = Clock()
    scout = dataclasses.replace(scout, max_steps=1)

    class SlowBroker(FakeBroker):
        def invoke(self, *args, **kwargs):
            clock.advance(61)
            return super().invoke(*args, **kwargs)

    model, broker = ScriptedModel([cap_action("echo.caps.search", q="x")]), SlowBroker()
    result = Runner(model, broker, registry, clock=clock).run(scout, "x", lease_for(scout), budget=Budget(max_wall_s=60))
    assert result.status == "failed" and result.proof.verdict == "FAIL"
    assert result.error["code"] == "budget_exceeded" and result.spent.calls == 2
    assert result.steps[1].ok and result.steps[1].audit_id == "audit-1"
    assert len(broker.calls) == model.calls == 1


@pytest.mark.parametrize("write", ["memory", "proof"])
def test_slow_durable_write_cannot_publish_a_pass(write, scout, registry, lease_for, tmp_path):
    clock = Clock()
    memory, ledger = MemoryStore(tmp_path), Ledger(tmp_path)
    target = memory if write == "memory" else ledger
    method = "append" if write == "memory" else "write_proof"
    original = getattr(target, method)

    def slow_write(*args, **kwargs):
        result = original(*args, **kwargs)
        clock.advance(61)
        return result

    setattr(target, method, slow_write)
    result = Runner(ScriptedModel([final_action("answer")]), FakeBroker(), registry, clock=clock, ledger=ledger, memory=memory).run(
        scout, "x", lease_for(scout), budget=Budget(max_wall_s=60), memory_scope="deadline"
    )
    assert result.status == "failed" and result.proof.verdict == "FAIL" and result.final_text is None
    assert ledger.read_proof(result.run_id) == result.proof
    assert result.proof.events_sha256 == Proof.hash_events(result.steps)
    assert len(memory.read("deadline")) == 1  # a completed append is not undone or replayed
    if write == "memory":
        assert not any(env.kind == "final" for env in result.steps)
        assert result.steps[-2].data["memory_receipt"]["record_id"] == memory.read("deadline")[0]["record_id"]


def test_live_catalog_and_dispatch_share_the_remaining_run_deadline(scout, registry, lease_for, monkeypatch):
    clock, timeouts, requests = Clock(), [], []
    monkeypatch.setattr(deadline, "monotonic", clock)
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", "synthetic-test-key")

    def opener(request, *, timeout):
        wire = json.loads(request.data)
        timeouts.append(timeout)
        requests.append(wire["capability"])
        clock.advance(1)
        if wire["capability"] == "echo.caps.detail":
            return response({"cap": {"id": "echo.caps.search", "tier": 0, "scope": "caps.read", "verb": "search"}})
        if wire["params"].get("q") == "echo.caps.search":
            return response({"caps": [{"id": "echo.caps.search", "tier": 0, "scope": "caps.read"}]})
        return response({"caps": []})

    class TimedModel(ScriptedModel):
        def complete(self, system, messages):
            clock.advance(2 if self.calls == 0 else 0)
            return super().complete(system, messages)

    broker = GateBroker(opener=opener, registry=registry, timeout_s=30)
    model = TimedModel([cap_action("echo.caps.search", q="work"), final_action("must not run")])
    result = Runner(model, broker, registry, clock=clock).run(scout, "x", lease_for(scout), budget=Budget(max_wall_s=5))
    assert timeouts == [3, 2, 1]
    assert requests == ["echo.caps.detail", "echo.caps.search", "echo.caps.search"]
    assert result.status == "failed" and result.error["code"] == "budget_exceeded" and model.calls == 1
    assert broker.timeout_s == 30 and deadline.io_timeout(30) == 30


def test_catalog_deadline_refuses_next_request_before_model(scout, lease_for, monkeypatch):
    clock, requests = Clock(), []
    monkeypatch.setattr(deadline, "monotonic", clock)
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", "synthetic-test-key")
    scout = dataclasses.replace(scout, capabilities=("echo.fixture.read",))

    def opener(request, *, timeout):
        requests.append(json.loads(request.data)["capability"])
        assert timeout == 5
        clock.advance(6)
        return response({"cap": {"id": "echo.fixture.read", "tier": 0, "scope": "caps.read", "verb": "read"}})

    model = ScriptedModel([final_action("must not run")])
    broker = GateBroker(opener=opener, registry=CapabilityRegistry())
    result = Runner(model, broker, clock=clock).run(scout, "x", lease_for(scout), budget=Budget(max_wall_s=5))
    assert requests == ["echo.caps.detail"] and model.calls == 0
    assert result.status == "failed" and result.error["code"] == "budget_exceeded"
    assert result.proof.verdict == "FAIL"


@pytest.mark.parametrize("provider", ["loopback", "cli"])
def test_builtin_model_timeouts_are_narrowed_without_mutating_provider(provider, monkeypatch):
    clock, seen = Clock(), []
    monkeypatch.setattr(deadline, "monotonic", clock)
    if provider == "loopback":

        def opener(request, *, timeout):
            seen.append(timeout)
            return Response(json.dumps({"choices": [{"message": {"content": final_action("ok")}}], "model": "qwen"}).encode())

        model = LoopbackModel(timeout_s=120, opener=opener)
    else:

        def cli_runner(*args, **kwargs):
            seen.append(kwargs["timeout_s"])
            return CliResult(0, json.dumps({"result": final_action("ok")}), "", False)

        model = CliModel("claude", timeout_s=120, runner=cli_runner)
    with deadline.limit_io(5):
        clock.advance(2)
        model.complete("system", [{"role": "user", "content": "work"}])
        clock.advance(3)
        with pytest.raises(ProviderUnavailable):
            model.complete("system", [{"role": "user", "content": "expired"}])
    assert seen == [3] and model.timeout_s == 120
    assert deadline.io_timeout(120) == 120


def test_request_local_deadlines_do_not_leak_or_extend_other_runs(monkeypatch):
    monkeypatch.setattr(deadline, "monotonic", lambda: 0)
    barrier = threading.Barrier(2)

    def check(seconds):
        with deadline.limit_io(seconds):
            barrier.wait(timeout=5)
            with deadline.limit_io(100):
                result = deadline.io_timeout(120)
        assert deadline.io_timeout(120) == 120
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(check, [3, 9])) == [3, 9]


@pytest.mark.parametrize("unknown", [False, True])
def test_deadline_settlement_keeps_actual_or_uncertain_inflight_cost(agents_copy, tmp_path, monkeypatch, unknown):
    clock = Clock()

    class TimedModel(ScriptedModel):
        def complete(self, system, messages):
            reply = super().complete(system, messages)
            clock.advance(61)
            if unknown:
                raise ProviderUnavailable("claude", "in-flight request timed out")
            return reply

    model = TimedModel([final_action("late")], cost_per_call_usd=0.25)
    monkeypatch.setattr(service_module, "Runner", lambda *args, **kwargs: Runner(*args, clock=clock, **kwargs))
    service = AdkService(agents_copy, tmp_path / "state", broker=FakeBroker(), model_factory=lambda _: model, start_scheduler=False)
    try:
        admitted = service.create_run({"agent": "paid-steward", "prompt": "work", "budget": {"max_cost_usd": 1, "max_wall_s": 60}})
        run_id = admitted["run_id"]
        run = wait_for(lambda: (lambda doc: doc if doc["status"] == "failed" else None)(service.get_run(run_id)))
        assert run["error"]["code"] == "budget_exceeded" and run["proof"]["verdict"] == "FAIL"
        assert run["spent"]["wall_s"] == 61 and model.calls == 1
        receipt = service.run_budget(run_id)
        assert receipt["uncertain_cost"] is unknown
        assert receipt["charged_usd"] == (1 if unknown else 0.25)
        assert receipt["reserved_usd"] == 0 and service.budget_health()["reserved_usd"] == 0
    finally:
        service.close()
