"""Regression checks for an immutable pilot receipt. No model, customer data or network providers."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from echo_adk.api import fielddesk
from echo_adk.api.server import BackgroundServer
from echo_adk.core.types import Envelope, Proof
from tests.conftest import http

ROUTE = "/pilots/fielddesk-followups/result"


def test_disabled_readback_and_post(monkeypatch):
    monkeypatch.delenv("ECHO_ADK_FIELDDESK_PROOF_DIR", raising=False)
    with BackgroundServer(SimpleNamespace()) as url:
        status, _, error = http("GET", url + ROUTE)
        assert status == 503 and error["error"] == "pilot_unavailable"
        assert http("POST", url + ROUTE, {})[0] == 405


@pytest.fixture
def archive(tmp_path, monkeypatch):
    run_id = "a" * 32
    (tmp_path / "data").mkdir()
    (tmp_path / "drafts").mkdir()
    fixture = {"goal": "synthetic goal", "appointments": [{"id": "FD-01", "note": "I will reply."}]}
    state = {"appointments": {"FD-01": {"run_id": run_id, "decision": "PROMISE", "source_quote": "will reply"}}}
    files = {"data/appointments.json": json.dumps(fixture), "state.json": json.dumps(state),
             "score.json": json.dumps({"decisions_correct": 1, "decisions_total": 1}), "drafts/FD-01.md": "draft"}
    for name, data in files.items():
        (tmp_path / name).write_bytes(data.encode())
    hashes = {name: hashlib.sha256(data.encode()).hexdigest() for name, data in files.items()}
    monkeypatch.setattr(fielddesk, "PILOT_DIGEST", fielddesk._digest(hashes))
    monkeypatch.setenv("ECHO_ADK_FIELDDESK_PROOF_DIR", str(tmp_path))
    step = Envelope(run_id, 0, "model", "allowed", True, data={"answered": {"provider": "qwen-local", "model": fielddesk.MODEL}})
    proof = Proof.from_envelopes(run_id, "c" * 32, [step], "PASS").to_dict()
    run = {"run_id": run_id, "agent": fielddesk.AGENT, "status": "done", "proof": proof,
           "spent": {"cost_usd": 0}, "steps": [step.to_dict()]}
    monkeypatch.setattr(fielddesk, "RUNS_DIGEST", fielddesk._digest({run_id: proof["events_sha256"]}))
    return tmp_path, run


def test_repeatable_read_and_tamper_detection(archive):
    root, run = archive
    with BackgroundServer(SimpleNamespace(get_run=lambda run_id: run)) as url:
        expected = http("GET", url + ROUTE)
        assert expected[0] == 200
        payload = expected[2]
        assert payload["kind"] == "completed_pilot"
        assert payload["decisions"][0]["draft"] == "draft"
        assert payload["decisions"][0]["source_note"] == "I will reply."
        assert http("GET", url + ROUTE)[2] == payload
        (root / "drafts/FD-01.md").write_text("changed")
        assert http("GET", url + ROUTE)[0] == 503


def test_worker_receipt_must_match_its_steps(archive):
    _, run = archive
    run["steps"][0]["duration_ms"] += 1
    with BackgroundServer(SimpleNamespace(get_run=lambda run_id: run)) as url:
        assert http("GET", url + ROUTE)[0] == 503
