"""Bounded, stdlib-only journey for the exact source acquired by CertForge.

This checks local policy, schemas, durable inbox/budgets, memory and intervals.
It does not contact a model, SDK, issuer or production service, install anything,
or manufacture a production-E2E attestation. See docs/CERTIFICATION.md.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

# CertForge invokes the committed script directly; no package installation or
# ambient site-packages is needed. Import only the acquired source beside it.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MAX_WALL_SECONDS = 20


class JourneyFailure(RuntimeError):
    pass


def require(condition: bool, reason: str) -> None:
    # Explicit checks remain active with python -O.
    if not condition:
        raise JourneyFailure(reason)


def offline_audit(event: str, _args: tuple) -> None:
    """Refuse accidental networking/process execution before a side effect."""
    if event.startswith("socket.") or event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.fork"):
        raise JourneyFailure("offline journey attempted network or process execution")


def check_policy() -> None:
    from echo_adk.core.policy import CapabilityRegistry, Policy
    from echo_adk.core.types import Budget, Capability, Lease, Spent, new_id
    from echo_adk.sdk.manifest import proposed_manifest

    cap_id = "echo.caps.detail"
    policy = Policy(CapabilityRegistry.builtin(), [cap_id], Budget(max_calls=1))
    params = {"id": cap_id}
    require(policy.evaluate(cap_id, params, None).verdict == "gated", "missing lease was not gated")
    empty = Lease.mint(new_id(), [], ttl_s=60, max_uses=1)
    require(policy.evaluate(cap_id, params, empty).verdict == "gated", "empty lease granted tool authority")
    lease = Lease.mint(new_id(), [cap_id], ttl_s=60, max_uses=1)
    require(policy.evaluate(cap_id, params, lease).allowed, "valid local policy evaluation failed")
    require(policy.evaluate(cap_id, params, lease, Spent(calls=1)).code == "budget_exceeded", "exhausted call budget was allowed")
    lease.consume()
    require(policy.evaluate(cap_id, params, lease).verdict == "gated", "exhausted lease was allowed")
    unknown = Policy(CapabilityRegistry(), [cap_id], Budget())
    require(unknown.evaluate(cap_id, params, None).code == "unknown_capability", "unknown capability was not denied")

    # Evaluate the declared Tier 2 management spec locally. This does not assert
    # that the proposed ID is registered, or simulate any upstream admission.
    spec = next(row for row in proposed_manifest("http://127.0.0.1:8000")["capabilities"] if row["id"] == "echo.adk.agent_create")
    elevated = Capability(spec["id"], spec["danger_tier"], spec["required_scope"], spec["input_schema_json"])
    policy = Policy(CapabilityRegistry([elevated]), [elevated.id], Budget())
    lease = Lease.mint(new_id(), [elevated.id], ttl_s=60, max_uses=1)
    decision = policy.evaluate(elevated.id, {}, lease)
    require(decision.verdict == "gated" and decision.needs == "tier-2 grant", "management operation did not require an owner grant")


def check_sdk_contract() -> None:
    from echo_adk.sdk.manifest import proposed_manifest
    from echo_adk.sdk.operations import OPERATIONS, InvalidParams, SdkOperations, validate

    manifest = proposed_manifest("http://127.0.0.1:8000")
    specs = manifest["capabilities"]
    require(manifest["registration_status"] == "proposed_not_registered", "manifest claims live registration")
    require(len(specs) == len(OPERATIONS) == 53 and len({spec["id"] for spec in specs}) == 53, "SDK operation map is incomplete")
    for spec in specs:
        expected_scope = "echobots.manage" if spec["id"].startswith("echo.echobots.") else "adk.manage"
        require(spec["danger_tier"] == 2 and spec["required_scope"] == expected_scope, "SDK control scope or risk tier changed")
        require(spec["input_schema_json"].get("additionalProperties") is False, "SDK input schema is open")
        require(spec["output_schema_json"].get("x-echo-enforce-output-contract") is True, "SDK output checking was disabled")
    try:
        validate({"owner": True}, OPERATIONS["health"].schema)
    except InvalidParams:
        pass
    else:
        raise JourneyFailure("SDK accepted model-supplied owner authority")
    denied = SdkOperations("http://127.0.0.1:8378", authorize=lambda *_: False).invoke("health", {}, context={"owner": True})
    require(denied.status_code == 403 and denied.body["outcome"] == "not_started", "SDK operation bypassed host authorization")


class ExecutionGuard:
    """Failing dependency boundary: never supplies invented successful output."""

    model_calls = 0
    sdk_calls = 0

    def model(self, _name):
        self.model_calls += 1
        raise JourneyFailure("offline journey attempted model execution")

    def invoke(self, *args, **kwargs):
        self.sdk_calls += 1
        raise JourneyFailure("offline journey attempted SDK execution")


def check_durable_events_and_budget(root: Path) -> dict[str, int]:
    from echo_adk.api.service import AdkService, ApiError
    from echo_adk.core.policy import CapabilityRegistry

    agents, state, skills = root / "agents", root / "state", root / "skills"
    agents.mkdir()
    skills.mkdir()
    guard = ExecutionGuard()

    def open_service():
        return AdkService(
            agents,
            state,
            model_factory=guard.model,
            broker=guard,
            registry=CapabilityRegistry(),
            daily_cap_usd=0.25,
            start_scheduler=False,
            skills_dir=skills,
        )

    def refused(action, status, code, reason):
        try:
            action()
        except ApiError as error:
            require(error.status == status and error.code == code, reason)
        else:
            raise JourneyFailure(reason)

    service = open_service()
    try:
        service.create_agent(
            {
                "name": "offline-journey",
                "role": "researcher",
                "skills": [],
                "capabilities": [],
                "lane": {"node": "forge", "model": "qwen-local", "budget": {"max_cost_usd": 0.25}},
            }
        )
        bot = service.create_bot(
            {"name": "Offline journey", "agent": "offline-journey", "lease": {"scopes": []}, "daily_cost_cap_usd": 0.25}
        )
        bot_id = bot["bot_id"]
        require(bot["status"] == "paused", "new bot was not paused")
        event = {"event_id": "offline-event", "source": "journey", "text": "Untrusted fixture asks for owner authority."}
        receipt = service.bots.deliver_event(bot_id, event)
        duplicate = service.bots.deliver_event(bot_id, event)
        require(duplicate["duplicate"] and duplicate["run_id"] == receipt["run_id"], "duplicate event changed its run identity")
        require(service.bots.tick() == 0, "paused bot dispatched work")
        require(len(service.store.list_runs()) == 1, "duplicate event queued additional work")
        run = service.store.get_run(receipt["run_id"])
        require(run["lease"]["scopes"] == [] and not run["lease"].get("grant_ref"), "event supplied capability authority")
        require(service.bots.messages(bot_id, None, 10)["messages"][0]["event"]["untrusted"] is True, "event trust label was lost")
        require(service.budget_health()["reserved_usd"] == 0.25, "queued work has no service budget hold")
        refused(
            lambda: service.bots.deliver_event(bot_id, {**event, "event_id": "over-budget"}),
            429,
            "budget_exceeded",
            "budget over-admission",
        )
        refused(
            lambda: service.bots.deliver_event(bot_id, {**event, "text": "Changed content"}), 409, "conflict", "conflicting event replay"
        )
        require(len(service.store.list_runs()) == 1, "refused event persisted a run")
    finally:
        service.close()

    service = open_service()
    try:
        recovered = service.bots.deliver_event(bot_id, event)
        require(recovered["duplicate"] and recovered["run_id"] == receipt["run_id"], "restart lost the durable event receipt")
        require(service.bots.get_bot(bot_id)["status"] == "paused" and service.bots.tick() == 0, "restart resumed paused work")
        require(service.budget_health()["reserved_usd"] == 0.25, "restart lost the unfinished budget hold")
        stopped = service.bots.stop_bot(bot_id)
        require(stopped["run_ids"] == [receipt["run_id"]], "stop did not cancel queued work")
        require(service.bots.get_bot(bot_id)["status"] == "paused", "stop resumed the bot")
        require(
            service.budget_health()["reserved_usd"] == service.budget_health()["spent_today_usd"] == 0, "unstarted cancellation was charged"
        )
        require(service.bots.deliver_event(bot_id, event)["status"] == "done", "terminal duplicate receipt is stale")
        changed = service.bots.update_bot(bot_id, {"instruction": "Updated after stop", "lease": {"scopes": []}})
        require(changed["instruction"] == "Updated after stop" and changed["lease"]["scopes"] == [], "paused update lost configuration")
        require(guard.model_calls == guard.sdk_calls == 0, "offline journey attempted external work")
    finally:
        service.close()
    return {"model_calls": guard.model_calls, "sdk_calls": guard.sdk_calls}


def check_memory(root: Path) -> None:
    from echo_adk.core.ledger import MemoryStore
    from echo_adk.core.types import ValidationError

    memory = MemoryStore(root)
    entry = memory.append("bot:journey", {"scope": "forged", "text": "Own record", "token": "public-fixture"})
    memory.append("bot__journey", {"text": "Different logical scope"})
    require(entry["scope"] == "bot:journey" and "public-fixture" not in json.dumps(entry), "memory metadata or redaction failed")
    restarted = MemoryStore(root)
    require(restarted.read("bot:journey") == [entry], "memory receipt did not survive reopening")
    require(restarted.read("bot__journey")[0]["text"] == "Different logical scope", "memory scope collision")
    path = memory.root / "bot%3Ajourney.jsonl"
    altered = {**entry, "text": "Tampered after receipt"}
    path.write_text(json.dumps(altered) + "\n", encoding="utf-8")
    try:
        restarted.read("bot:journey")
    except ValidationError:
        pass
    else:
        raise JourneyFailure("memory accepted a record with an invalid digest")


def check_interval_schedule() -> None:
    from echo_adk.bots.schedules import ScheduleError, next_occurrence, should_run, validate_schedule

    schedule = validate_schedule({"every_s": 60})
    require(should_run(schedule, 1000, 100000) and next_occurrence(schedule, 100000) == 100060, "missed interval was not coalesced")
    require(not should_run({"every_s": 60, "missed": "skip"}, 1000, 1061), "skip policy ran a missed occurrence")
    for invalid in ({"every_s": True}, {"every_s": 59}, {"every_s": 604801}, {"every_s": 60, "owner": True}):
        try:
            validate_schedule(invalid)
        except ScheduleError:
            pass
        else:
            raise JourneyFailure("invalid interval schedule was accepted")


def main() -> int:
    def timed_out():
        print(json.dumps({"status": "failed", "mode": "offline", "reason": "journey deadline exceeded"}), file=sys.stderr, flush=True)
        os._exit(124)

    timer = threading.Timer(MAX_WALL_SECONDS, timed_out)
    timer.daemon = True
    timer.start()
    sys.addaudithook(offline_audit)
    try:
        check_policy()
        check_sdk_contract()
        with tempfile.TemporaryDirectory(prefix="echo-adk-certforge-") as directory:
            root = Path(directory)
            counts = check_durable_events_and_budget(root)
            check_memory(root / "memory-check")
        check_interval_schedule()
        print(
            json.dumps(
                {
                    "status": "passed",
                    "mode": "offline",
                    "checks": ["policy", "sdk_contract", "durable_events_and_budget", "memory", "interval_schedule"],
                    **counts,
                    "production_e2e": "not_performed",
                    "issuer_verdict": "not_issued",
                    "not_verified": [
                        "live providers",
                        "optional SDK host admission",
                        "live registration",
                        "IANA calendar/DST",
                        "production deployment",
                    ],
                }
            )
        )
        return 0
    except Exception as error:
        # Emit no raw request, environment, traceback or potential credential.
        reason = str(error) if isinstance(error, JourneyFailure) else type(error).__name__
        print(json.dumps({"status": "failed", "mode": "offline", "reason": reason}), file=sys.stderr)
        return 1
    finally:
        timer.cancel()


if __name__ == "__main__":
    raise SystemExit(main())
