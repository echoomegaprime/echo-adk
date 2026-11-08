from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from echo_adk.bots.continuity import BotContinuity, ContinuityError
from echo_adk.core.types import Capability, Envelope

CAPTURE = "echo.continuity.capture"
RECALL = "echo.continuity.recall"
BOT = "a" * 32
OTHER = "b" * 32


class OwnerBroker:
    def __init__(self):
        self.calls = []
        self.rows = {}
        self.lose_ack = False
        self.bad_recall = False
        self.reject = False
        self.excerpt_units = 1200
        self.entered = None
        self.release = None

    def resolve_capability(self, cap):
        return Capability(
            cap,
            1 if cap == CAPTURE else 0,
            "continuity.write" if cap == CAPTURE else "continuity.read",
            {"type": "object"},
            "green",
            "capture" if cap == CAPTURE else "recall",
        )

    def invoke(self, cap, params, lease, *, run_id, step, tier, context):
        assert lease.run_id == run_id and lease.permits(cap) and not lease.exhausted()
        assert lease.scopes == [CAPTURE, RECALL] and lease.max_uses <= 2
        assert set(context) == {"bot_id", "source"}
        lease.consume()
        self.calls.append((cap, dict(params), lease.lease_id))
        if self.entered:
            self.entered.set()
            assert self.release.wait(5)
        if self.reject:
            body = {"ok": False, "error": "insufficient_scope"}
        elif cap == CAPTURE:
            key = params["requestId"]
            duplicate = key in self.rows
            if duplicate:
                assert self.rows[key]["payload"] == params
            else:
                text = (params["title"] + "\n" + params["detail"]).strip()
                receipt = {
                    "id": "note_123",
                    "eventId": "evt_" + key[-20:],
                    "kind": "note",
                    "requestId": key,
                    "saved": True,
                    "verified": True,
                    "receiptState": "capture_verified",
                    "duplicate": False,
                    "contentSha256": hashlib.sha256(text.encode()).hexdigest(),
                }
                self.rows[key] = {"payload": dict(params), "text": text, "receipt": receipt}
            body = {"ok": True, "service": "echo-continuity-fabric-mcp", "result": {**self.rows[key]["receipt"], "duplicate": duplicate}}
            if self.lose_ack:
                self.lose_ack = False
                raise TimeoutError("private provider error must not be returned")
        else:
            row = next(r for r in self.rows.values() if r["receipt"]["eventId"] == params["query"])
            text = row["text"]
            encoded = text.encode("utf-16-le")
            clipped = len(encoded) > self.excerpt_units * 2
            excerpt = encoded[: (self.excerpt_units - 1) * 2].decode("utf-16-le", errors="surrogatepass") + "…" if clipped else text
            body = {
                "ok": True,
                "service": "echo-continuity-fabric-mcp",
                "result": {
                    "events": [
                        {
                            "id": row["receipt"]["eventId"],
                            "project_id": row["payload"]["projectId"],
                            "content_sha256": "0" * 64 if self.bad_recall else row["receipt"]["contentSha256"],
                            "content_text": excerpt,
                            "excerpt_truncated": clipped,
                        }
                    ]
                },
            }
        return Envelope(
            run_id,
            step,
            "cap",
            "allowed",
            True,
            capability=cap,
            tier=tier,
            data={"status": "ok", "run_id": "routing-only", "result": {"status_code": 200, "body": body}},
        )


@pytest.fixture
def fixture(tmp_path):
    broker = OwnerBroker()
    bots = {b: {"bot_id": b, "agent": "notes", "lease": {"scopes": [CAPTURE, RECALL], "max_uses": 2, "ttl_s": 60}} for b in (BOT, OTHER)}
    manager = BotContinuity(
        tmp_path,
        owner_id="test-owner",
        require_bot=lambda b: bots[b],
        capabilities=lambda b: [CAPTURE, RECALL],
        broker=lambda: broker,
        projects={BOT: "project_notes", OTHER: "project_other"},
        enabled=True,
    )
    yield manager, broker, bots, tmp_path
    manager.close()


