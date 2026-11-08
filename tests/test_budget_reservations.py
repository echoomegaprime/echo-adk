"""Budget safety under real SQLite contention, HTTP concurrency, cancellation and restart."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from echo_adk.api import budgets
from echo_adk.api.budgets import BudgetAdmissionError
from echo_adk.api.server import BackgroundServer
from echo_adk.api.service import AdkService, ApiError
from echo_adk.api.store import Store
from echo_adk.core import runner, types
from echo_adk.core.models import ProviderUnavailable
from echo_adk.core.types import new_id, now_iso
from tests.conftest import http, wait_for
from tests.fakes import FailingModel, FakeBroker, ScriptedModel, final_action


def run_doc(cost: float, *, bot_id: str | None = None) -> dict:
    return {
        "run_id": new_id(),
        "agent": "paid-steward",
        "status": "queued",
        "started_at": now_iso(),
        "bot_id": bot_id,
        "budget": {"max_calls": 12, "max_cost_usd": cost, "max_wall_s": 300},
        "spent": {"calls": 0, "cost_usd": 0, "wall_s": 0},
    }


def finished(service, run_id):
    return wait_for(lambda: (lambda r: r if r["status"] in ("done", "failed", "gated") else None)(service.get_run(run_id)))


def test_sqlite_contention_reserves_both_scopes_without_overspending(tmp_path):
    # Separate connections, rather than a shared Python lock, contend for the same allowance.
    stores = [Store(tmp_path) for _ in range(12)]
    barrier = threading.Barrier(len(stores))

    def attempt(store):
        doc = run_doc(0.25, bot_id="a")
        barrier.wait(timeout=5)
        try:
            store.admit_run(doc, {"service": 1.0, "bot:a": 0.5})
            return doc
        except BudgetAdmissionError:
            return None

    try:
        with ThreadPoolExecutor(max_workers=len(stores)) as pool:
            accepted = [result for result in pool.map(attempt, stores) if result]
        assert len(accepted) == 2
        assert stores[0].count_runs() == 2
        assert stores[0].budget_status("service", 1.0)["reserved_usd"] == 0.5
        assert stores[0].budget_status("bot:a", 0.5)["available_usd"] == 0
    finally:
        for store in stores:
            store.close()


def test_atomic_run_and_reservation_and_nested_transaction_rollback(tmp_path):
    store = Store(tmp_path)
    try:
        with pytest.raises(BudgetAdmissionError), store.transaction():
            store.admit_run(run_doc(0.5), {"service": 0.75})
            store.admit_run(run_doc(0.5), {"service": 0.75})
        assert store.count_runs() == 0
        assert store.budget_status("service", 0.75)["reserved_usd"] == 0

        invalid = run_doc(0.5)
        del invalid["agent"]  # failure after reservation must roll the entire admission back
        with pytest.raises(KeyError):
            store.admit_run(invalid, {"service": 0.75})
        assert store.budget_status("service", 0.75)["reserved_usd"] == 0
    finally:
        store.close()


def test_exactly_once_settlement_releases_unused_hold_and_charges_both_scopes(tmp_path):
    store = Store(tmp_path)
    doc = run_doc(0.75, bot_id="a")
    try:
        store.admit_run(doc, {"service": 1.0, "bot:a": 1.0})
        receipt = store.settle_budget(doc, 0.2)
        assert receipt["charged_usd"] == 0.2 and receipt["reserved_usd"] == 0
        assert store.settle_budget(doc, 0.9) == receipt
        assert store.spend_today("service") == 0.2
        assert store.spend_today("bot:a") == 0.2
        with pytest.raises(ValueError, match="cannot be reused"):
            store.reserve_budget(doc["run_id"], 0.75, {"service": 1.0, "bot:a": 1.0})
    finally:
        store.close()


def test_forgetting_deleted_run_cannot_erase_fleet_spend_or_uncertainty(tmp_path):
    store = Store(tmp_path)
    doc = run_doc(0.75, bot_id="a")
    try:
        store.admit_run(doc, {"service": 1.0, "bot:a": 1.0})
        store.settle_budget(doc, 0.1, uncertain=True)
        store.forget_budget(doc["run_id"])
        store.delete_runs_for_bot("a")
        store.delete_bot("a")
        health = store.budget_status("service", 1.0)
        assert health["spent_today_usd"] == 0.75
        assert health["uncertain_charged_today_usd"] == 0.75
        assert health["available_usd"] == 0.25
        assert store.spend_today("bot:a") == 0
    finally:
        store.close()


def test_midnight_keeps_all_holds_and_charges_the_settlement_day(tmp_path, monkeypatch):
    store = Store(tmp_path)
    try:
        monkeypatch.setattr(budgets, "now_iso", lambda: "2026-11-07T23:59:59+00:00")
        first = run_doc(0.8)
        store.admit_run(first, {"service": 1.0})
        monkeypatch.setattr(budgets, "now_iso", lambda: "2026-11-08T00:00:01+00:00")
        with pytest.raises(BudgetAdmissionError):
            store.admit_run(run_doc(0.3), {"service": 1.0})
        store.settle_budget(first, 0.3)
        assert store.spend_today("service", day="2026-11-07") == 0
        assert store.spend_today("service", day="2026-11-08") == 0.3
        store.admit_run(run_doc(0.7), {"service": 1.0})
        assert store.budget_status("service", 1.0)["available_usd"] == 0
    finally:
        store.close()


def test_old_spend_migration_cannot_reset_allowance(tmp_path):
    path = tmp_path / "echo-adk.sqlite3"
    with sqlite3.connect(path) as old:
        old.execute("CREATE TABLE spend(day TEXT,scope TEXT,usd REAL,PRIMARY KEY(day,scope))")
        old.execute("INSERT INTO spend VALUES (?,?,?)", (now_iso()[:10], "service", 0.6))
    store = Store(tmp_path)
    try:
        with pytest.raises(BudgetAdmissionError):
            store.admit_run(run_doc(0.5), {"service": 1.0})
        assert store.spend_today("service") == 0.6
    finally:
        store.close()


class BlockingModel(ScriptedModel):
    def __init__(self, entered: threading.Event, release: threading.Event):
        super().__init__([final_action("done")], cost_per_call_usd=0.25)
        self.entered, self.release = entered, release

    def complete(self, system, messages):
        self.entered.set()
        assert self.release.wait(timeout=10), "test did not release the model"
        return super().complete(system, messages)


def test_concurrent_http_admission_rejects_before_second_model_call(agents_copy, tmp_path):
    entered, release = threading.Event(), threading.Event()
    model = BlockingModel(entered, release)
    service = AdkService(
        agents_copy, tmp_path / "state", model_factory=lambda _: model, broker=FakeBroker(), daily_cap_usd=1.0, start_scheduler=False
    )
    try:
        with BackgroundServer(service) as url, ThreadPoolExecutor(max_workers=8) as pool:
            body = {"agent": "paid-steward", "prompt": "work", "budget": {"max_cost_usd": 1.0}}
            replies = list(pool.map(lambda _: http("POST", url + "/runs", body), range(8)))
            assert sorted(status for status, _, _ in replies) == [202] + [429] * 7
            assert entered.wait(timeout=5)
            assert service.budget_health()["reserved_usd"] == 1.0
            assert service.store.count_runs() == 1
            assert all(reply[2]["error"] == "budget_exceeded" for reply in replies if reply[0] == 429)
            release.set()
            accepted = next(reply[2] for reply in replies if reply[0] == 202)
            assert finished(service, accepted["run_id"])["spent"]["cost_usd"] == 0.25
            assert model.calls == 1
            assert service.budget_health()["spent_today_usd"] == 0.25
            assert service.budget_health()["reserved_usd"] == 0
    finally:
        release.set()
        service.close()


def test_inbox_schedule_and_group_all_obey_service_admission(agents_copy, tmp_path):
    models = []

    def factory(_):
        model = ScriptedModel([final_action("done")])
        models.append(model)
        return model

    service = AdkService(
        agents_copy, tmp_path / "state", model_factory=factory, broker=FakeBroker(), daily_cap_usd=1.5, start_scheduler=False
    )
    try:
        bots = [service.create_bot({"name": f"b{i}", "agent": "paid-steward", "daily_cost_cap_usd": 1}) for i in range(2)]
        # A group is all-or-nothing: failure on its second reservation leaves no runs or holds.
        with pytest.raises(ApiError) as denied:
            service.create_group({"name": "pair", "bots": [b["bot_id"] for b in bots], "prompt": "work"})
        assert denied.value.status == 429
        assert service.store.count_runs() == 0 and service.budget_health()["reserved_usd"] == 0

        first = service.message_bot(bots[0]["bot_id"], {"text": "queued while paused"})
        with pytest.raises(ApiError) as denied:
            service.message_bot(bots[1]["bot_id"], {"text": "cannot spend same money"})
        assert denied.value.extra["scope"] == "service"
        assert service.store.count_messages(bots[1]["bot_id"]) == 0

        scheduled = service.create_bot({"name": "schedule", "agent": "paid-steward", "daily_cost_cap_usd": 1, "schedule": {"every_s": 60}})
        service.bots.resume(scheduled["bot_id"])
        service.bots.tick()
        assert not models  # every denied path stopped before constructing a model
        assert service.store.count_runs() == 1
        service.stop_run(first["run_id"])
        assert service.budget_health()["reserved_usd"] == 0
        assert service.budget_health()["spent_today_usd"] == 0
    finally:
        service.close()


def test_bot_admission_counts_other_pending_messages(agents_copy, tmp_path):
    service = AdkService(agents_copy, tmp_path / "state", broker=FakeBroker(), start_scheduler=False)
    try:
        bot = service.create_bot({"name": "paid", "agent": "paid-steward", "daily_cost_cap_usd": 3})
        first = service.message_bot(bot["bot_id"], {"text": "one"})
        with pytest.raises(ApiError) as denied:
            service.message_bot(bot["bot_id"], {"text": "two"})
        assert denied.value.extra["scope"] == "bot"
        assert service.budget_health(bot["bot_id"])["reserved_usd"] == 2
        service.stop_run(first["run_id"])
        second = service.message_bot(bot["bot_id"], {"text": "two"})
        assert first["run_id"] != second["run_id"]
        service.stop_run(second["run_id"])
        assert service.budget_health(bot["bot_id"])["reserved_usd"] == 0
    finally:
        service.close()


@pytest.mark.parametrize("during_call", [False, True])
def test_failure_before_provider_releases_but_unknown_inflight_cost_is_conservative(agents_copy, tmp_path, during_call):
    def factory(_):
        if during_call:
            return FailingModel()
        raise ProviderUnavailable("claude", "not installed")

    service = AdkService(agents_copy, tmp_path / "state", model_factory=factory, broker=FakeBroker(), start_scheduler=False)
    try:
        result = service.create_run({"agent": "paid-steward", "prompt": "work", "budget": {"max_cost_usd": 1}})
        run = finished(service, result["run_id"])
        assert run["status"] == "failed" and run["spent"]["cost_usd"] == 0
        receipt = service.run_budget(result["run_id"])
        assert receipt["uncertain_cost"] is during_call
        assert receipt["charged_usd"] == (1 if during_call else 0)
        service._finish(result["run_id"], "failed")
        assert service.run_budget(result["run_id"]) == receipt  # exactly once despite a duplicate finish
        assert service.budget_health()["reserved_usd"] == 0
    finally:
        service.close()


def test_restart_preserves_queued_receipt_and_does_not_replay_interrupted_work(agents_copy, tmp_path):
    state = tmp_path / "state"
    service = AdkService(agents_copy, state, broker=FakeBroker(), start_scheduler=False)
    interrupted_bot = service.create_bot({"name": "interrupted", "agent": "paid-steward", "daily_cost_cap_usd": 1})
    queued_bot = service.create_bot({"name": "queued", "agent": "paid-steward", "daily_cost_cap_usd": 1})
    interrupted = service.message_bot(interrupted_bot["bot_id"], {"text": "in flight"})["run_id"]
    queued = service.message_bot(queued_bot["bot_id"], {"text": "still queued"})["run_id"]
    # Persist the exact state left by a process dying after dispatch, without invoking a provider.
    service.store.update_run(interrupted, status="running", execution_started=True)
    service.close()

    constructed = []

    def factory(_):
        constructed.append(True)
        return ScriptedModel([final_action("done")], cost_per_call_usd=0.25)

    reopened = AdkService(agents_copy, state, model_factory=factory, broker=FakeBroker(), start_scheduler=False)
    try:
        assert not constructed
        assert reopened.get_run(interrupted)["status"] == "failed"
        assert reopened.run_budget(interrupted)["uncertain_cost"] is True
        assert reopened.run_budget(interrupted)["charged_usd"] == 1
        assert reopened.bots.get_bot(interrupted_bot["bot_id"])["status"] == "paused"
        assert reopened.get_run(queued)["status"] == "queued"
        assert reopened.budget_health()["reserved_usd"] == 1
        assert reopened.budget_health()["uncertain_charged_today_usd"] == 1
        assert reopened.budget_health()["recovery"] == {"interrupted": 1, "cancelled": 0, "queued_preserved": 1}
        assert reopened.bot_messages(queued_bot["bot_id"], None, 50)["messages"][0]["run_id"] == queued
        reopened.stop_run(queued)
        assert reopened.budget_health()["reserved_usd"] == 0
    finally:
        reopened.close()

    again = AdkService(agents_copy, state, broker=FakeBroker(), start_scheduler=False)
    try:
        assert again.budget_health()["spent_today_usd"] == 1  # restarting never charges the same run twice
    finally:
        again.close()


def test_paused_bot_lease_starts_on_dispatch_without_widening_after_restart(agents_copy, tmp_path, monkeypatch):
    clock = [datetime.now(timezone.utc)]

    class TestClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz is not None else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(types, "datetime", TestClock)
    monkeypatch.setattr(runner, "datetime", TestClock)
    state = tmp_path / "state"
    service = AdkService(agents_copy, state, broker=FakeBroker(), start_scheduler=False)
    bot = service.create_bot(
        {
            "name": "paused",
            "agent": "forge-steward",
            "lease": {"scopes": ["echo.caps.search"], "max_uses": 3, "ttl_s": 30},
        }
    )
    queued = service.message_bot(bot["bot_id"], {"text": "later"})
    original = service.store.get_run(queued["run_id"])["lease"]
    # Later bot configuration must not expand authority for the already-queued turn.
    current = service.store.get_bot(bot["bot_id"])
    current["lease"] = {
        "scopes": ["echo.caps.search", "echo.monitor.health.node"],
        "max_uses": 40,
        "ttl_s": 600,
        "grant_ref": "must-not-be-forwarded",
    }
    service.store.put_bot(current)
    service.close()
    clock[0] += timedelta(hours=1)
    model = ScriptedModel([final_action("done")])
    reopened = AdkService(agents_copy, state, model_factory=lambda _: model, broker=FakeBroker(), start_scheduler=False)
    try:
        assert model.calls == 0 and reopened.get_run(queued["run_id"])["status"] == "queued"
        reopened.bots.resume(bot["bot_id"])
        reopened.bots.tick()
        assert finished(reopened, queued["run_id"])["status"] == "done"
        dispatched = reopened.store.get_run(queued["run_id"])["lease"]
        assert dispatched["lease_id"] != original["lease_id"]
        assert dispatched["scopes"] == ["echo.caps.search"]
        assert dispatched["max_uses"] == 3 and dispatched.get("grant_ref") is None
        assert datetime.fromisoformat(dispatched["expires_at"]) > clock[0]
        assert datetime.fromisoformat(dispatched["expires_at"]) <= clock[0] + timedelta(seconds=30)
        assert reopened.bot_messages(bot["bot_id"], None, 50)["messages"][0]["message_id"] == queued["message_id"]
    finally:
        reopened.close()


def test_direct_run_lease_is_not_renewed_and_reduced_bot_cap_is_honored(agents_copy, tmp_path, monkeypatch):
    model = ScriptedModel([final_action("done")])
    service = AdkService(agents_copy, tmp_path / "state", model_factory=lambda _: model, broker=FakeBroker(), start_scheduler=False)
    try:
        monkeypatch.setattr(service, "submit_run", lambda _: None)  # leave the admitted direct run queued
        direct = service.create_run(
            {
                "agent": "forge-steward",
                "prompt": "later",
                "lease": {"scopes": ["echo.caps.search"], "ttl_s": 30, "max_uses": 3},
            }
        )
        doc = service.store.get_run(direct["run_id"])
        doc["lease"]["expires_at"] = "2000-01-01T00:00:00+00:00"
        service.store.put_run(doc)
        assert service.execute_run(direct["run_id"])["status"] == "gated"
        assert model.calls == 0

        bot = service.create_bot({"name": "paid", "agent": "paid-steward", "daily_cost_cap_usd": 1})
        queued = service.message_bot(bot["bot_id"], {"text": "later"})
        current = service.store.get_bot(bot["bot_id"])
        current["daily_cost_cap_usd"] = 0.1
        service.store.put_bot(current)
        result = service.execute_run(queued["run_id"])
        assert result["status"] == "failed" and result["error"]["code"] == "budget_exceeded"
        assert model.calls == 0 and service.budget_health()["reserved_usd"] == 0
    finally:
        service.close()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True])
def test_nonfinite_negative_and_boolean_money_is_refused(value):
    with pytest.raises(ValueError):
        budgets.money_micros(value)
