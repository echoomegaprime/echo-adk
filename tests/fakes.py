"""Stdlib-only test doubles: scripted model, fake broker, fake gate HTTP server, fake loopback model server."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from echo_adk.core.broker import BrokerError
from echo_adk.core.models import Answered, ModelReply, ProviderUnavailable
from echo_adk.core.types import Envelope, Lease


def cap_action(capability: str, **params: Any) -> str:
    return json.dumps({"action": "cap", "capability": capability, "params": params})


def final_action(text: str) -> str:
    return json.dumps({"action": "final", "text": text})


class ScriptedModel:
    """Returns scripted replies in order; the last reply repeats. Counts calls."""

    provider = "fake"

    def __init__(
        self, replies: list[str], *, provider: str = "fake", cost_per_call_usd: float = 0.0, model_name: Optional[str] = "fake-1"
    ) -> None:
        self.replies = list(replies)
        self.provider = provider
        self.cost_per_call_usd = cost_per_call_usd
        self.model_name = model_name
        self.calls = 0
        self.seen: list[tuple[str, list[dict[str, str]]]] = []

    def complete(self, system: str, messages: list[dict[str, str]]) -> ModelReply:
        self.calls += 1
        self.seen.append((system, list(messages)))
        index = min(self.calls - 1, len(self.replies) - 1)
        return ModelReply(self.replies[index], Answered(self.provider, "fake-requested", self.model_name, self.model_name is not None))


class FailingModel:
    provider = "claude"
    cost_per_call_usd = 0.0

    def __init__(self, provider: str = "claude") -> None:
        self.provider = provider
        self.calls = 0

    def complete(self, system: str, messages: list[dict[str, str]]) -> ModelReply:
        self.calls += 1
        raise ProviderUnavailable(self.provider, "simulated outage")


class FakeBroker:
    """In-process broker: records calls, consumes the lease, returns an allowed envelope."""

    provider = "fake-gate"

    def __init__(self, data: Optional[dict[str, Any]] = None, *, fail: Optional[BrokerError] = None) -> None:
        self.data = data or {"ok": True, "hits": [{"title": "ESH grants", "doc_id": "esh-tiers-and-trust"}]}
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke(self, capability, params, lease: Optional[Lease], *, run_id, step, tier, context=None) -> Envelope:
        self.calls.append((capability, dict(params)))
        if self.fail is not None:
            raise self.fail
        if lease is None or not lease.permits(capability):
            return Envelope(
                run_id=run_id,
                step=step,
                kind="cap",
                verdict="gated",
                ok=False,
                capability=capability,
                tier=tier,
                needs=f"lease scope {capability}",
                how="re-mint",
            )
        lease.consume()
        return Envelope(
            run_id=run_id,
            step=step,
            kind="cap",
            verdict="allowed",
            ok=True,
            capability=capability,
            tier=tier,
            audit_id=f"audit-{step}",
            data=dict(self.data),
        )


class _RecordingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: dict[str, Any]

    def log_message(self, *args: Any) -> None:
        return None

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else None
        except json.JSONDecodeError:
            return {"raw": raw.decode("utf-8", "replace")}

    def _send(self, status: int, payload: Any, headers: Optional[dict[str, str]] = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        state = type(self).state
        record = {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": self._body()}
        state.setdefault("requests", []).append(record)
        response = state.get("responder")
        status, payload = response(record) if callable(response) else (state.get("status", 200), state.get("payload", {"ok": True}))
        self._send(status, payload, state.get("headers"))

    def do_GET(self) -> None:
        state = type(self).state
        state.setdefault("requests", []).append(
            {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": None}
        )
        if self.path == "/v1/models":
            self._send(200, {"data": [{"id": state.get("model_id", "qwen")}]})
            return
        self._send(404, {"error": "not_found"})


class FakeHttpServer:
    """Loopback server recording requests. ``state['responder']`` may compute (status, payload)."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {"requests": []}
        handler = type("StateHandler", (_RecordingHandler,), {"state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)

    def __enter__(self) -> FakeHttpServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self.state["requests"]


def fake_gate_responder(record: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    body = record["body"] or {}
    return 200, {
        "ok": True,
        "audit_id": "gate-audit-1",
        "data": {"capability": body.get("capability"), "echo": body.get("params"), "api_key": "sk-abcdefghijklmnopqrstuvwxyz0123"},
    }


def openai_responder(replies: list[str]):
    """``/v1/chat/completions`` responder yielding scripted assistant contents."""
    counter = {"n": 0}

    def respond(record: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        index = min(counter["n"], len(replies) - 1)
        counter["n"] += 1
        return 200, {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": "qwen-served",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": replies[index]}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    return respond
