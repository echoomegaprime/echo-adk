from __future__ import annotations

import io
import json
import logging

import pytest

from echo_adk.core import log as echo_log
from echo_adk.core.broker import BrokerError, GateBroker, validate_gate_url
from echo_adk.core.redact import is_secret_field, redact, redact_value
from echo_adk.core.types import Lease, ValidationError, new_id
from tests.fakes import FakeHttpServer, fake_gate_responder

KEY = "sk-live-TESTKEY0123456789abcdefghijklmnop"


@pytest.mark.parametrize("encoded_body", [False, True])
@pytest.mark.parametrize("signed", [False, True])
def test_gate_scrubs_decoded_credentials_in_values_and_object_keys(monkeypatch, encoded_body, signed):
    key, secret = "synthetic-test-key", "ab" * 32
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", key)
    monkeypatch.setenv("ECHO_ADK_GATE_HMAC_SECRET", secret)
    target = json.dumps({"ok": True, "note": key, "nested": [{"trace": secret, key: "key-named field", secret: "signer-named field"}]})
    # Equivalent JSON escapes must not evade redaction after either decoding
    # layer. These values deliberately lack the generic token-prefix patterns.
    for sensitive in (key, secret):
        target = target.replace(sensitive, "".join(f"\\u{ord(char):04x}" for char in sensitive))
    body = json.dumps(target) if encoded_body else target
    raw = ('{"status":"ok","result":{"status_code":200,"body":' + body + "}}").encode()

    class Response(io.BytesIO):
        status = 200
        headers = {"X-Echo-Audit-Id": "reflected-" + key}

    broker = GateBroker(opener=lambda *_args, **_kwargs: Response(raw), verify_catalog=False)
    ok, data, audit_id, _, count = broker._request("echo.fixture.read", {"command": "read"}, {}, sign=signed)
    assert ok and audit_id is None and count >= 5
    assert key not in json.dumps(data) and secret not in json.dumps(data)
    assert data["result"]["body"]["note"] == "[REDACTED]"
    assert data["result"]["body"]["nested"][0]["trace"] == "[REDACTED]"


def test_gate_credential_scrubbing_bounds_deep_untrusted_data(monkeypatch):
    key = "synthetic-opaque-credential"
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", key)
    value = {"note": key}
    for _ in range(80):
        value = {"nested": value}
    raw = json.dumps({"status": "ok", "result": {"status_code": 200, "body": {"ok": True, "value": value}}}).encode()

    class Response(io.BytesIO):
        status = 200
        headers = {}

    broker = GateBroker(opener=lambda *_args, **_kwargs: Response(raw), verify_catalog=False)
    ok, data, _, _, count = broker._request("echo.fixture.read", {"command": "read"}, {})
    assert ok and count >= 1
    output = json.dumps(data)
    assert key not in output and output.count('"nested"') <= 64 and "[REDACTED]" in output


def test_redact_patterns_and_count():
    text = (
        "api_key=abc123 Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig "
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 password: 'hunter2' ok"
    )
    clean, count = redact(text)
    assert "abc123" not in clean and "hunter2" not in clean and "ghp_" not in clean
    assert "Bearer [REDACTED]" in clean
    assert count >= 4
    assert redact("nothing secret here") == ("nothing secret here", 0)
    key_block = "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"
    assert redact(key_block) == ("[REDACTED PRIVATE KEY]", 1)


def test_redact_value_wipes_secret_named_fields_recursively():
    value = {"token": "abc", "nested": [{"api_key": "x", "safe": "fine"}], "text": "secret=zzz", "empty": ""}
    cleaned, count = redact_value(value)
    assert cleaned["token"] == "[REDACTED]" and cleaned["nested"][0]["api_key"] == "[REDACTED]" and cleaned["nested"][0]["safe"] == "fine"
    assert cleaned["text"] == "secret=[REDACTED]" and cleaned["empty"] == "" and count == 3
    assert is_secret_field("X-Echo-Api-Key") and is_secret_field("private_key") and not is_secret_field("monkey")


def test_gate_url_is_loopback_unless_explicitly_allowed():
    assert validate_gate_url("http://127.0.0.1:8000/", allow_remote=False) == "http://127.0.0.1:8000"
    with pytest.raises(ValidationError, match="not loopback"):
        validate_gate_url("http://100.113.87.107:8000", allow_remote=False)
    assert validate_gate_url("http://100.113.87.107:8000", allow_remote=True).endswith(":8000")
    with pytest.raises(ValidationError):
        validate_gate_url("http://user:pw@127.0.0.1:8000", allow_remote=True)


