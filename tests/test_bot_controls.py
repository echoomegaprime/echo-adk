"""Owner control preserves bot identity and does not leave old work behind."""

import threading

import pytest

from echo_adk.api.server import BackgroundServer
from echo_adk.api.service import AdkService
from tests.conftest import http, wait_for
from tests.fakes import FakeBroker, ScriptedModel, cap_action, final_action


@pytest.fixture
def controls(agents_copy, tmp_path):
    service = AdkService(
        agents_copy,
        tmp_path / "state",
        start_scheduler=False,
        model_factory=lambda _: ScriptedModel([final_action("finished")]),
        broker=FakeBroker(),
    )
    with BackgroundServer(service) as url:
        yield service, url
    service.close()


def test_update_preserves_identity_and_requires_a_stopped_empty_bot(controls):
    service, url = controls
    original = http("POST", url + "/v2/bots", {"name": "Before", "agent": "knowledge-scout", "instruction": "old"})[2]
    path = url + "/v2/bots/" + original["bot_id"]
    assert http("PUT", path, {})[0] == 400
    assert http("PUT", path, {"agent": "forge-steward"})[0] == 400
    assert http("POST", path + "/resume")[0] == 200
    assert http("PUT", path, {"name": "Not yet"})[0] == 409
    assert http("POST", path + "/pause")[0] == 200
    schedule = {"at": "09:00", "tz": "UTC", "weekdays": [1, 3, 5], "missed": "once"}
    status, _, updated = http(
        "PUT",
        path,
        {"name": "After", "instruction": None, "schedule": schedule, "plugins": [], "lease": {"scopes": []}, "daily_cost_cap_usd": 0},
    )
    assert status == 200 and updated["status"] == "paused"
    assert updated["bot_id"] == original["bot_id"] and updated["memory"] == original["memory"]
    assert updated["created_at"] == original["created_at"] and updated["schedule"] == schedule
    assert updated["lease"] == {**original["lease"], "scopes": []}
    assert not updated.get("instruction") and updated["next_run_at"]
    assert http("PUT", path.replace("/v2", ""), {"name": "legacy"})[0] == 405
    assert service.bots.get_bot(original["bot_id"])["name"] == "After"


def test_stop_cancels_queued_events_without_replaying_or_resuming(controls):
    service, url = controls
    bot = http("POST", url + "/v2/bots", {"name": "Queue", "agent": "knowledge-scout"})[2]
    path = url + "/v2/bots/" + bot["bot_id"]
    event = {"event_id": "task-1", "source": "prime", "text": "do this"}
    receipt = http("POST", path + "/events", event)[2]
    assert http("PUT", path, {"instruction": "different"})[0] == 409
    status, _, stopped = http("POST", path + "/stop", {})
    assert status == 200 and stopped["stop_requested"] and stopped["run_ids"] == [receipt["run_id"]]
    assert stopped["status"] == "paused" and service.bots.get_bot(bot["bot_id"])["inbox_depth"] == 0
    assert service.run_budget(receipt["run_id"])["state"] == "settled"
    assert service.get_run(receipt["run_id"])["proof"]["verdict"] == "UNKNOWN"
    assert http("POST", path + "/events", event)[2]["duplicate"] is True
    assert http("PUT", path, {"instruction": "new task", "schedule": None})[0] == 200
    assert http("POST", path + "/stop", {"grant": "anything"})[0] == 400
    assert service.bots.tick() == 0


def test_stopping_active_bot_prevents_new_capability_after_current_model_call(agents_copy, tmp_path):
    entered, release = threading.Event(), threading.Event()

    class BlockedModel(ScriptedModel):
        def complete(self, system, messages):
            entered.set()
            release.wait(5)
            return super().complete(system, messages)

    model = BlockedModel([cap_action("echo.caps.search", q="must not execute"), final_action("late")])
    broker = FakeBroker()
    service = AdkService(agents_copy, tmp_path / "state", start_scheduler=False, model_factory=lambda _: model, broker=broker)
    try:
        bot = service.create_bot({"name": "Running", "agent": "knowledge-scout"})
        receipt = service.message_bot(bot["bot_id"], {"text": "work"})
        service.bots.resume(bot["bot_id"])
        service.bots.tick()
        assert entered.wait(2)
        stopped = service.bots.stop_bot(bot["bot_id"])
        assert stopped["status"] == "running" and receipt["run_id"] in stopped["run_ids"]
        release.set()
        wait_for(lambda: service.bots.get_bot(bot["bot_id"])["status"] == "paused")
        assert broker.calls == [] and model.calls == 1
        assert service.get_run(receipt["run_id"])["proof"]["verdict"] == "UNKNOWN"
    finally:
        release.set()
        service.close()
