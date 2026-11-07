"""Shared governance state for one ADK run: policy, Lease, step counter, envelopes, Proof."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Any, Optional

from ..core.broker import Broker, BrokerError, GateBroker
from ..core.ledger import Ledger
from ..core.policy import ALLOWED, CapabilityRegistry, Decision, Policy
from ..core.types import Capability, EchoAgent, Envelope, Lease, Proof, Spent, new_id


class GovernedSession:
    def __init__(
        self,
        agent: EchoAgent,
        *,
        broker: Broker,
        registry: Optional[CapabilityRegistry] = None,
        lease: Optional[Lease] = None,
        run_id: Optional[str] = None,
        ledger: Optional[Ledger] = None,
        clock=time.monotonic,
    ) -> None:
        self.agent = agent
        self.registry = (
            registry if registry is not None else broker.registry if isinstance(broker, GateBroker) else CapabilityRegistry.builtin()
        )
        if isinstance(broker, GateBroker):
            broker.registry = self.registry
        self.policy = Policy(self.registry, agent.capabilities, agent.lane.budget)
        self.run_id = run_id or (lease.run_id if lease else new_id())
        budget = agent.lane.budget
        self.lease = lease or Lease.mint(self.run_id, agent.capabilities, ttl_s=budget.max_wall_s, max_uses=budget.max_calls)
        self.broker = broker
        if isinstance(broker, GateBroker) and not self.lease.expired() and not self.lease.exhausted():
            broker.hydrate(tuple(cap_id for cap_id in agent.capabilities if self.lease.permits(cap_id)))
        self.ledger = ledger
        self._clock = clock
        self._t0 = clock()
        self._lock = threading.Lock()
        self._step = 0
        self.envelopes: list[Envelope] = []

    def next_step(self) -> int:
        with self._lock:
            self._step += 1
            return self._step

    def spent(self) -> Spent:
        calls = sum(1 for e in self.envelopes if e.kind in ("cap", "model") and e.verdict == ALLOWED)
        return Spent(calls, 0.0, self._clock() - self._t0)

    def record(self, env: Envelope) -> Envelope:
        with self._lock:
            self.envelopes.append(env)
        if self.ledger is not None:
            self.ledger.append(env)
        return env

    def refusal(
        self, decision: Decision, capability: Optional[str], *, step: Optional[int] = None, extra: Optional[dict[str, Any]] = None
    ) -> Envelope:
        return self.record(
            Envelope(
                run_id=self.run_id,
                step=step or self.next_step(),
                kind="cap",
                verdict=decision.verdict if decision.verdict != ALLOWED else "denied",
                ok=False,
                capability=capability,
                tier=decision.tier,
                needs=decision.needs,
                how=decision.how,
                data={"error": decision.reason, "code": decision.code or decision.verdict, **(extra or {})},
            )
        )

    def evaluate(self, cap_id: str, args: Mapping[str, Any]) -> Decision:
        return self.policy.evaluate(cap_id, dict(args), self.lease, self.spent())

    def invoke(self, cap: Capability, args: Mapping[str, Any]) -> Envelope:
        """Policy + broker for one capability call; always returns an Envelope (never raises)."""
        step = self.next_step()
        decision = self.evaluate(cap.id, args)
        if decision.verdict != ALLOWED:
            return self.refusal(decision, cap.id, step=step)
        try:
            env = self.broker.invoke(
                cap.id, dict(args), self.lease, run_id=self.run_id, step=step, tier=cap.tier, context={"source": "echo-adk-google-adk"}
            )
        except BrokerError as exc:
            env = Envelope(
                run_id=self.run_id,
                step=step,
                kind="cap",
                verdict=ALLOWED,
                ok=False,
                capability=cap.id,
                tier=cap.tier,
                data={"error": str(exc), "code": exc.code, "provider": exc.provider},
            )
        return self.record(env)

    def record_model(self, answered: Mapping[str, Any], *, ok: bool = True, error: Optional[str] = None) -> Envelope:
        data: dict[str, Any] = {"answered": dict(answered)}
        if error:
            data["error"] = error
        return self.record(Envelope(run_id=self.run_id, step=self.next_step(), kind="model", verdict=ALLOWED, ok=ok, data=data))

    def proof(self, verdict: Optional[str] = None) -> Proof:
        if verdict is None:
            failed = any(e.verdict == "denied" or (e.verdict == ALLOWED and not e.ok) for e in self.envelopes)
            final = any(e.kind == "final" for e in self.envelopes)
            verdict = "FAIL" if failed else ("PASS" if final else "UNKNOWN")
        proof = Proof.from_envelopes(self.run_id, self.lease.lease_id, self.envelopes, verdict)
        if self.ledger is not None:
            self.ledger.write_proof(proof)
        return proof