def payload(**overrides):
    return {"request_ref": "decision-001", "title": "Bounded decision", "detail": "Explicit owner summary.", **overrides}


def test_capture_binds_project_bot_lease_and_exact_receipt(fixture):
    manager, broker, _, _ = fixture
    result = manager.capture(BOT, payload())
    assert result["state"] == "verified" and result["saved"] and result["verified"]
    assert result["request_ref"] == payload()["request_ref"]
    sent = broker.calls[0][1]
    assert sent["projectId"] == "project_notes" and BOT in sent["detail"]
    assert sent["kind"] == "note" and result["receipt"]["requestId"] == sent["requestId"]
    assert [c[0] for c in broker.calls] == [CAPTURE, RECALL]
    assert broker.calls[0][2] == broker.calls[1][2]
    assert manager.capture(BOT, payload()) == result
    assert len(broker.calls) == 2
    assert manager.status(OTHER)["captures"] == []
    with pytest.raises(ContinuityError, match="not found"):
        manager.reconcile(OTHER, result["capture_id"])


def test_unknown_write_survives_restart_and_reuses_original_request(fixture):
    manager, broker, bots, directory = fixture
    broker.lose_ack = True
    first = manager.capture(BOT, payload())
    assert first["state"] == "uncertain" and first["saved"] is False and len(broker.rows) == 1
    assert "private provider" not in json.dumps(first)
    manager.close()
    reopened = BotContinuity(
        directory,
        owner_id="test-owner",
        require_bot=lambda b: bots[b],
        capabilities=lambda b: [CAPTURE, RECALL],
        broker=lambda: broker,
        projects={BOT: "project_notes"},
        enabled=True,
    )
    try:
        assert len(broker.calls) == 1  # reopening never retries
        result = reopened.reconcile(BOT, first["capture_id"])
        assert result["verified"] and result["receipt"]["duplicate"]
        assert len(broker.rows) == 1 and broker.calls[0][1] == broker.calls[1][1]
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "changed",
    [
        {"projectId": "another"},
        {"owner_id": "other"},
        {"lease": {}},
        {"detail": "password=do-not-copy"},
        {"title": "x" * 201},
        {"request_ref": "ghp_" + "x" * 24},
    ],
)
def test_invalid_or_secret_input_never_dispatches(fixture, changed):
    manager, broker, *_ = fixture
    with pytest.raises(ContinuityError):
        manager.capture(BOT, payload(**changed))
    assert not broker.calls and not manager.status(BOT)["captures"]


def test_scope_revocation_and_binding_changes_never_expand_permission(fixture):
    manager, broker, bots, _ = fixture
    bots[BOT]["lease"]["scopes"] = [RECALL]
    with pytest.raises(ContinuityError, match="lease"):
        manager.capture(BOT, payload())
    assert not broker.calls
    bots[BOT]["lease"]["scopes"] = [CAPTURE, RECALL]
    broker.lose_ack = True
    result = manager.capture(BOT, payload())
    manager.projects[BOT] = "changed-project"
    with pytest.raises(ContinuityError, match="binding"):
        manager.reconcile(BOT, result["capture_id"])
    assert len(broker.calls) == 1


def test_receipt_saved_but_mismatched_recall_is_not_verified(fixture):
    manager, broker, *_ = fixture
    broker.bad_recall = True
    first = manager.capture(BOT, payload(detail="a" * 1600))
    assert first["state"] == "saved" and first["saved"] and not first["verified"]
    broker.bad_recall = False
    final = manager.reconcile(BOT, first["capture_id"])
    assert final["verified"]
    assert [c[0] for c in broker.calls] == [CAPTURE, RECALL, RECALL]


@pytest.mark.parametrize("units", [160, 511, 1200])
def test_actual_owner_variable_utf16_excerpt_preserves_exact_readback(fixture, units):
    manager, broker, *_ = fixture
    broker.excerpt_units = units
    result = manager.capture(BOT, payload(detail="😀" * 1700))
    assert result["verified"] and result["receipt"]["contentSha256"] == result["content_sha256"]


