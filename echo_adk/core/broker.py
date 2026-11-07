"""Capability calls through the Echo gate (``POST {ECHO_GATE_URL}/sdk/invoke``).

* The API key is read from the environment variable named by ``ECHO_ADK_GATE_KEY_ENV`` (default
  ``ECHO_ADK_GATE_KEY``) **at call time**. It is never stored on the object, never logged, never written.
* The local Lease check runs before any network call (R3).
* Every response is wrapped in an :class:`Envelope` with ``untrusted:true`` and secrets redacted.
* The gate defaults to loopback only; a remote gate needs ``ECHO_ADK_GATE_ALLOW_REMOTE=1`` (or
  ``allow_remote=True``) because the key then leaves the host.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Optional, Protocol

from .log import get_logger
from .policy import CapabilityRegistry, validate_params
from .redact import redact_value
from .types import CAP_ID, Capability, Envelope, Lease, ValidationError

log = get_logger("broker")

DEFAULT_GATE_URL = "http://127.0.0.1:8000"
DEFAULT_KEY_ENV = "ECHO_ADK_GATE_KEY"
DEFAULT_HMAC_ENV = "ECHO_ADK_GATE_HMAC_SECRET"
GATE_PROVIDER = "echo-gate"
MAX_RESPONSE_BYTES = 1024 * 1024
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")
SUCCESS_STATUSES = ("ok", "success", "succeeded", "completed", "done")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never forward a gate credential to another origin


def _scrub_credentials(value: Any, credentials: tuple[str, ...], *, depth: int = 0) -> tuple[Any, int]:
    """Remove request credentials after JSON decoding, including object keys.

    The transport caps input at 1 MiB; this additional depth bound prevents
    arbitrarily nested untrusted values from reaching model/ledger consumers.
    Credentials remain request-local and are never kept on the broker object.
    """
    if depth > 64:
        return "[REDACTED]", 1
    if isinstance(value, str):
        count = 0
        for credential in credentials:
            count += value.count(credential)
            value = value.replace(credential, "[REDACTED]")
        return value, count
    if isinstance(value, dict):
        output, count = {}, 0
        for key, item in value.items():
            clean_key, key_count = _scrub_credentials(key, credentials, depth=depth + 1)
            clean_item, item_count = _scrub_credentials(item, credentials, depth=depth + 1)
            output[clean_key] = clean_item
            count += key_count + item_count
        return output, count
    if isinstance(value, list):
        output, count = [], 0
        for item in value:
            clean_item, item_count = _scrub_credentials(item, credentials, depth=depth + 1)
            output.append(clean_item)
            count += item_count
        return output, count
    return value, 0


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
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        raise ValidationError("ECHO_GATE_URL has an invalid port") from None
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}{port}"


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
        registry: Optional[CapabilityRegistry] = None,
        verify_catalog: bool = True,
        hmac_env: Optional[str] = None,
    ) -> None:
        remote = _truthy(os.environ.get("ECHO_ADK_GATE_ALLOW_REMOTE")) if allow_remote is None else allow_remote
        self.gate_url = validate_gate_url(gate_url or os.environ.get("ECHO_GATE_URL") or DEFAULT_GATE_URL, allow_remote=remote)
        self.key_env = key_env or os.environ.get("ECHO_ADK_GATE_KEY_ENV") or DEFAULT_KEY_ENV
        self.hmac_env = hmac_env or os.environ.get("ECHO_ADK_GATE_HMAC_SECRET_ENV") or DEFAULT_HMAC_ENV
        if not self.key_env.replace("_", "").isalnum():
            raise ValidationError("ECHO_ADK_GATE_KEY_ENV must name an environment variable")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", self.hmac_env):
            raise ValidationError("ECHO_ADK_GATE_HMAC_SECRET_ENV must name an environment variable")
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s)
            or not 0 < timeout_s <= 600
        ):
            raise ValidationError("Invalid broker timeout")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_RESPONSE_BYTES:
            raise ValidationError("Invalid broker response cap")
        self.timeout_s = float(timeout_s)
        self.max_bytes = max_bytes
        self._open = opener or urllib.request.build_opener(_NoRedirect()).open
        self.registry = registry if registry is not None else CapabilityRegistry.builtin()
        # Disabling discovery is dependency injection for a pinned offline fixture;
        # production construction leaves the live active-catalog check enabled.
        self.verify_catalog = verify_catalog
        self._catalog: dict[str, tuple[float, Capability]] = {}
        self._catalog_lock = threading.RLock()

    @property
    def provider(self) -> str:
        return GATE_PROVIDER

    def key_present(self) -> bool:
        return bool(os.environ.get(self.key_env))

    def hydrate(self, capability_ids: list[str] | tuple[str, ...]) -> None:
        """Resolve missing definitions through the authenticated active catalog."""
        missing = [cap_id for cap_id in capability_ids if cap_id not in self.registry]
        if len(missing) > 64:
            raise BrokerError("invalid", "At most 64 missing capability IDs may be resolved at once", provider=GATE_PROVIDER)
        for cap_id in missing:
            self.registry.register(self.resolve_capability(cap_id))

    def _request(
        self,
        capability: str,
        params: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        sign: bool = False,
        timeout_s: Optional[float] = None,
    ) -> tuple[bool, dict[str, Any], Optional[str], int, int]:
        wait_s = self.timeout_s if timeout_s is None else min(self.timeout_s, timeout_s)
        if wait_s <= 0:
            raise BrokerError("unavailable", "SDK operation deadline expired; outcome unknown", provider=GATE_PROVIDER)
        key = os.environ.get(self.key_env)
        if not key:
            raise BrokerError("unavailable", f"gate key env var {self.key_env} is not set", provider=GATE_PROVIDER)
        scrub_values = [key]
        secret_hex = os.environ.get(self.hmac_env, "")
        if len(secret_hex) >= 6:
            # Known configured secrets stay private even on unsigned reads.
            scrub_values.append(secret_hex)
            if re.fullmatch(r"[0-9a-fA-F]{64}", secret_hex):
                scrub_values.extend((secret_hex.lower(), secret_hex.upper()))
        envelope = {"envelope_version": 1, "capability": capability, "params": dict(params), "context": dict(context)}
        try:
            if sign:
                if not re.fullmatch(r"[0-9a-fA-F]{64}", secret_hex):
                    raise BrokerError("unavailable", f"configure the admitted per-key signer in {self.hmac_env}", provider=GATE_PROVIDER)
                # Keep the exact signer used for this request, even if its
                # configured environment value rotates while awaiting I/O.
                nonce, timestamp = secrets.token_hex(16), int(time.time())
                canonical_params = json.dumps(dict(params), sort_keys=True, separators=(",", ":"), allow_nan=False)
                canonical = f"v1|{key}|{capability}|{canonical_params}|{nonce}|{timestamp}".encode()
                signature = hmac.new(bytes.fromhex(secret_hex), canonical, hashlib.sha256).hexdigest()
                envelope["auth"] = {"hmac": signature, "nonce": nonce, "ts": timestamp}
                del canonical
            encoded = json.dumps(envelope, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            raise BrokerError("invalid", "gate request must contain JSON values", provider=GATE_PROVIDER) from None
        del secret_hex
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise BrokerError("invalid", "gate request exceeds the 1 MiB cap", provider=GATE_PROVIDER)
        request = urllib.request.Request(f"{self.gate_url}/sdk/invoke", data=encoded, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        request.add_header("X-Echo-API-Key", key)
        credentials = tuple(dict.fromkeys(scrub_values))
        del key
        started = time.monotonic()
        status, raw, audit_id = 0, b"", None
        try:
            with self._open(request, timeout=wait_s) as response:
                status = int(getattr(response, "status", 200))
                raw = response.read(self.max_bytes + 1)
                headers = getattr(response, "headers", None)
                if headers is not None:
                    audit_id = headers.get("X-Echo-Audit-Id") or headers.get("X-Request-Id")
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                raw = exc.read(self.max_bytes + 1)
            except (OSError, ValueError):
                raw = b""
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise BrokerError("unavailable", "the Echo gate is unreachable or timed out; outcome unknown", provider=GATE_PROVIDER) from None
        duration_ms = int((time.monotonic() - started) * 1000)
        if time.monotonic() - started > wait_s:
            raise BrokerError("unavailable", "SDK operation timed out; outcome unknown", provider=GATE_PROVIDER)
        if len(raw) > self.max_bytes:
            raise BrokerError("unavailable", "gate response exceeded the 1 MiB cap; outcome unknown", provider=GATE_PROVIDER)
        transport_redactions = 0
        try:
            raw_text = raw.decode("utf-8")
            for sensitive in credentials:
                if audit_id and sensitive in audit_id:
                    audit_id = None
                    transport_redactions += 1
                if len(sensitive) >= 6:
                    escaped = json.dumps(sensitive)[1:-1]
                    transport_redactions += raw_text.count(escaped)
                    raw_text = raw_text.replace(escaped, "[REDACTED]")
            body = json.loads(raw_text) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            body = None
        if not isinstance(body, dict):
            return (
                False,
                {"error": "gate returned an unrecognized JSON envelope", "http_status": status},
                audit_id,
                duration_ms,
                transport_redactions,
            )
        data = dict(body)
        data["http_status"] = status
        ok = (
            200 <= status < 300
            and body.get("status") in SUCCESS_STATUSES
            and body.get("ok", True) is True
            and body.get("success", True) is True
        )
        result = body.get("result")
        if not isinstance(result, dict):
            ok = False
        else:
            target_status = result.get("status_code")
            if isinstance(target_status, bool) or not isinstance(target_status, int) or not 200 <= target_status < 300:
                ok = False
            target = result.get("body")
            if isinstance(target, str):
                try:
                    target = json.loads(target)
                except (json.JSONDecodeError, RecursionError):
                    target = None
                # Never retain an opaque string body after a failed second
                # decode: its outcome is invalid and escaped data is untrusted.
                data["result"] = {**data["result"], "body": target}
            if not isinstance(target, (dict, list)):
                ok = False
            elif isinstance(target, dict) and (target.get("ok", True) is not True or target.get("success", True) is not True):
                ok = False
            if target is not None:
                data["result"] = {**data["result"], "body": target}
        # Exact raw-text replacement alone misses equivalent Unicode escapes.
        # Scrub after both outer and optional target-body JSON decoding.
        data, decoded_redactions = _scrub_credentials(data, credentials)
        transport_redactions += decoded_redactions
        # The SDK's run_id/trace are retained as routing evidence in data. They
        # are not promoted into a signed audit_id or a deployment certification.
        return ok, data, audit_id, duration_ms, transport_redactions

    def resolve_capability(self, capability: str) -> Capability:
        """Read current metadata and prove exact active catalog membership.

        Detail alone can expose inactive rows. Both calls are read-only and use
        this broker's configured credential; this method never mints a token.
        Successful entries are cached for at most 60 seconds, with a 256-ID cap.
        """
        if not isinstance(capability, str) or not CAP_ID.fullmatch(capability):
            raise BrokerError("invalid", "capability id is not valid", provider=GATE_PROVIDER)
        if not self.verify_catalog:
            cap = self.registry.get(capability)
            if cap is None:
                raise BrokerError("invalid", f"capability {capability} is not in the pinned registry", provider=GATE_PROVIDER)
            return cap
        with self._catalog_lock:
            previous = self._catalog.get(capability)
            if previous and previous[0] > time.monotonic():
                return previous[1]
            context = {"source": "echo-adk-catalog"}
            ok, detail, _, _, _ = self._request("echo.caps.detail", {"command": "detail", "id": capability}, context)
            if not ok:
                raise BrokerError("unavailable", f"live SDK detail could not verify {capability}", provider=GATE_PROVIDER)
            payload = redact_value(detail["result"]["body"])[0]
            try:
                cap = Capability.from_caps_detail(payload)
            except (ValidationError, TypeError, ValueError, AttributeError):
                raise BrokerError("invalid", f"SDK detail for {capability} has incomplete metadata", provider=GATE_PROVIDER) from None
            if cap.id != capability or not cap.scope or cap.scope == "unknown" or not cap.verb:
                raise BrokerError(
                    "invalid", f"SDK detail did not verify the exact id, scope and verb for {capability}", provider=GATE_PROVIDER
                )
            if cap.id == "echo.shell.run" and not cap.input_schema.get("properties"):
                # Verified sovereign_admin_router.ShellReq.opts/shell_run uses
                # cmd. Catalog help hides command/options and omits this alias.
                cap = Capability(
                    cap.id,
                    cap.tier,
                    cap.scope,
                    {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
                    cap.health,
                    cap.verb,
                    cap.description,
                )
            ok, search, _, _, _ = self._request("echo.caps.search", {"command": "search", "q": capability, "limit": 100}, context)
            rows = search.get("result", {}).get("body", {}) if ok else {}
            rows = rows.get("caps") if isinstance(rows, dict) else None
            active = (
                next((row for row in rows if isinstance(row, dict) and row.get("id") == capability), None)
                if isinstance(rows, list)
                else None
            )
            if not ok or not isinstance(rows, list) or active is None:
                raise BrokerError("unavailable", f"SDK capability {capability} is not verified active", provider=GATE_PROVIDER)
            if active.get("tier", cap.tier) != cap.tier or active.get("scope", cap.scope) != cap.scope:
                raise BrokerError(
                    "unavailable", f"SDK catalog changed while resolving {capability}; retry discovery", provider=GATE_PROVIDER
                )
            if cap.health == "red":
                raise BrokerError("unavailable", f"SDK capability {capability} reports unhealthy", provider=GATE_PROVIDER)
            if len(self._catalog) >= 256:
                self._catalog.pop(next(iter(self._catalog)))
            self._catalog[capability] = (time.monotonic() + 60.0, cap)
            self.registry.register(cap)
            return cap

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
        if lease.run_id != run_id:
            raise BrokerError("invalid", "lease.run_id does not match the run", provider=GATE_PROVIDER)
        if not isinstance(params, Mapping):
            raise BrokerError("invalid", "capability params must be an object", provider=GATE_PROVIDER)
        cap = self.resolve_capability(capability)
        if cap.tier != tier:
            return Envelope(
                run_id=run_id,
                step=step,
                kind="cap",
                verdict="denied",
                ok=False,
                capability=capability,
                tier=cap.tier,
                data={"error": "live capability tier changed; refresh the registry before running", "code": "catalog_changed"},
            )
        if cap.tier >= 2 and not lease.grant_ref:
            return Envelope(
                run_id=run_id,
                step=step,
                kind="cap",
                verdict="gated",
                ok=False,
                capability=capability,
                tier=cap.tier,
                needs=f"tier-{cap.tier} grant",
                how="obtain an admitted signed grant and a credential with the exact scope",
                data={"error": "privileged capability requires a grant"},
            )
        if lease.expired() or lease.exhausted():
            return Envelope(
                run_id=run_id,
                step=step,
                kind="cap",
                verdict="gated",
                ok=False,
                capability=capability,
                tier=cap.tier,
                needs="lease renewal",
                how="mint a fresh Lease",
                data={"error": "lease expired during catalog discovery"},
            )
        params = dict(params)
        if capability == "echo.shell.run" and "command" in params:
            if "cmd" in params or "command_str" in params:
                raise BrokerError("invalid", "shell command has conflicting fields", provider=GATE_PROVIDER)
            params["cmd"] = params.pop("command")
        problem = validate_params(params, cap.input_schema)
        if problem:
            raise BrokerError("invalid", problem, provider=GATE_PROVIDER)
        # Generic SDK handlers take flat fields. Legacy catalog routes also
        # accept {command, options}, but that wrapper fails strict handlers.
        # Keep the transport verb separate from the source-verified shell cmd.
        if "command" in params and params["command"] != cap.verb:
            raise BrokerError("invalid", "params may not override the SDK command", provider=GATE_PROVIDER)
        wire_params = {"command": cap.verb, **params}
        invoked_at = time.monotonic()
        ok, body, audit_id, duration_ms, transport_redactions = self._request(
            capability, wire_params, {"source": "echo-adk", **dict(context or {}), "run_id": run_id, "step": step}
        )
        challenge = body.get("detail")
        if (
            cap.tier >= 2
            and body.get("http_status") == 423
            and isinstance(challenge, dict)
            and challenge.get("error") == "hmac_required_for_tier2_plus"
            and challenge.get("capability") == capability
        ):
            # This exact gate challenge occurs before dispatch. It is the only
            # automatic retry: one signed attempt, never a retry of an unknown
            # write outcome. The trusted host supplies a per-key secret through
            # its secret store; ADK does not query credential tables or mint keys.
            ok, body, audit_id, signed_ms, signed_redactions = self._request(
                capability,
                wire_params,
                {"source": "echo-adk", **dict(context or {}), "run_id": run_id, "step": step},
                sign=True,
                timeout_s=self.timeout_s - (time.monotonic() - invoked_at),
            )
            duration_ms += signed_ms
            transport_redactions += signed_redactions
        lease.consume()
        data, redacted = redact_value(body)
        redacted += transport_redactions
        log.debug("gate_invoke", capability=capability, status=data.get("http_status"), ok=ok, duration_ms=duration_ms, redacted=redacted)
        return Envelope(
            run_id=run_id,
            step=step,
            kind="cap",
            verdict="allowed",
            ok=ok,
            capability=capability,
            tier=cap.tier,
            redacted=redacted,
            audit_id=audit_id if isinstance(audit_id, str) and len(audit_id) <= 128 else None,
            duration_ms=duration_ms,
            data=data,
        )
