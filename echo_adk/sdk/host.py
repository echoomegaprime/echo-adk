"""Installable adapter inside the real ECHO SDK FastAPI host.

Run the existing SDK app through ``echo_adk.sdk.host:create_app --factory``.
Actual SDK pre-dispatch hooks run after gate/HMAC/policy admission; its verified
local router transport uses the same process through httpx.ASGITransport. The
one-use context below is therefore unavailable to direct HTTP target requests.
No HMAC or owner claim is accepted from application parameters or identity hints.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import time
import urllib.parse
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Optional

from ..api.server import is_loopback_host
from .manifest import PROPOSED_IDS, ROUTE_PREFIX
from .operations import MAX_BYTES, OPERATIONS, InvalidParams, SdkOperations, failure, validate


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    ).hexdigest()


def _key_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class InvocationFrame:
    principal: Optional[str]
    capability: Optional[str] = None
    params: Optional[str] = None
    armed_at: float = 0
    consumed: bool = False
    closed: bool = False


_FRAME: ContextVar[Optional[InvocationFrame]] = ContextVar("echo_adk_sdk_invocation", default=None)


def _header(scope: dict[str, Any], name: bytes) -> Optional[str]:
    values = [value for key, value in scope.get("headers", []) if key.lower() == name]
    if len(values) != 1 or not 0 < len(values[0]) <= 4096:
        return None
    return values[0].decode("latin-1")


class InvocationMiddleware:
    """Reset each outer/nested SDK invocation frame even when any hook refuses."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path", "").rstrip("/") != "/sdk/invoke":
            await self.app(scope, receive, send)
            return
        key = _header(scope, b"x-echo-api-key")
        frame = InvocationFrame(_key_digest(key) if key else None)
        token = _FRAME.set(frame)
        try:
            await self.app(scope, receive, send)
        finally:
            # A task spawned during dispatch can inherit this object even after
            # the parent's ContextVar resets. Revoke its unused proof as well.
            frame.closed = True
            _FRAME.reset(token)


