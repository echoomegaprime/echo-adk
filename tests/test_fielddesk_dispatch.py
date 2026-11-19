import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from echo_adk.api.service import AdkService, ApiError
from echo_adk.core.models import ProviderUnavailable
from echo_adk.pilots import fielddesk_jobs, fielddesk_server
from echo_adk.pilots.fielddesk_jobs import AGENT, MODEL, Jobs
from echo_adk.pilots.fielddesk_server import Pilot, ResidentModel
from echo_adk.pilots.fielddesk_worker import advance
from tests.fakes import FakeBroker, ScriptedModel, final_action


@pytest.fixture
def setup_pilot(tmp_path, monkeypatch):
    notes = [{"id": f"FD-{n:02d}", "note": "I promised to call."} for n in range(1, 21)]
    inputs = tmp_path / "appointments.json"
    inputs.write_text(json.dumps({"goal": "Review synthetic appointments", "appointments": notes}))
    monkeypatch.setattr(fielddesk_jobs, "FIXTURE_SHA", hashlib.sha256(inputs.read_bytes()).hexdigest())
    jobs = Jobs(tmp_path / "state", inputs)
    agents = tmp_path / "agents"
    agents.mkdir()
    source = Path(__file__).resolve().parents[1] / "goal-loop" / "agent.json"
    (agents / (AGENT + ".json")).write_bytes(source.read_bytes())
    return jobs, agents, inputs


def test_concurrent_admission_reopen_and_independent_failure(setup_pilot):
    jobs, _, inputs = setup_pilot
    key = {"client_request_id": str(uuid.uuid4())}
    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(jobs.create, [key] * 12))
    assert sum(status == 202 for status, _ in replies) == 1
    gid = replies[0][1]["goal_id"]
    assert {r["goal_id"] for _, r in replies} == {gid}
    reopened = Jobs(jobs.path.parent, inputs)
    assert reopened.create(key)[0] == 200
    first = reopened.next_item()
    reopened.finish(first, error="failed safely")
    assert reopened.next_item()["appointment"] != "FD-01"
    assert reopened.result(gid)["status"] == "pending"
    for _ in range(19):
        reopened.finish(reopened.next_item(), error="fixture failure")
    result = reopened.result(gid)
    assert result["status"] == "completed_with_errors" and len(result["errors"]) == 20
    with pytest.raises(ApiError):
        jobs.create({"client_request_id": "bad", "fixture": "/other"})


def test_fixed_tag_and_context_guard(monkeypatch):
    model = ResidentModel()
    requests = []

    def request(path, body=None):
        requests.append(path)
        return {"models": [{"name": "wrong", "context_length": 262144, "digest": fielddesk_jobs.MODEL_DIGEST}]}

    monkeypatch.setattr(model, "_request", request)
    with pytest.raises(ProviderUnavailable):
        model.complete("test", [{"role": "user", "content": "hello"}])
    assert requests == ["/api/ps"]


def test_crash_between_run_record_and_submit_recovers_exactly_once(setup_pilot, monkeypatch):
    jobs, agents, _ = setup_pilot
    import dataclasses

    from echo_adk.core.models import Answered

    class NamedModel(ScriptedModel):
        def complete(self, system, messages):
            reply = super().complete(system, messages)
            return dataclasses.replace(reply, answered=Answered("qwen-local", MODEL, MODEL, True))

    action = final_action(json.dumps({"decision": "PROMISE", "source_quote": "promised to call", "draft": "May I call today?"}))
    adk = AdkService(
        agents, jobs.path.parent / "adk", model_factory=lambda _: NamedModel([action]), broker=FakeBroker(), start_scheduler=False
    )
    monkeypatch.setattr(fielddesk_server, "seat_idle", lambda: True)
    monkeypatch.setattr(ResidentModel, "ready", lambda self: True)
    pilot = Pilot(jobs, adk)
    _, initial = jobs.create({"client_request_id": str(uuid.uuid4())})
    gid = initial["goal_id"]
    try:
        real_submit = adk.submit_run

        def fault(_):
            raise RuntimeError("interruption after persisting the run")

        monkeypatch.setattr(adk, "submit_run", fault)
        with pytest.raises(RuntimeError):
            pilot.run(gid, "FD-01")
        assert adk.store.count_runs() == 1
        run_id = jobs.next_item()["run_id"]
        assert adk.get_run(run_id)["status"] == "queued"
        monkeypatch.setattr(adk, "submit_run", real_submit)
        recovered = Pilot(jobs, adk)
        for _ in range(100):
            advance(jobs, recovered.run)
            if jobs.result(gid)["completed"]:
                break
            time.sleep(0.01)
        result = jobs.result(gid)
        assert result["decisions"][0]["run_id"] == run_id and result["decisions"][0]["draft"] == "May I call today?"
        assert adk.store.count_runs() == 1 and recovered.run(gid, "FD-01")["status"] == "done"
        assert jobs.create({"client_request_id": str(uuid.uuid4())})[0] == 202
        with pytest.raises(ApiError):
            recovered.run(gid, "FD-20")
    finally:
        adk.close()
