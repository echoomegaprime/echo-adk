from __future__ import annotations

import json

import pytest

from echo_adk.api.service import AdkService, ApiError
from echo_adk.api.store import Store
from echo_adk.bots.manager import BotError, bot_lease_template
from tests.conftest import wait_for
from tests.fakes import FakeBroker, ScriptedModel, cap_action, final_action


@pytest.fixture
def svc(agents_copy, tmp_path):
    def factory(model_id: str):
        cost = 0.4 if model_id == "claude" else 0.0
        return ScriptedModel([final_action("report: all green")], cost_per_call_usd=cost)

    service = AdkService(agents_copy, tmp_path / "state", model_factory=factory, broker=FakeBroker(), start_scheduler=False)
    yield service
    service.close()


def finished(svc, run_id):
    return wait_for(lambda: (lambda r: r if r["status"] in ("done", "failed", "gated") else None)(svc.get_run(run_id)))


def test_lease_template_defaults_and_validation(scout):
    assert bot_lease_template(scout, None) == {"scopes": list(scout.capabilities), "max_uses": 40, "ttl_s": 600}
    assert bot_lease_template(scout, {"scopes": ["echo.caps.search"], "max_uses": 2, "ttl_s": 30})["scopes"] == ["echo.caps.search"]
    for bad in ({"scopes": ["*"]}, {"scopes": ["echo.builds.log"]}, {"max_uses": 0}, {"ttl_s": 5000}, {"grant_ref": "x"}):
        with pytest.raises(BotError):
            bot_lease_template(scout, bad)


def test_bots_start_paused_and_schedule_dispatches_only_when_idle(svc):
    bot = svc.create_bot({"name": "sched", "agent": "forge-steward", "schedule": {"every_s": 60}})
    assert bot["status"] == "paused"
    assert svc.bots.tick(now=1_000.0) == 0  # paused: nothing happens
    svc.bots.resume(bot["bot_id"])
    assert svc.bots.tick(now=1_000.0) == 1  # first scheduled turn
    run_id = svc.store.list_runs(bot_id=bot["bot_id"])[0]["run_id"]
    assert finished(svc, run_id)["status"] == "done"
    wait_for(lambda: svc.bots.get_bot(bot["bot_id"])["status"] == "idle")
    assert svc.bots.tick(now=1_030.0) == 0  # not due yet (every_s = 60)
    assert svc.bots.tick(now=1_100.0) == 1  # due again


def test_bot_turn_uses_its_own_lease_and_memory_scope(svc):
    a = svc.create_bot({"name": "a", "agent": "forge-steward", "lease": {"scopes": ["echo.caps.search"], "max_uses": 3, "ttl_s": 120}})
    b = svc.create_bot({"name": "b", "agent": "forge-steward"})
    svc.bots.resume(a["bot_id"])
    svc.bots.resume(b["bot_id"])
    ra = svc.message_bot(a["bot_id"], {"text": "hi a"})["run_id"]
    rb = svc.message_bot(b["bot_id"], {"text": "hi b"})["run_id"]
    assert svc.bots.tick() == 2
    finished(svc, ra)
    finished(svc, rb)
    doc_a, doc_b = svc.store.get_run(ra), svc.store.get_run(rb)
    assert doc_a["lease"]["scopes"] == ["echo.caps.search"] and doc_a["lease"]["max_uses"] == 3
    assert doc_b["lease"]["scopes"] == ["echo.caps.search", "echo.monitor.health.node"]
    assert doc_a["lease"]["lease_id"] != doc_b["lease"]["lease_id"]  # never shared
    assert doc_a["memory_scope"] == f"bot:{a['bot_id']}" and doc_b["memory_scope"] == f"bot:{b['bot_id']}"
    wait_for(lambda: svc.memory.read(f"bot:{a['bot_id']}"))
    assert svc.memory.read(f"bot:{a['bot_id']}")[-1]["run_id"] == ra
    assert all(entry["run_id"] != ra for entry in svc.memory.read(f"bot:{b['bot_id']}"))