def test_cross_project_readback_never_verifies_even_with_matching_hash(fixture, monkeypatch):
    manager, broker, *_ = fixture
    original = broker.invoke

    def wrong_project(cap, *args, **kwargs):
        result = original(cap, *args, **kwargs)
        if cap == RECALL:
            result.data["result"]["body"]["result"]["events"][0]["project_id"] = "other-project"
        return result

    monkeypatch.setattr(broker, "invoke", wrong_project)
    result = manager.capture(BOT, payload())
    assert result["saved"] and not result["verified"] and result["state"] == "saved"


def test_nested_rejection_never_becomes_saved(fixture):
    manager, broker, *_ = fixture
    broker.reject = True
    result = manager.capture(BOT, payload())
    assert not result["saved"] and not result["verified"] and result["state"] == "blocked"
    assert len(broker.calls) == 1


def test_same_key_changed_summary_refuses_without_write(fixture):
    manager, broker, *_ = fixture
    manager.capture(BOT, payload())
    with pytest.raises(ContinuityError, match="different"):
        manager.capture(BOT, payload(detail="changed"))
    assert len(broker.calls) == 2


def test_concurrent_capture_is_bounded_and_delete_preserves_unknown(fixture):
    manager, broker, *_ = fixture
    broker.entered, broker.release = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(manager.capture, BOT, payload())
        assert broker.entered.wait(3)
        with pytest.raises(ContinuityError, match="active"):
            manager.capture(BOT, payload())
        with pytest.raises(ContinuityError, match="pending"):
            manager.ensure_deletable(BOT)
        broker.release.set()
        assert future.result(timeout=3)["verified"]
    manager.ensure_deletable(BOT)
    assert manager.has_resources(BOT)
    manager.delete_bot(BOT)
    assert not manager.has_resources(BOT) and len(broker.rows) == 1  # canonical record is retained


def test_later_scope_denial_cannot_erase_an_earlier_unknown_write(fixture):
    manager, broker, *_ = fixture
    broker.lose_ack = True
    first = manager.capture(BOT, payload())
    broker.reject = True
    denied = manager.reconcile(BOT, first["capture_id"])
    assert denied["state"] == "uncertain" and not denied["verified"]
    with pytest.raises(ContinuityError, match="pending"):
        manager.ensure_deletable(BOT)


def test_late_owner_ack_is_saved_but_never_claimed_verified(fixture, monkeypatch):
    import echo_adk.bots.continuity as module

    manager, broker, *_ = fixture
    original = broker.invoke

    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        time.sleep(0.02)
        return result

    monkeypatch.setattr(module, "WALL_S", 0.01)
    # TTL validation remains integer; narrow only the cooperative elapsed time.
    monkeypatch.setattr(
        module.Lease,
        "mint",
        lambda run, scopes, **_: __import__("echo_adk.core.types", fromlist=["Lease"]).Lease(run, run, scopes, "2099-01-01T00:00:00Z", 2),
    )
    monkeypatch.setattr(broker, "invoke", slow)
    result = manager.capture(BOT, payload())
    assert result["saved"] and not result["verified"] and len(broker.calls) == 1


def test_host_config_is_closed_and_disabled_without_a_binding(tmp_path, monkeypatch):
    from echo_adk.bots.continuity import from_env

    monkeypatch.delenv("ECHO_ADK_CONTINUITY_CONFIG", raising=False)
    args = dict(owner_id="fixed-owner", require_bot=lambda _: {}, capabilities=lambda _: [], broker=lambda: None)
    manager = from_env(tmp_path, **args)
    try:
        assert not manager.status(BOT)["configured"] and not manager.status(BOT)["lease_permits_capture"]
        with pytest.raises(ContinuityError, match="not enabled"):
            manager.capture(BOT, payload())
    finally:
        manager.close()
    config = tmp_path / "continuity.json"
    config.write_text(json.dumps({"enabled": True, "projects": {BOT: "notes"}, "owner": "caller"}), encoding="utf-8")
    monkeypatch.setenv("ECHO_ADK_CONTINUITY_CONFIG", str(config))
    with pytest.raises(ContinuityError, match="invalid"):
        from_env(tmp_path, **args)


