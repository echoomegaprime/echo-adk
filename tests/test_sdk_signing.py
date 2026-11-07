"""Signing uses synthetic vectors from the actual gate's canonicalization."""

import json
from pathlib import Path

import pytest

from echo_adk.core.broker import BrokerError, GateBroker
from echo_adk.core.policy import CapabilityRegistry
from echo_adk.core.types import Capability, Lease, new_id
from tests.fakes import FakeHttpServer
from tests.test_sdk_integration import gate_envelope

VECTORS = json.loads((Path(__file__).parent / "fixtures/sdk_hmac_vectors.json").read_text())
CAP = "echo.fixture.privileged"


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda v: v["name"])
def test_signer_matches_independent_python_gate_vectors(monkeypatch, vector):
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", VECTORS["api_key"])
    monkeypatch.setenv("ECHO_ADK_GATE_HMAC_SECRET", VECTORS["synthetic_hmac_secret_hex"])
    monkeypatch.setattr("echo_adk.core.broker.secrets.token_hex", lambda _: VECTORS["nonce"])
    monkeypatch.setattr("echo_adk.core.broker.time.time", lambda: VECTORS["ts"])
    with FakeHttpServer() as gate:
        gate.state["payload"] = gate_envelope({"ok": True})
        broker = GateBroker(gate.url)
        ok, _, _, _, _ = broker._request(VECTORS["capability"], json.loads(vector["wire_json"]), {}, sign=True)
        assert ok
        assert gate.requests[-1]["body"]["auth"] == {
            "hmac": vector["hmac_sha256_hex"],
            "nonce": VECTORS["nonce"],
            "ts": VECTORS["ts"],
        }
        assert VECTORS["synthetic_hmac_secret_hex"] not in repr(vars(broker))


def privileged(broker):
    rid = new_id()
    lease = Lease.mint(rid, [CAP], ttl_s=60, max_uses=2, grant_ref="synthetic-admitted-grant-reference")
    return broker.invoke(CAP, {"text": "test"}, lease, run_id=rid, step=1, tier=2)


def registry():
    return CapabilityRegistry([Capability(CAP, 2, "fixture.control", {"type": "object"}, "unknown", "control")])


def test_only_exact_pre_dispatch_challenge_gets_one_signed_attempt(monkeypatch):
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", "synthetic-test-key")
    monkeypatch.setenv("ECHO_ADK_GATE_HMAC_SECRET", "ab" * 32)
    with FakeHttpServer() as gate:

        def response(record):
            if "auth" not in record["body"]:
                return 423, {"detail": {"error": "hmac_required_for_tier2_plus", "capability": CAP}}
            return 200, gate_envelope({"ok": True, "message": "synthetic-test-key", "secret_hmac": "ab" * 32})

        gate.state["responder"] = response
        env = privileged(GateBroker(gate.url, registry=registry(), verify_catalog=False))
        assert env.ok and len(gate.requests) == 2
        assert gate.requests[0]["body"]["params"] == gate.requests[1]["body"]["params"]
        assert "auth" not in gate.requests[0]["body"] and len(gate.requests[1]["body"]["auth"]["nonce"]) == 32
        assert "synthetic-test-key" not in json.dumps(env.to_dict()) and "ab" * 32 not in json.dumps(env.to_dict())
        assert env.redacted >= 2


@pytest.mark.parametrize(
    "status,error,cap",
    [(423, "approval_required", CAP), (403, "hmac_required_for_tier2_plus", CAP), (423, "hmac_required_for_tier2_plus", "echo.other.cap")],
)
def test_other_challenges_are_never_signed_or_retried(monkeypatch, status, error, cap):
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", "synthetic-test-key")
    monkeypatch.setenv("ECHO_ADK_GATE_HMAC_SECRET", "ab" * 32)
    with FakeHttpServer() as gate:
        gate.state.update(status=status, payload={"detail": {"error": error, "capability": cap}})
        env = privileged(GateBroker(gate.url, registry=registry(), verify_catalog=False))
        assert not env.ok and len(gate.requests) == 1


def test_signer_absence_and_second_challenge_do_not_loop(monkeypatch):
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", "synthetic-test-key")
    monkeypatch.delenv("ECHO_ADK_GATE_HMAC_SECRET", raising=False)
    with FakeHttpServer() as gate:
        gate.state.update(status=423, payload={"detail": {"error": "hmac_required_for_tier2_plus", "capability": CAP}})
        broker = GateBroker(gate.url, registry=registry(), verify_catalog=False)
        with pytest.raises(BrokerError, match="per-key signer"):
            privileged(broker)
        assert len(gate.requests) == 1
        monkeypatch.setenv("ECHO_ADK_GATE_HMAC_SECRET", "ab" * 32)
        env = privileged(broker)
        assert not env.ok and len(gate.requests) == 3