def test_daily_cost_cap_refuses_turn_with_budget_exceeded(svc):
    bot = svc.create_bot({"name": "paid", "agent": "paid-steward", "daily_cost_cap_usd": 0.5})
    svc.bots.resume(bot["bot_id"])
    first = svc.message_bot(bot["bot_id"], {"text": "turn one"})["run_id"]
    assert svc.bots.tick() == 1
    run = finished(svc, first)
    assert run["status"] == "done" and run["spent"]["cost_usd"] == pytest.approx(0.4)
    wait_for(lambda: svc.bots.get_bot(bot["bot_id"])["status"] == "idle")
    assert svc.bots.get_bot(bot["bot_id"])["spent_today_usd"] == pytest.approx(0.4)
    before_runs = svc.store.count_runs()
    before_messages = svc.store.count_messages(bot["bot_id"])
    with pytest.raises(ApiError) as refused:
        svc.message_bot(bot["bot_id"], {"text": "turn two"})
    assert refused.value.status == 429 and refused.value.code == "budget_exceeded"
    assert refused.value.extra["scope"] == "bot"
    assert svc.store.count_runs() == before_runs
    assert svc.store.count_messages(bot["bot_id"]) == before_messages
    assert svc.bots.get_bot(bot["bot_id"])["spent_today_usd"] == pytest.approx(0.4)


def test_daily_spend_rolls_over_by_utc_day(tmp_path):
    store = Store(tmp_path)
    store.add_spend("bot:x", 1.25, day="2026-01-01")
    assert store.spend_today("bot:x", day="2026-01-01") == pytest.approx(1.25)
    assert store.spend_today("bot:x") == 0.0
    store.close()


def test_delete_leaves_nothing_behind(svc, tmp_path):
    bot = svc.create_bot({"name": "ghost", "agent": "forge-steward"})
    bot_id = bot["bot_id"]
    svc.bots.resume(bot_id)
    run_id = svc.message_bot(bot_id, {"text": "remember me"})["run_id"]
    svc.bots.tick()
    finished(svc, run_id)
    wait_for(lambda: svc.memory.read(f"bot:{bot_id}"))
    svc.store.add_spend(f"bot:{bot_id}", 0.0)
    state = tmp_path / "state"
    assert (state / "ledger" / f"{run_id}.jsonl").exists() and (state / "proofs" / f"{run_id}.json").exists()
    removed = svc.bots.delete(bot_id)
    assert removed["bot_id"] == bot_id and removed["deleted"] is True
    assert removed["cleanup"]["memory_deleted"] is True
    assert removed["cleanup"]["connections"]["connections"] == []
    assert svc.store.get_bot(bot_id) is None and svc.store.get_run(run_id) is None
    assert svc.store.list_messages(bot_id, limit=1000) == [] and svc.store.list_runs(bot_id=bot_id) == []
    assert svc.store.spend_today(f"bot:{bot_id}") == 0.0 and svc.memory.read(f"bot:{bot_id}") == []
    assert not (state / "ledger" / f"{run_id}.jsonl").exists() and not (state / "proofs" / f"{run_id}.json").exists()
    leftovers = [p for p in state.rglob("*") if p.is_file() and bot_id in p.read_bytes().decode("utf-8", "replace")]
    assert leftovers == []
    with svc.store._lock:
        for table in ("runs", "bots", "messages", "groups"):
            rows = svc.store._db.execute(f"SELECT doc FROM {table}").fetchall()  # noqa: S608 - fixed table names
            assert all(bot_id not in json.dumps(row) for row in rows), table
        spend_rows = svc.store._db.execute("SELECT scope FROM spend").fetchall()
        assert all(bot_id not in scope for (scope,) in spend_rows)


def test_pause_while_running_takes_effect_after_turn(svc):
    bot = svc.create_bot({"name": "p", "agent": "forge-steward"})
    svc.bots.resume(bot["bot_id"])
    run_id = svc.message_bot(bot["bot_id"], {"text": "go"})["run_id"]
    svc.bots.tick()
    svc.bots.pause(bot["bot_id"])
    finished(svc, run_id)
    wait_for(lambda: svc.bots.get_bot(bot["bot_id"])["status"] == "paused")


def test_group_round_robin_each_bot_own_lease(svc):
    bots = [svc.create_bot({"name": f"g{i}", "agent": "forge-steward"}) for i in range(2)]
    svc.model_factory = lambda model_id: ScriptedModel([cap_action("echo.caps.search", q="x"), final_action("my take")])
    result = svc.create_group({"name": "pair", "bots": [b["bot_id"] for b in bots], "prompt": "discuss"})
    group = wait_for(lambda: (lambda g: g if g["status"] in ("done", "failed") else None)(svc.bots.get_group(result["group_id"])))
    assert group["status"] == "done" and [m["text"] for m in group["messages"]] == ["my take", "my take"]
    leases = {svc.store.get_run(r)["lease"]["lease_id"] for r in result["run_ids"]}
    assert len(leases) == 2
    second_prompt_seen = svc.store.get_run(result["run_ids"][1])
    assert second_prompt_seen["group_id"] == result["group_id"]
    with pytest.raises(BotError):
        svc.bots.create_group({"name": "x", "bots": [bots[0]["bot_id"]] * 1, "prompt": "p"})