def test_broker_sends_key_header_from_env_and_redacts_response(monkeypatch):
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", KEY)
    stream = io.StringIO()
    monkeypatch.setenv("ECHO_ADK_DEBUG", "1")
    echo_log.configure(stream, force=True)
    with FakeHttpServer() as gate:
        gate.state["responder"] = fake_gate_responder
        gate.state["headers"] = {"X-Echo-Audit-Id": "hdr-audit"}
        broker = GateBroker(gate.url, timeout_s=5)
        run_id = new_id()
        lease = Lease.mint(run_id, ["echo.caps.search"], ttl_s=60, max_uses=2)
        env = broker.invoke("echo.caps.search", {"q": "knowledge"}, lease, run_id=run_id, step=1, tier=0)
        sent = gate.requests[-1]
        assert sent["headers"].get("x-echo-api-key") == KEY
        assert sent["body"] == {
            "envelope_version": 1,
            "capability": "echo.caps.search",
            "params": {"command": "search", "q": "knowledge"},
            "context": {"run_id": run_id, "step": 1, "source": "echo-adk"},
        }
        assert env.ok and env.verdict == "allowed" and env.untrusted is True and env.audit_id == "hdr-audit"
        assert env.data["result"]["body"]["data"]["api_key"] == "[REDACTED]" and env.redacted >= 1
        assert lease.uses == 1
        serialised = json.dumps(env.to_dict())
        assert KEY not in serialised
    echo_log.configure(force=True)
    logging.getLogger("echo_adk").setLevel(logging.INFO)
    assert KEY not in stream.getvalue() and "gate_invoke" in stream.getvalue()


def test_broker_checks_lease_before_any_network_call(monkeypatch):
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", KEY)
    with FakeHttpServer() as gate:
        gate.state["responder"] = fake_gate_responder
        broker = GateBroker(gate.url, timeout_s=5)
        run_id = new_id()
        lease = Lease.mint(run_id, ["echo.caps.list"], ttl_s=60, max_uses=2)
        env = broker.invoke("echo.caps.search", {}, lease, run_id=run_id, step=1, tier=0)
        assert env.verdict == "gated" and env.needs == "lease scope echo.caps.search"
        assert gate.requests == []  # R3: nothing left the process
        assert broker.invoke("echo.caps.search", {}, None, run_id=run_id, step=2, tier=0).verdict == "gated"
        assert gate.requests == []


def test_broker_without_key_or_gate_is_unavailable(monkeypatch):
    monkeypatch.delenv("ECHO_ADK_GATE_KEY", raising=False)
    run_id = new_id()
    lease = Lease.mint(run_id, ["echo.caps.list"], ttl_s=60, max_uses=2)
    broker = GateBroker("http://127.0.0.1:1", timeout_s=1)
    with pytest.raises(BrokerError) as exc:
        broker.invoke("echo.caps.list", {}, lease, run_id=run_id, step=1, tier=0)
    assert exc.value.code == "unavailable" and "ECHO_ADK_GATE_KEY" in str(exc.value)
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", KEY)
    with pytest.raises(BrokerError) as exc2:
        broker.invoke("echo.caps.list", {}, lease, run_id=run_id, step=1, tier=0)
    assert exc2.value.code == "unavailable" and exc2.value.provider == "echo-gate"
    assert lease.uses == 0  # a failed call does not consume the lease


def test_broker_key_env_name_is_configurable(monkeypatch):
    monkeypatch.setenv("ECHO_ADK_GATE_KEY_ENV", "MY_GATE")
    monkeypatch.setenv("MY_GATE", KEY)
    with FakeHttpServer() as gate:
        gate.state["responder"] = fake_gate_responder
        broker = GateBroker(gate.url, timeout_s=5)
        run_id = new_id()
        lease = Lease.mint(run_id, ["echo.caps.list"], ttl_s=60, max_uses=2)
        broker.invoke("echo.caps.list", {}, lease, run_id=run_id, step=1, tier=0)
        assert gate.requests[-1]["headers"]["x-echo-api-key"] == KEY
        assert not hasattr(broker, "key") and KEY not in repr(vars(broker))


def test_gate_error_status_is_not_ok_but_still_enveloped(monkeypatch):
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", KEY)
    with FakeHttpServer() as gate:
        gate.state["status"], gate.state["payload"] = 403, {"ok": False, "error": "scope_forbidden", "token": "leak"}
        broker = GateBroker(gate.url, timeout_s=5, verify_catalog=False)
        run_id = new_id()
        lease = Lease.mint(run_id, ["echo.caps.list"], ttl_s=60, max_uses=2)
        env = broker.invoke("echo.caps.list", {}, lease, run_id=run_id, step=1, tier=0)
        assert env.ok is False and env.verdict == "allowed" and env.data["http_status"] == 403 and env.data["token"] == "[REDACTED]"