def test_outbox_capacity_preserves_uncertain_identity(fixture, monkeypatch):
    import echo_adk.bots.continuity as module

    manager, broker, *_ = fixture
    monkeypatch.setattr(module, "MAX_RECORDS_PER_BOT", 1)
    broker.lose_ack = True
    first = manager.capture(BOT, payload())
    with pytest.raises(ContinuityError, match="capacity") as denied:
        manager.capture(BOT, payload(request_ref="another-summary"))
    assert denied.value.status == 429 and len(broker.calls) == 1
    assert manager.status(BOT)["captures"][0]["request_ref"] == "decision-001"
    assert manager.reconcile(BOT, first["capture_id"])["verified"]


def test_longer_sdk_transport_is_limited_to_two_continuity_mutations(monkeypatch):
    from echo_adk.sdk.operations import SdkOperations

    observed = []

    class Timeouts:
        def open(self, request, *, timeout):
            observed.append((request.full_url, timeout))
            raise TimeoutError("synthetic transport wait")

    client = SdkOperations("http://127.0.0.1:12345", authorize=lambda *_: True)
    monkeypatch.setattr(client, "_opener", Timeouts())
    for op, params in (
        ("bot.continuity.capture", {"bot_id": BOT, **payload()}),
        ("bot.continuity.reconcile", {"bot_id": BOT, "capture_id": OTHER}),
        ("bot.continuity.status", {"bot_id": BOT}),
        ("health", {}),
    ):
        result = client.invoke(op, params, context=None)
        assert result.body["ok"] is False
        if op.endswith(("capture", "reconcile")):
            assert result.body["outcome"] == "unknown"
    assert [wait for _, wait in observed] == [55, 55, 30, 30]
    with pytest.raises(ValueError, match="continuity timeout"):
        SdkOperations("http://127.0.0.1:12345", authorize=lambda *_: True, continuity_timeout_s=56)


