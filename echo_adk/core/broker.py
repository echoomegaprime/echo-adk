"""Capability calls through the Echo gate (``POST {ECHO_GATE_URL}/sdk/invoke``).

* The API key is read from the environment variable named by ``ECHO_ADK_GATE_KEY_ENV`` (default
  ``ECHO_ADK_GATE_KEY``) **at call time**. It is never stored on the object, never logged, never written.
* The local Lease check runs before any network call (R3).
* Every response is wrapped in an :class:`Envelope` with ``untrusted:true`` and secrets redacted.
* The gate defaults to loopback only; a remote gate needs ``ECHO_ADK_GATE_ALLOW_REMOTE=1`` (or
  ``allow_remote=True``) because the key then leaves the host.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Optional, Protocol

from .log import get_logger
from .redact import redact_value
from .types import CAP_ID, Envelope, Lease, ValidationError

log = get_logger("broker")

DEFAULT_GATE_URL = "http://127.0.0.1:8000"
DEFAULT_KEY_ENV = "ECHO_ADK_GATE_KEY"
GATE_PROVIDER = "echo-gate"
MAX_RESPONSE_BYTES = 1024 * 1024
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")


class BrokerError(Exception):
    """A capability call could not be completed. ``code`` is ``unavailable`` or ``invalid``."""

    def __init__(self, code: str, message: str, *, provider: Optional[str] = None) -> None:
        self.code = code
        self.provider = provider
        super().__init__(message)


class Broker(Protocol):
    def invoke(
        self,
        capability: str,
        params: Mapping[str, Any],
        lease: Optional[Lease],
        *,
        run_id: str,
        step: int,
        tier: Optional[int],
        context: Optional[Mapping[str, Any]] = None,
    ) -> Envelope: ...


def validate_gate_url(url: Any, *, allow_remote: bool) -> str:
    if not isinstance(url, str) or len(url) > 300:
        raise ValidationError("ECHO_GATE_URL must be an http(s) origin")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValidationError("ECHO_GATE_URL must be a credential-free http(s) origin")
    if parts.path not in ("", "/"):
        raise ValidationError("ECHO_GATE_URL must be an origin without a path")
    if parts.hostname.lower() not in LOOPBACK_HOSTS and not allow_remote:
        raise ValidationError("ECHO_GATE_URL is not loopback; set ECHO_ADK_GATE_ALLOW_REMOTE=1 to allow a remote gate")
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


class GateBroker:
    """Invoke capabilities on the Echo SDK gate with the envelope ``{envelope_version:1, capability, params, context}``."""

    def __init__(
        self,
        gate_url: Optional[str] = None,
        *,
        key_env: Optional[str] = None,
        timeout_s: float = 30.0,
        allow_remote: Optional[bool] = None,
        max_bytes: int = MAX_RESPONSE_BYTES,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        remote = _truthy(os.environ.get("ECHO_ADK_GATE_ALLOW_REMOTE")) if allow_remote is None else allow_remote
        self.gate_url = validate_gate_url(gate_url or os.environ.get("ECHO_GATE_URL") or DEFAULT_GATE_URL, allow_remote=remote)
        self.key_env = key_env or os.environ.get("ECHO_ADK_GATE_KEY_ENV") or DEFAULT_KEY_ENV
        if not self.key_env.replace("_", "").isalnum():
            raise ValidationError("ECHO_ADK_GATE_KEY_ENV must name an environment variable")
        if not isinstance(timeout_s, (int, float)) or timeout_s <= 0 or timeout_s > 600:
            raise ValidationError("Invalid broker timeout")
        self.timeout_s = float(timeout_s)
        self.max_bytes = max_bytes
        self._open = opener or urllib.request.urlopen

    @property
    def provider(self) -> str:
        return GATE_PROVIDER

    def key_present(self) -> bool:
        return bool(os.environ.get(self.key_env))

    def invoke(
        self,
        capability: str,
        params: Mapping[str, Any],
        lease: Optional[Lease],
        *,
        run_id: str,
        step: int,
        tier: Optional[int],
        context: Optional[Mapping[str, Any]] = None,
    ) -> Envelope:
        if not isinstance(capability, str) or not CAP_ID.match(capability):
            raise BrokerError("invalid", "capability id is not valid")
        # R3: the local lease check happens before any network call.
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
                how=f"re-mint the Lease with {capability} listed in scopes",
                data={"error": "lease does not permit this capability"},
            )
        if lease.expired() or lease.exhausted():
            return Envelope(
                run_id=run_id,
                step=step,
                kind="cap",
                verdict="gated",
                ok=False,
                capability=capability,
                tier=tier,
                needs="lease renewal",
                how="mint a fresh Lease",
                data={"error": "lease expired or exhausted"},
            )
        key = os.environ.get(self.key_env)
        if not key:
            raise BrokerError("unavailable", f"gate key env var {self.key_env} is not set", provider=GATE_PROVIDER)

        envelope = {
            "envelope_version": 1,
            "capability": capability,
            "params": dict(params or {}),
            "context": {"run_id": run_id, "step": step, "source": "echo-adk", **(dict(context or {}))},
        }
        request = urllib.request.Request(f"{self.gate_url}/sdk/invoke", data=json.dumps(envelope).encode("utf-8"), method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        request.add_header("X-Echo-API-Key", key)
        del key
        started = time.monotonic()
        status = 0
        raw = b""
        audit_id: Optional[str] = None
        try:
            with self._open(request, timeout=self.timeout_s) as response:
                status = int(getattr(response, "status", 200))
                raw = response.read(self.max_bytes + 1)
                headers = getattr(response, "headers", None)
                if headers is not None:
                    audit_id = headers.get("X-Echo-Audit-Id") or headers.get("X-Request-Id")
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                raw = exc.read(self.max_bytes + 1)
            except Exception:
                raw = b""
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            log.debug("gate_unreachable", capability=capability, error=type(exc).__name__)
            raise BrokerError("unavailable", "the Echo gate is unreachable or timed out", provider=GATE_PROVIDER) from None
        duration_ms = int((time.monotonic() - started) * 1000)
        if len(raw) > self.max_bytes:
            raise BrokerError("unavailable", "gate response exceeded the 1 MiB cap", provider=GATE_PROVIDER)
        try:
            body: Any = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {"raw": raw.decode("utf-8", "replace")[:2000]}
        lease.consume()
        data, redacted = redact_value(body if isinstance(body, dict) else {"result": body})
        ok = 200 <= status < 300 and (not isinstance(data, dict) or data.get("ok", True) is not False)
        if isinstance(data, dict):
            for key_name in ("audit_id", "run_id", "request_id"):
                candidate = data.get(key_name)
                if isinstance(candidate, str) and candidate and key_name != "run_id":
                    audit_id = audit_id or candidate
            data.setdefault("http_status", status)
        log.debug("gate_invoke", capability=capability, status=status, ok=ok, duration_ms=duration_ms, redacted=redacted)
        return Envelope(
            run_id=run_id,
            step=step,
            kind="cap",
            verdict="allowed",
            ok=ok,
            capability=capability,
            tier=tier,
            redacted=redacted,
            audit_id=audit_id if isinstance(audit_id, str) and len(audit_id) <= 128 else None,
            duration_ms=duration_ms,
            data=data,
        )