def install(app: Any, *, owner: str, worker_origin: str, sdk: Any = None, projector: Any = None, scope_match: Any = None) -> Any:
    """Mount on the SDK's actual app before startup; dependencies default to real host helpers.

    Injectable helper arguments support offline host-contract tests. They are
    construction-time Python objects, never application/HTTP parameters.
    """
    from fastapi import APIRouter, HTTPException, Request
    from fastapi.responses import JSONResponse

    if not isinstance(owner, str) or not owner.strip() or len(owner) > 320:
        raise ValueError("ECHO_ADK_OWNER must explicitly name the verified SDK key owner")
    if getattr(app.state, "echo_adk_sdk_installed", False):
        raise RuntimeError("ADK SDK adapter is already installed")
    if app.middleware_stack is not None:
        raise RuntimeError("Install the ADK SDK adapter before the SDK host starts serving requests")
    sdk = sdk or importlib.import_module("routers.sdk_invoke")
    projector = projector or importlib.import_module("routers._dispatcher_projection").project_envelope
    scope_match = scope_match or importlib.import_module("routers._scope_match").scope_match
    authenticate = sdk._authenticate
    register = sdk.register_pre_dispatch_hook
    if not all(callable(value) for value in (projector, scope_match, authenticate, register)):
        raise RuntimeError("SDK host authentication/projection/hook contract is unavailable")
    authorized = object()
    operations = SdkOperations(worker_origin, authorize=lambda _operation, context: context is authorized)
    reverse = {capability: operation for operation, capability in PROPOSED_IDS.items()}

    async def pre_dispatch(envelope: Any, cap_row: dict[str, Any], _actor: Any) -> None:
        capability = cap_row.get("id")
        operation = reverse.get(capability)
        if operation is None:
            return
        frame = _FRAME.get()
        if frame is None or frame.principal is None or frame.closed or frame.consumed or frame.capability is not None:
            return
        target = urllib.parse.urlsplit(str(cap_row.get("target_url", "")))
        # Registration mistakes must not weaken the gate or redirect this proof
        # toward an unrelated route/process. Remote router targets cannot carry it.
        if (
            cap_row.get("danger_tier") != 2
            or cap_row.get("required_scope") != "tier:2"
            or cap_row.get("handler_kind") != "router"
            or cap_row.get("target_method") != "POST"
            or (cap_row.get("args_mode") or "json_body") != "json_body"
            or target.scheme != "http"
            or not target.hostname
            or not is_loopback_host(target.hostname)
            or target.username
            or target.password
            or target.query
            or target.fragment
            or target.path != ROUTE_PREFIX + "/" + operation
        ):
            return
        try:
            params = projector(envelope, cap_row)
            validate(params, OPERATIONS[operation].schema)
            digest = _digest(params)
        except (InvalidParams, TypeError, ValueError, UnicodeError, RecursionError):
            return
        frame.capability, frame.params, frame.armed_at = capability, digest, time.monotonic()

    router = APIRouter()

    # Avoid local forward-annotation resolution by FastAPI on the host's Python version.
    async def invoke(operation: str, request: Any) -> Any:
        def error(status: int, code: str, message: str) -> Any:
            result = failure(operation, status, code, message)
            return JSONResponse(result.body, status_code=result.status_code)

        if operation not in OPERATIONS:
            return error(404, "not_found", "Unknown ADK SDK operation")
        frame = _FRAME.get()
        key = _header(request.scope, b"x-echo-api-key")
        capability = _header(request.scope, b"x-echo-capability")
        if (
            frame is None
            or frame.closed
            or frame.consumed
            or frame.capability != PROPOSED_IDS[operation]
            or capability != frame.capability
            or not key
            or _key_digest(key) != frame.principal
            or not 0 <= time.monotonic() - frame.armed_at <= 30
        ):
            return error(403, "denied", "A current, single-use SDK gate admission is required")
        # Consume synchronously before authentication or worker I/O can yield.
        frame.consumed = True
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BYTES:
                return error(413, "invalid", "Application parameters exceed 1 MiB")
        try:
            params = json.loads(raw.decode("utf-8"))
            if _digest(params) != frame.params:
                return error(403, "denied", "Application parameters differ from the admitted SDK request")
        except (ValueError, UnicodeError, RecursionError):
            return error(400, "invalid", "Application parameters must be valid JSON")
        # Reauthentication derives owner/scopes from the actual key table. The
        # forwarded Commander email/capability labels alone confer no authority.
        try:
            info = await authenticate(key)
        except HTTPException as exc:
            status = exc.status_code if exc.status_code in (401, 403, 429) else 503
            return error(status, "denied" if status in (401, 403) else "unavailable", "SDK key authentication was refused")
        except Exception:
            return error(503, "unavailable", "SDK key authentication could not be verified")
        if (
            not isinstance(info, dict)
            or info.get("active") is not True
            or info.get("owner") != owner
            or not scope_match(info.get("scopes", []), "tier:2")
        ):
            return error(403, "denied", "Verified SDK owner and tier:2 scope are required")
        result = await asyncio.to_thread(operations.invoke, operation, params, context=authorized)
        return JSONResponse(result.body, status_code=result.status_code)

    invoke.__annotations__["request"] = Request
    router.add_api_route(ROUTE_PREFIX + "/{operation}", invoke, methods=["POST"])
    app.add_middleware(InvocationMiddleware)
    app.include_router(router)
    register(pre_dispatch)
    app.state.echo_adk_sdk_installed = True
    return app


def create_app() -> Any:
    """Uvicorn factory wrapping the existing SDK host, with no policy edits."""
    owner = os.environ.get("ECHO_ADK_OWNER", "")
    worker_origin = os.environ.get("ECHO_ADK_WORKER_ORIGIN", "")
    if not owner or not worker_origin:
        raise RuntimeError("Set ECHO_ADK_OWNER and ECHO_ADK_WORKER_ORIGIN before starting the SDK host adapter")
    app = importlib.import_module("main").app
    return install(app, owner=owner, worker_origin=worker_origin)
