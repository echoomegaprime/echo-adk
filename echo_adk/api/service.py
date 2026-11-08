"""The service behind the frozen ``echo-adk-api`` contract (docs/CONTRACT.md).

Transport-agnostic: :class:`AdkService` methods take parsed bodies and return the contract's JSON
shapes; :class:`ApiError` carries the HTTP status and error code. ``echo_adk.api.server`` maps HTTP to
these methods, ``tests/test_contract.py`` drives them over real loopback HTTP.
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from .. import PROFILE, SERVICE_NAME, __version__
from ..agents import load_agents_dir
from ..bots.manager import BotError, BotManager
from ..core.broker import Broker, BrokerError, GateBroker
from ..core.ledger import Ledger, MemoryStore
from ..core.log import get_logger
from ..core.models import Model, ProviderUnavailable, build_model
from ..core.policy import CapabilityRegistry
from ..core.runner import STATUS_DONE, STATUS_FAILED, STATUS_QUEUED, STATUS_RUNNING, Runner
from ..core.skills import SkillLibrary, SkillValidationError
from ..core.types import (
    API_AGENT_NAME,
    BUDGET_CAPS,
    CAP_ID,
    MAX_PROMPT_CHARS,
    Budget,
    EchoAgent,
    Envelope,
    Lease,
    Proof,
    Spent,
    ValidationError,
    is_id,
    new_id,
    now_iso,
)
from .budgets import BudgetAdmissionError, money_micros
from .store import Store

log = get_logger("api")

DEFAULT_DAILY_CAP_USD = 200.0
MAX_LIST_RUNS = 50
MAX_LIST_MESSAGES = 200


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        body = {"error": self.code, "message": self.message}
        body.update(self.extra)
        return body


def _daily_cap() -> float:
    raw = os.environ.get("ECHO_ADK_DAILY_CAP_USD", "").strip()
    if not raw:
        return DEFAULT_DAILY_CAP_USD
    try:
        value = float(raw)
        money_micros(value)
    except ValueError:
        raise ValidationError("ECHO_ADK_DAILY_CAP_USD must be finite and nonnegative") from None
    return min(value, 10_000.0)


class AdkService:
    def __init__(
        self,
        agents_dir: str | Path,
        state_dir: str | Path,
        *,
        model_factory: Optional[Callable[[str], Model]] = None,
        broker: Optional[Broker] = None,
        registry: Optional[CapabilityRegistry] = None,
        workers: int = 4,
        daily_cap_usd: Optional[float] = None,
        start_scheduler: bool = True,
        scheduler_tick_s: float = 2.0,
        skills_dir: Optional[str | Path] = None,
    ) -> None:
        self.agents_dir = Path(agents_dir)
        self.state_dir = Path(state_dir)
        self.agents: dict[str, EchoAgent] = load_agents_dir(self.agents_dir)
        self.daily_cap_usd = _daily_cap() if daily_cap_usd is None else float(daily_cap_usd)
        try:
            money_micros(self.daily_cap_usd)
        except ValueError:
            raise ValidationError("daily_cap_usd must be finite and nonnegative") from None
        self.store = Store(self.state_dir)
        self.ledger = Ledger(self.state_dir)
        self.memory = MemoryStore(self.state_dir)
        skills_root = skills_dir or os.environ.get("ECHO_ADK_SKILLS_DIR")
        self.skill_library = SkillLibrary(skills_root) if skills_root else None
        self.registry = registry if registry is not None else self._default_registry()
        self.model_factory = model_factory or (lambda model_id: build_model(model_id))
        self._broker = broker
        if isinstance(broker, GateBroker):
            broker.registry = self.registry
        self._executor = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="echo-adk-run")
        self._stops: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self.bots = BotManager(self, tick_s=scheduler_tick_s)
        from .workspace import BotWorkspace

        self.workspace = BotWorkspace(self)
        self._recovery = self._recover_incomplete_runs()
        if start_scheduler:
            self.bots.start()
            self.workspace.start()
        log.info("service_ready", agents=len(self.agents), state_dir=str(self.state_dir), daily_cap_usd=self.daily_cap_usd)

    def _default_registry(self) -> CapabilityRegistry:
        registry = CapabilityRegistry.builtin()
        for candidate in (os.environ.get("ECHO_ADK_CAPS_FILE"), str(self.state_dir / "capabilities.json")):
            if candidate and Path(candidate).is_file():
                count = registry.load_json(candidate)
                log.info("capabilities_loaded", path=candidate, count=count)
        return registry

    @property
    def broker(self) -> Broker:
        if self._broker is None:
            self._broker = GateBroker(registry=self.registry)
        return self._broker

    def close(self) -> None:
        self.workspace.stop()
        self.bots.stop()
        for event in list(self._stops.values()):
            event.set()
        self._executor.shutdown(wait=True, cancel_futures=True)
        self.workspace.close()
        # Shutdown cancels executor futures, but paused inbox jobs remain durable. They are
        # never silently replayed: recovery pauses any affected bot before its scheduler starts.
        for doc in self.store.unfinished_runs():
            if doc["status"] == STATUS_QUEUED and (not doc.get("bot_id") or doc.get("group_id")):
                self.refuse_run(doc["run_id"], "unavailable", "Service stopped before this run began; no work was executed")
        self.store.close()

    # ------------------------------------------------------------------ health / agents
    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "service": SERVICE_NAME,
            "profile": PROFILE,
            "version": __version__,
            "agents": len(self.agents),
            "bots": len(self.store.list_bots()),
            "runs_active": self.store.count_runs(statuses=(STATUS_QUEUED, STATUS_RUNNING)),
        }

    def budget_health(self, bot_id: Optional[str] = None) -> dict[str, Any]:
        """Additive diagnostics for v2; the frozen v1 health shape remains unchanged."""
        limits = self._budget_limits(bot_id)
        scope = f"bot:{bot_id}" if bot_id else "service"
        status = self.store.budget_status(scope, limits[scope])
        if bot_id is None:
            status["recovery"] = dict(self._recovery)
        return status

    def validate_plugins(self, names: list[str]) -> None:
        """Explicit plugin selection must have executable installed guidance."""
        if not names:
            return
        if self.skill_library is None:
            raise ApiError(503, "unavailable", "Install the requested skills and configure ECHO_ADK_SKILLS_DIR before selecting plugins")
        try:
            self.skill_library.context(names)
        except SkillValidationError as exc:
            raise ApiError(400, "invalid", str(exc)) from None

    def run_budget(self, run_id: str) -> dict[str, Any]:
        """A v2 accounting receipt; keep GET /runs/{id}'s frozen v1 keys intact."""
        if not is_id(run_id) or self.store.get_run(run_id) is None:
            raise ApiError(404, "not_found", "Run not found")
        return self.store.budget_accounting(run_id)

    def list_agents(self) -> dict[str, Any]:
        return {"agents": [self.agents[name].to_dict() for name in sorted(self.agents)]}

    def get_agent(self, name: str) -> EchoAgent:
        agent = self.agents.get(name) if isinstance(name, str) else None
        if agent is None:
            raise ApiError(404, "not_found", f"agent {name!r} not found")
        return agent

    def agent_dict(self, name: str) -> dict[str, Any]:
        return self.get_agent(name).to_dict()

    # ------------------------------------------------------------------ agent definitions (POST/PUT/DELETE)
    def _agent_from_body(self, body: Any, *, expect_name: Optional[str] = None, previous: Optional[EchoAgent] = None) -> EchoAgent:
        """Validate a definition exactly like a file definition, then clamp the lane budget and stamp ``origin``."""
        if not isinstance(body, Mapping):
            raise ApiError(400, "invalid", "body must be an object")
        if "source" in body:
            raise ApiError(400, "invalid", "source is assigned by the service; omit it")
        name = body.get("name")
        if not isinstance(name, str) or not API_AGENT_NAME.match(name):
            raise ApiError(400, "invalid", "name must match ^[a-z0-9][a-z0-9-]{1,62}$")
        if expect_name is not None and name != expect_name:
            raise ApiError(400, "invalid", f"body name {name!r} does not match the path {expect_name!r}")
        data = dict(body)
        lane = data.get("lane")
        if isinstance(lane, Mapping) and isinstance(lane.get("budget"), Mapping):
            try:
                data["lane"] = {**lane, "budget": Budget.from_dict(lane["budget"]).to_dict()}
            except ValidationError as exc:
                raise ApiError(400, "invalid", f"lane.budget: {exc}")
        origin_in = data.get("origin")
        if origin_in is not None and not isinstance(origin_in, Mapping):
            raise ApiError(400, "invalid", "origin must be an object")
        origin: dict[str, Any] = {"by": "owner"}
        if origin_in:
            if set(origin_in) - {"by", "prompt_sha256"}:
                raise ApiError(400, "invalid", "origin accepts by and prompt_sha256 only; the service stamps created_at")
            origin.update({k: v for k, v in origin_in.items() if v is not None})
        if previous is not None and previous.origin is not None:
            origin["created_at"] = previous.origin.get("created_at", now_iso())
            origin["updated_at"] = now_iso()
        else:
            origin["created_at"] = now_iso()
        data["origin"] = origin
        try:
            agent = EchoAgent.from_dict(data, source=f"{name}.json")
        except ValidationError as exc:
            raise ApiError(400, "invalid", str(exc))
        unknown = [cap for cap in agent.capabilities if cap not in self.registry]
        if unknown and isinstance(self.broker, GateBroker):
            if len(unknown) > 64:
                raise ApiError(400, "invalid", "At most 64 new capability IDs may be resolved per definition")
            try:
                for cap_id in unknown:
                    self.registry.register(self.broker.resolve_capability(cap_id))
            except BrokerError as exc:
                raise ApiError(503 if exc.code == "unavailable" else 400, exc.code, str(exc), provider="echo-gate") from None
            unknown = [cap for cap in agent.capabilities if cap not in self.registry]
        if unknown:
            raise ApiError(400, "invalid", f"capabilities not in the registry (default-deny): {unknown}")
        if self.skill_library is not None:
            self.validate_plugins(list(agent.skills))
        return agent

    def _write_agent_file(self, agent: EchoAgent) -> None:
        payload = agent.to_dict()
        payload.pop("source", None)
        path = self.agents_dir / f"{agent.name}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def create_agent(self, body: Any) -> dict[str, Any]:
        agent = self._agent_from_body(body)
        with self._lock:
            if agent.name in self.agents or (self.agents_dir / f"{agent.name}.json").exists():
                raise ApiError(409, "invalid", f"agent {agent.name!r} already exists")
            self._write_agent_file(agent)
            self.agents[agent.name] = agent
        log.info("agent_created", agent=agent.name, by=(agent.origin or {}).get("by"))
        return agent.to_dict()

    def replace_agent(self, name: str, body: Any) -> dict[str, Any]:
        with self._lock:
            previous = self.get_agent(name)
            agent = self._agent_from_body(body, expect_name=name, previous=previous)
            self._write_agent_file(agent)
            self.agents[agent.name] = agent
        log.info("agent_replaced", agent=agent.name)
        return agent.to_dict()

    def delete_agent(self, name: str) -> dict[str, Any]:
        with self._lock:
            agent = self.get_agent(name)
            referencing = [b["bot_id"] for b in self.store.list_bots() if b.get("agent") == name]
            if referencing:
                raise ApiError(409, "invalid", f"agent {name!r} is referenced by bot(s) {referencing}; delete them first")
            path = self.agents_dir / f"{agent.name}.json"
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            self.agents.pop(name, None)
        log.info("agent_deleted", agent=name)
        return {"name": name, "deleted": True}

    # ------------------------------------------------------------------ runs
    def _parse_budget(self, agent: EchoAgent, given: Any) -> Budget:
        if given is None:
            return agent.lane.budget
        try:
            return Budget.from_dict(given, defaults=agent.lane.budget).clamp_to(agent.lane.budget)
        except ValidationError as exc:
            raise ApiError(400, "invalid", f"budget: {exc}")

    def _parse_lease(self, agent: EchoAgent, given: Any, run_id: str, budget: Budget) -> Lease:
        if given is None:
            return Lease.mint(run_id, agent.capabilities, ttl_s=budget.max_wall_s, max_uses=budget.max_calls)
        if not isinstance(given, Mapping):
            raise ApiError(400, "invalid", "lease must be an object")
        extra = set(given) - {"scopes", "ttl_s", "max_uses"}
        if extra:
            raise ApiError(400, "invalid", f"lease: unknown field(s) {sorted(extra)}")
        scopes = given.get("scopes")
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            raise ApiError(400, "invalid", "lease.scopes must be a list of exact capability ids; [] disables capability use")
        for scope in scopes:
            if "*" in scope or not CAP_ID.match(scope):
                raise ApiError(400, "invalid", f"lease.scopes entry {scope!r} must be an exact capability id (no wildcards)")
            if scope not in agent.capabilities:
                raise ApiError(400, "invalid", f"lease.scopes entry {scope!r} is outside the agent allowlist")
        ttl = given.get("ttl_s", budget.max_wall_s)
        uses = given.get("max_uses", budget.max_calls)
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= int(BUDGET_CAPS["max_wall_s"]):
            raise ApiError(400, "invalid", "lease.ttl_s must be an integer between 1 and 4800")
        if isinstance(uses, bool) or not isinstance(uses, int) or not 1 <= uses <= int(BUDGET_CAPS["max_calls"]):
            raise ApiError(400, "invalid", "lease.max_uses must be an integer between 1 and 120")
        return Lease.mint(run_id, scopes, ttl_s=min(ttl, budget.max_wall_s), max_uses=uses)

    @staticmethod
    def _budget_error(exc: BudgetAdmissionError) -> ApiError:
        return ApiError(
            429,
            "budget_exceeded",
            str(exc),
            scope="service" if exc.scope == "service" else "bot",
            daily_cap_usd=exc.cap_usd,
            spent_today_usd=exc.spent_usd,
            reserved_usd=exc.reserved_usd,
            requested_usd=exc.requested_usd,
        )

    def _budget_limits(self, bot_id: Optional[str] = None) -> dict[str, float]:
        limits = {"service": self.daily_cap_usd}
        if bot_id:
            bot = self.store.get_bot(bot_id)
            if bot is None:
                raise ApiError(404, "not_found", "Bot no longer exists")
            limits[f"bot:{bot_id}"] = float(bot["daily_cost_cap_usd"])
        return limits

    def create_run(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, Mapping):
            raise ApiError(400, "invalid", "body must be an object")
        extra = set(body) - {"agent", "prompt", "budget", "lease"}
        if extra:
            raise ApiError(400, "invalid", f"unknown field(s) {sorted(extra)}")
        agent = self.get_agent(body.get("agent")) if isinstance(body.get("agent"), str) else None
        if agent is None:
            raise ApiError(400, "invalid", "agent must be a string")
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ApiError(400, "invalid", "prompt must be a non-empty string")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ApiError(400, "invalid", f"prompt is longer than {MAX_PROMPT_CHARS} characters")
        budget = self._parse_budget(agent, body.get("budget"))
        run_id = new_id()
        lease = self._parse_lease(agent, body.get("lease"), run_id, budget)
        self._store_new_run(run_id, agent, prompt, budget, lease, memory_scope=f"agent:{agent.name}")
        self.submit_run(run_id)
        return {"run_id": run_id, "status": STATUS_QUEUED}

    def _store_new_run(
        self,
        run_id: str,
        agent: EchoAgent,
        prompt: str,
        budget: Budget,
        lease: Lease,
        *,
        memory_scope: str,
        bot_id: Optional[str] = None,
        message_id: Optional[str] = None,
        group_id: Optional[str] = None,
        agent_override: Optional[dict[str, Any]] = None,
        bot_lease_template: Optional[dict[str, Any]] = None,
    ) -> None:
        doc: dict[str, Any] = {
            "run_id": run_id,
            "agent": agent.name,
            "status": STATUS_QUEUED,
            "started_at": now_iso(),
            "finished_at": None,
            "steps": [],
            "proof": None,
            "error": None,
            "budget": budget.to_dict(),
            "spent": Spent().to_dict(),
            "prompt": prompt,
            "lease": lease.to_dict(),
            "memory_scope": memory_scope,
            "bot_id": bot_id,
            "message_id": message_id,
            "group_id": group_id,
            "agent_override": agent_override,
            "final_text": None,
            "stop_requested": False,
        }
        if bot_lease_template is not None:
            doc["bot_lease_template"] = {
                "scopes": list(bot_lease_template["scopes"]),
                "ttl_s": bot_lease_template["ttl_s"],
                "max_uses": bot_lease_template["max_uses"],
            }
        try:
            with self.store.transaction():
                self.store.admit_run(doc, self._budget_limits(bot_id))
        except BudgetAdmissionError as exc:
            raise self._budget_error(exc) from None

    def queue_bot_run(
        self,
        bot: Mapping[str, Any],
        agent: EchoAgent,
        prompt: str,
        *,
        message_id: Optional[str],
        group_id: Optional[str] = None,
    ) -> str:
        """Create a queued run for a bot turn using the bot's own Lease template. Not submitted."""
        run_id = new_id()
        template = bot["lease"]
        budget = Budget(
            min(template["max_uses"], agent.lane.budget.max_calls),
            min(agent.lane.budget.max_cost_usd, float(bot["daily_cost_cap_usd"])),
            min(template["ttl_s"], agent.lane.budget.max_wall_s),
        )
        lease = Lease.mint(run_id, template["scopes"], ttl_s=template["ttl_s"], max_uses=template["max_uses"])
        self._store_new_run(
            run_id,
            agent,
            prompt,
            budget,
            lease,
            memory_scope=f"bot:{bot['bot_id']}",
            bot_id=bot["bot_id"],
            message_id=message_id,
            group_id=group_id,
            agent_override={"instruction": agent.instruction, "skills": list(agent.skills)},
            bot_lease_template=template,
        )
        return run_id

    def submit_run(self, run_id: str) -> None:
        try:
            self._executor.submit(self._guarded_execute, run_id)
        except RuntimeError:
            self.refuse_run(run_id, "unavailable", "Service is stopping; no work was executed")
            raise ApiError(503, "unavailable", "Service is stopping; no work was executed") from None

    def submit_group(self, group_id: str) -> None:
        try:
            self._executor.submit(self._guarded_group, group_id)
        except RuntimeError:
            self._fail_group(group_id, "Service is stopping; unstarted group turns were cancelled")
            raise ApiError(503, "unavailable", "Service is stopping; group was not started") from None

    def _fail_group(self, group_id: str, message: str) -> None:
        group = self.store.get_group(group_id)
        if group is None:
            return
        for run_id in group.get("run_ids", []):
            doc = self.store.get_run(run_id)
            if doc is not None and doc["status"] in (STATUS_QUEUED, STATUS_RUNNING):
                self._finish(run_id, STATUS_FAILED, error={"code": "unavailable", "message": message})
        group["status"] = STATUS_FAILED
        group["summary"] = message
        self.store.put_group(group)
        self.bots.release_group(group_id)

    def _guarded_group(self, group_id: str) -> None:
        try:
            self.bots.run_group(group_id)
        except Exception:
            log.error("group_crashed", group_id=group_id, exc_info=True)
            self._fail_group(group_id, "Group execution was interrupted; no automatic replay was made")

    def _guarded_execute(self, run_id: str) -> None:
        try:
            self.execute_run(run_id)
        except Exception as exc:  # the run record must never be left running
            log.error("run_crashed", run_id=run_id, exc_info=True)
            self._finish(run_id, STATUS_FAILED, error={"code": "unavailable", "message": f"internal error: {type(exc).__name__}"})

    def refuse_run(self, run_id: str, code: str, message: str) -> None:
        """Fail a queued run before any model call (daily cap, service cap)."""
        self._finish(run_id, STATUS_FAILED, error={"code": code, "message": message})

    def _finish(
        self,
        run_id: str,
        status: str,
        *,
        error: Optional[dict[str, Any]] = None,
        steps: Optional[list[Envelope]] = None,
        proof: Optional[Proof] = None,
        spent: Optional[Spent] = None,
        final_text: Optional[str] = None,
        uncertain_cost: Optional[bool] = None,
    ) -> Optional[dict[str, Any]]:
        with self._lock, self.store.transaction():
            doc = self.store.get_run(run_id)
            if doc is None:
                return None
            if doc.get("finished_at"):
                return doc  # settlement and delivery are terminal, even if a worker calls twice
            if uncertain_cost is None:
                uncertain_cost = spent is None and bool(doc.get("execution_started"))
            if proof is None:
                lease_id = doc.get("lease", {}).get("lease_id") or "0" * 32
                envelopes = steps if steps is not None else [Envelope.from_dict(s) for s in doc.get("steps", [])]
                verdict = "FAIL" if status == STATUS_FAILED else "UNKNOWN"
                proof = Proof.from_envelopes(run_id, lease_id, envelopes, verdict)
                self.ledger.write_proof(proof)
            doc["status"] = status
            doc["finished_at"] = now_iso()
            doc["error"] = error
            doc["proof"] = proof.to_dict()
            if steps is not None:
                doc["steps"] = [s.to_dict() for s in steps]
            if spent is not None:
                doc["spent"] = spent.to_dict()
            if final_text is not None:
                doc["final_text"] = final_text
            measured = spent.cost_usd if spent is not None else float(doc.get("spent", {}).get("cost_usd", 0.0))
            self.store.settle_budget(doc, measured, uncertain=uncertain_cost)
            self._stops.pop(run_id, None)
        if doc.get("bot_id") and not doc.get("group_id"):
            self.bots.on_run_finished(doc)
        return doc

    def execute_run(self, run_id: str, *, prompt_override: Optional[str] = None) -> dict[str, Any]:
        """Run synchronously on the calling thread (worker pool or group turn). Returns the run doc."""
        admission_error = None
        cancelled_before_start = False
        with self._lock, self.store.transaction():
            doc = self.store.get_run(run_id)
            if doc is None:
                raise ApiError(404, "not_found", f"run {run_id} not found")
            if doc["status"] != STATUS_QUEUED:
                return doc
            if doc.get("stop_requested"):
                cancelled_before_start = True
            else:
                try:
                    doc["budget_accounting"] = self.store.reserve_budget(
                        run_id, doc["budget"]["max_cost_usd"], self._budget_limits(doc.get("bot_id"))
                    )
                except BudgetAdmissionError as exc:
                    admission_error = self._budget_error(exc)
                except ApiError as exc:
                    admission_error = exc
            if admission_error is None and not cancelled_before_start:
                # A bot authorizes a per-turn template; waiting in a paused inbox must not spend
                # its TTL. Use the queued immutable snapshot, never a later widened bot template.
                # Direct /runs leases and older bot rows without a snapshot keep their expiry.
                template = doc.get("bot_lease_template") if doc.get("bot_id") else None
                if template is not None:
                    doc["lease"] = Lease.mint(
                        run_id,
                        list(template["scopes"]),
                        ttl_s=template["ttl_s"],
                        max_uses=template["max_uses"],
                    ).to_dict()
                stop = threading.Event()
                self._stops[run_id] = stop
                doc["status"] = STATUS_RUNNING
                self.store.put_run(doc)
        if cancelled_before_start:
            return self._finish(run_id, STATUS_DONE) or doc
        if admission_error is not None:
            return (
                self._finish(
                    run_id,
                    STATUS_FAILED,
                    error={"code": admission_error.code, "message": admission_error.message},
                    uncertain_cost=False,
                )
                or doc
            )
        agent = self.agents.get(doc["agent"])
        if agent is None:
            return self._finish(run_id, STATUS_FAILED, error={"code": "not_found", "message": "agent no longer exists"}) or doc
        override = doc.get("agent_override") or {}
        if override.get("instruction"):
            agent = dataclasses.replace(agent, instruction=override["instruction"])
        if isinstance(override.get("skills"), list):
            agent = dataclasses.replace(agent, skills=tuple(override["skills"]))
        try:
            model = self.model_factory(agent.lane.model)
        except ProviderUnavailable as exc:
            return self._finish(run_id, STATUS_FAILED, error={"code": "unavailable", "message": str(exc), "provider": exc.provider}) or doc
        except ValidationError as exc:
            return self._finish(run_id, STATUS_FAILED, error={"code": "invalid", "message": str(exc)}) or doc
        lease = Lease.from_dict(doc["lease"])
        budget = Budget.from_dict(doc["budget"])
        runner = Runner(
            model,
            self.broker,
            self.registry,
            ledger=self.ledger,
            memory=self.memory,
            memory_notes=lambda scope: self.workspace.memory.read(scope[4:]) if scope.startswith("bot:") else None,
            skill_library=self.skill_library,
        )
        # Persist before invoking anything. If the process disappears after this point, recovery
        # treats the cost as unknown and retains the ceiling rather than inventing a zero charge.
        self.store.update_run(run_id, execution_started=True)

        def on_step(env: Envelope) -> None:
            with self._lock:
                current = self.store.get_run(run_id)
                if current is not None:
                    current["steps"].append(env.to_dict())
                    self.store.put_run(current)

        try:
            result = runner.run(
                agent,
                prompt_override or doc["prompt"],
                lease,
                run_id=run_id,
                budget=budget,
                stop=stop,
                memory_scope=doc.get("memory_scope"),
                on_step=on_step,
                context={"bot_id": doc.get("bot_id"), "group_id": doc.get("group_id")},
            )
        except ValidationError as exc:
            return self._finish(run_id, STATUS_FAILED, error={"code": "invalid", "message": str(exc)}) or doc
        finished = self._finish(
            run_id,
            result.status,
            error=result.error,
            steps=result.steps,
            proof=result.proof,
            spent=result.spent,
            final_text=result.final_text,
            uncertain_cost=any(env.kind == "model" and not env.ok and "cost_usd" not in env.data for env in result.steps),
        )
        return finished or doc

    def _public_run(self, doc: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "run_id": doc["run_id"],
            "agent": doc["agent"],
            "status": doc["status"],
            "started_at": doc["started_at"],
            "steps": list(doc.get("steps", [])),
            "budget": dict(doc["budget"]),
            "spent": dict(doc["spent"]),
        }
        if doc.get("finished_at"):
            out["finished_at"] = doc["finished_at"]
        if doc.get("proof"):
            out["proof"] = doc["proof"]
        if doc.get("error"):
            out["error"] = doc["error"]
        return out

    def get_run(self, run_id: str) -> dict[str, Any]:
        doc = self.store.get_run(run_id) if is_id(run_id) else None
        if doc is None:
            raise ApiError(404, "not_found", f"run {run_id} not found")
        return self._public_run(doc)

    def stop_run(self, run_id: str) -> dict[str, Any]:
        queued = False
        with self._lock:
            doc = self.store.get_run(run_id) if is_id(run_id) else None
            if doc is None:
                raise ApiError(404, "not_found", f"run {run_id} not found")
            if doc["status"] == STATUS_QUEUED:
                doc["stop_requested"] = True
                self.store.put_run(doc)
                queued = True
            elif doc["status"] == STATUS_RUNNING:
                event = self._stops.get(run_id)
                if event is not None:
                    event.set()
                doc["stop_requested"] = True
                self.store.put_run(doc)
        if queued:
            doc = self._finish(run_id, STATUS_DONE) or doc
        return {"run_id": run_id, "status": doc["status"]}

    def list_runs(self, agent: Optional[str], limit: Any) -> dict[str, Any]:
        count = _limit(limit, MAX_LIST_RUNS)
        if agent is not None and agent not in self.agents:
            return {"runs": []}
        rows = self.store.list_runs(agent, count)
        return {"runs": [{"run_id": r["run_id"], "agent": r["agent"], "status": r["status"], "started_at": r["started_at"]} for r in rows]}

    def forget_run(self, run_id: str) -> None:
        """Delete a run's ledger and proof files (used when a bot is deleted)."""
        with self.store.transaction():
            doc = self.store.get_run(run_id)
            if doc is not None:
                if doc["status"] == STATUS_RUNNING:
                    raise ApiError(409, "invalid", "Stop the active run and wait for it to finish before deleting its bot")
                if not doc.get("finished_at"):
                    self.store.settle_budget(doc, 0.0)
                self.store.forget_budget(run_id)
        for path in (self.state_dir / "ledger" / f"{run_id}.jsonl", self.state_dir / "proofs" / f"{run_id}.json"):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self._stops.pop(run_id, None)

    def _recover_incomplete_runs(self) -> dict[str, int]:
        """Fail ambiguous work closed, preserve paused inbox receipts, and never replay a run."""
        counts = {"interrupted": 0, "cancelled": 0, "queued_preserved": 0}
        affected_bots: set[str] = set()
        for doc in self.store.unfinished_runs():
            if doc["status"] == STATUS_RUNNING:
                if doc.get("bot_id"):
                    affected_bots.add(doc["bot_id"])
                self._finish(
                    doc["run_id"],
                    STATUS_FAILED,
                    error={"code": "unavailable", "message": "Service restarted during this run; outcome and cost are uncertain"},
                    uncertain_cost=True,
                )
                counts["interrupted"] += 1
            elif doc.get("bot_id") and not doc.get("group_id"):
                counts["queued_preserved"] += 1
            else:
                self.refuse_run(doc["run_id"], "unavailable", "Service restarted before this run began; no automatic replay was made")
                counts["cancelled"] += 1
        for group in self.store.unfinished_groups():
            self._fail_group(group["group_id"], "Service restarted; unfinished group turns were not replayed")
        for bot_id in affected_bots:
            bot = self.store.get_bot(bot_id)
            if bot is not None:
                bot["status"] = "paused"
                bot["active_run_id"] = None
                bot["active_group_id"] = None
                bot["last_error"] = "Service restarted; pending inbox work is preserved. Resume after reviewing interrupted runs."
                self.store.put_bot(bot)
        return counts

    # ------------------------------------------------------------------ bots / groups (delegated)
    def create_bot(self, body: Any) -> dict[str, Any]:
        try:
            return self.bots.create_bot(body)
        except BotError as exc:
            raise ApiError(400, "invalid", str(exc))

    def message_bot(self, bot_id: str, body: Any) -> dict[str, Any]:
        try:
            return self.bots.message(bot_id, body)
        except BotError as exc:
            raise ApiError(400, "invalid", str(exc))

    def bot_messages(self, bot_id: str, since: Optional[str], limit: Any) -> dict[str, Any]:
        return self.bots.messages(bot_id, since, _limit(limit, MAX_LIST_MESSAGES))

    def create_group(self, body: Any) -> dict[str, Any]:
        try:
            return self.bots.create_group(body)
        except BotError as exc:
            raise ApiError(400, "invalid", str(exc))


def _limit(value: Any, maximum: int) -> int:
    if value is None or value == "":
        return maximum
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "invalid", "limit must be an integer")
    if number < 1:
        raise ApiError(400, "invalid", "limit must be at least 1")
    return min(number, maximum)