@pytest.mark.parametrize("bad_reply", ["different_reference", "saved_without_ack", "receipt_without_saved"])
def test_sdk_refuses_mismatched_or_false_save_receipts(fixture, bad_reply):
    from echo_adk.sdk.operations import SdkOperations

    manager, _, *_ = fixture
    saved = manager.capture(BOT, payload())
    submitted_ref = "decision-001"
    if bad_reply == "different_reference":
        submitted_ref = "different-summary"
    elif bad_reply == "saved_without_ack":
        saved.update(state="saved", saved=False, verified=False, receipt=None)
    else:
        saved.update(state="uncertain", saved=False, verified=False)
    received = []

    class StaleWorker(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            raw = json.dumps(saved).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), StaleWorker)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = SdkOperations(f"http://127.0.0.1:{server.server_port}", authorize=lambda *_: True)
        result = client.invoke("bot.continuity.capture", {"bot_id": BOT, **payload(request_ref=submitted_ref)}, context=object())
        assert result.status_code == 502 and result.body["outcome"] == "unknown"
        assert len(received) == 1 and received[0]["request_ref"] == submitted_ref
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_sdk_native_http_and_real_gate_transport_use_bound_payload(agents_copy, tmp_path, monkeypatch):
    from echo_adk.api.server import BackgroundServer
    from echo_adk.api.service import AdkService
    from echo_adk.core.broker import GateBroker
    from echo_adk.sdk.operations import SdkOperations

    received, rows = [], {}
    key = "synthetic-test-owner-key"
    monkeypatch.setenv("CONTINUITY_TEST_GATE_KEY", key)

    class Owner(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == "/sdk/invoke" and self.headers["X-Echo-API-Key"] == key
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            cap, params = request["capability"], request["params"]
            received.append((cap, params))
            if cap in ("echo.caps.detail", "echo.caps.search"):
                target = params.get("id") or params.get("q")
                record = {
                    "id": target,
                    "tier": 1 if target == CAPTURE else 0,
                    "scope": "continuity.write" if target == CAPTURE else "continuity.read",
                    "health": "green",
                    "verb": "capture" if target == CAPTURE else "recall",
                    "params": [],
                }
                body = {"ok": True, "cap": record} if cap.endswith("detail") else {"ok": True, "caps": [record]}
            elif cap == CAPTURE:
                assert params["command"] == "capture" and params["projectId"] == "project_notes"
                assert set(params) == {"command", "kind", "title", "detail", "projectId", "requestId"}
                content = (params["title"] + "\n" + params["detail"]).strip()
                receipt = {
                    "id": "evt_controlled",
                    "eventId": "evt_controlled",
                    "kind": "note",
                    "requestId": params["requestId"],
                    "saved": True,
                    "verified": True,
                    "receiptState": "capture_verified",
                    "duplicate": False,
                    "contentSha256": hashlib.sha256(content.encode()).hexdigest(),
                }
                rows["evt_controlled"] = {
                    "id": "evt_controlled",
                    "project_id": "project_notes",
                    "content_text": content,
                    "content_sha256": receipt["contentSha256"],
                }
                body = {"ok": True, "service": "echo-continuity-fabric-mcp", "result": receipt}
            else:
                assert cap == RECALL and params == {"command": "recall", "query": "evt_controlled"}
                body = {"ok": True, "service": "echo-continuity-fabric-mcp", "result": {"events": [rows[params["query"]]]}}
            raw = json.dumps({"status": "ok", "run_id": "controlled-routing", "result": {"status_code": 200, "body": body}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    owner = ThreadingHTTPServer(("127.0.0.1", 0), Owner)
    thread = threading.Thread(target=owner.serve_forever, daemon=True)
    thread.start()
    broker = GateBroker(f"http://127.0.0.1:{owner.server_port}", key_env="CONTINUITY_TEST_GATE_KEY")
    service = AdkService(agents_copy, tmp_path / "service", broker=broker, start_scheduler=False)
    service.create_agent(
        {
            "name": "continuity-notes",
            "role": "researcher",
            "capabilities": [CAPTURE, RECALL],
            "lane": {"node": "forge", "model": "qwen-local"},
        }
    )
    bot = service.create_bot(
        {"name": "Notes", "agent": "continuity-notes", "lease": {"scopes": [CAPTURE, RECALL], "max_uses": 2, "ttl_s": 60}}
    )["bot_id"]
    service.workspace.continuity.enabled = True
    service.workspace.continuity.projects[bot] = "project_notes"
    # Definition admission legitimately performs live catalog reads. The owner
    # SDK refusal below must add no requests of its own, especially no write.
    assert received and all(cap in ("echo.caps.detail", "echo.caps.search") for cap, _ in received)
    received.clear()
    context = object()
    try:
        with BackgroundServer(service) as origin:
            sdk = SdkOperations(origin, authorize=lambda _, c: c is context)
            forbidden = sdk.invoke("bot.continuity.capture", {"bot_id": bot, **payload()}, context=None)
            assert forbidden.status_code == 403 and received == []
            invalid = sdk.invoke("bot.continuity.capture", {"bot_id": bot, **payload(), "projectId": "other"}, context=context)
            assert invalid.status_code == 400 and received == []
            result = sdk.invoke("bot.continuity.capture", {"bot_id": bot, **payload()}, context=context)
            assert result.status_code == 200 and result.body["data"]["verified"]
            assert result.body["data"]["bot_id"] == bot and key not in json.dumps(result.body)
            status = sdk.invoke("bot.continuity.status", {"bot_id": bot}, context=context)
            assert len(status.body["data"]["captures"]) == 1
            assert status.body["data"]["remote_authorization_verified"] is False
            assert len([c for c, _ in received if c == CAPTURE]) == 1
            assert service.health()["runs_active"] == 0  # no hidden model run
    finally:
        service.close()
        owner.shutdown()
        owner.server_close()
        thread.join(3)
