"""Strict SDK operation projection onto the real, governed v2 HTTP API.

Application parameters are flat: the SDK gate removes its transport ``command``
before dispatch using each closed input schema. Credentials and trusted caller
context are never application parameters. No operation bypasses the HTTP service,
retries an uncertain mutation, redirects, or chooses a caller-supplied destination.
"""

from __future__ import annotations

import copy
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional

from ..api.server import is_loopback_host
from ..core.redact import redact, redact_value

MAX_BYTES = 1024 * 1024
ID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
NAME = {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{1,62}$", "maxLength": 63}
WORD = {"type": "string", "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$", "maxLength": 64}
CAP = {"type": "string", "pattern": "^echo\\.[a-z0-9_]+(?:\\.[a-z0-9_]+)*$", "maxLength": 128}
TEXT = {"type": "string", "minLength": 1, "maxLength": 12000}


def obj(properties: dict[str, Any], required: tuple[str, ...] = (), *, minimum: int = 0) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": list(required), "minProperties": minimum}


def array(items: dict[str, Any], maximum: int, *, minimum: int = 0, unique: bool = False) -> dict[str, Any]:
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum, **({"uniqueItems": True} if unique else {})}


def integer(low: int, high: int) -> dict[str, Any]:
    return {"type": "integer", "minimum": low, "maximum": high}


def nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [{"type": "null"}, schema]}


BUDGET = obj({"max_calls": integer(1, 120), "max_cost_usd": {"type": "number", "minimum": 0, "maximum": 5}, "max_wall_s": integer(1, 4800)})
LEASE = obj({"scopes": array(CAP, 256), "ttl_s": integer(1, 4800), "max_uses": integer(1, 120)})
RUN_LEASE = obj(LEASE["properties"], ("scopes",))
MISSED = {"type": "string", "enum": ["skip", "once"]}
SCHEDULE = nullable(
    {
        "anyOf": [
            obj({"every_s": integer(60, 604800), "missed": MISSED}, ("every_s",)),
            obj(
                {
                    "at": {"type": "string", "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"},
                    "tz": {"type": "string", "minLength": 1, "maxLength": 100},
                    "weekdays": array(integer(1, 7), 7, minimum=1, unique=True),
                    "missed": MISSED,
                },
                ("at", "tz", "weekdays"),
            ),
            obj(
                {
                    "once_at": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T([01][0-9]|2[0-3]):[0-5][0-9]$"},
                    "tz": {"type": "string", "minLength": 1, "maxLength": 100},
                    "missed": MISSED,
                },
                ("once_at", "tz"),
            ),
            obj(
                {
                    "at": {"type": "string", "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"},
                    "tz": {"type": "string", "minLength": 1, "maxLength": 100},
                    "monthdays": array(integer(1, 31), 31, minimum=1, unique=True),
                    "months": array(integer(1, 12), 12, minimum=1, unique=True),
                    "missed": MISSED,
                },
                ("at", "tz", "monthdays"),
            ),
            obj(
                {
                    "cron": {"type": "string", "minLength": 9, "maxLength": 256},
                    "tz": {"type": "string", "minLength": 1, "maxLength": 100},
                    "missed": MISSED,
                },
                ("cron", "tz"),
            ),
        ]
    }
)
AGENT_FIELDS = {
    "name": NAME,
    "role": WORD,
    "skills": array(WORD, 256, unique=True),
    "capabilities": array(CAP, 256, unique=True),
    "lane": obj(
        {
            "node": {"type": "string", "minLength": 1, "maxLength": 64},
            "model": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:/-]*$", "maxLength": 100},
            "budget": BUDGET,
            "concurrency": integer(1, 16),
        },
        ("node", "model"),
    ),
    "max_steps": integer(1, 64),
    "instruction": nullable(TEXT),
    "origin": obj({"by": {"type": "string", "enum": ["owner", "echo"]}, "prompt_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}),
}
BOT_MUTABLE = {
    "name": {"type": "string", "minLength": 1, "maxLength": 64},
    "mode": {"type": "string", "enum": ["personal", "team"]},
    "schedule": SCHEDULE,
    "instruction": nullable({"type": "string", "maxLength": 12000}),
    "plugins": array({"type": "string", "minLength": 1, "maxLength": 64}, 32),
    "daily_cost_cap_usd": {"type": "number", "minimum": 0, "maximum": 5},
    "lease": LEASE,
}


@dataclass(frozen=True)
class Operation:
    name: str
    method: str
    path: str
    schema: dict[str, Any]
    read_only: bool = False
    path_field: Optional[str] = None
    query_fields: tuple[str, ...] = ()
    description: str = ""

    def project(self, params: Mapping[str, Any]) -> tuple[str, Optional[dict[str, Any]]]:
        route = self.path
        if self.path_field:
            route = route.replace("{" + self.path_field + "}", urllib.parse.quote(str(params[self.path_field]), safe=""))
        query = {field: params[field] for field in self.query_fields if field in params}
        if query:
            route += "?" + urllib.parse.urlencode(query)
        body = None if self.method == "GET" else {key: value for key, value in params.items() if key != self.path_field}
        # Agent replacement's name belongs in both its fixed path and its body.
        if self.name == "agent.replace":
            body = dict(params)
        return route, body


_ROWS = [
    Operation("health", "GET", "/v2/health", obj({}), True, description="Read local API readiness and service budget accounting."),
    Operation("agent.list", "GET", "/v2/agents", obj({}), True, description="List actual agent definitions."),
    Operation(
        "agent.get", "GET", "/v2/agents/{name}", obj({"name": NAME}, ("name",)), True, "name", description="Read an agent definition."
    ),
    Operation(
        "agent.create",
        "POST",
        "/v2/agents",
        obj(AGENT_FIELDS, ("name", "role", "capabilities", "lane")),
        description="Create a validated agent definition.",
    ),
    Operation(
        "agent.replace",
        "PUT",
        "/v2/agents/{name}",
        obj(AGENT_FIELDS, ("name", "role", "capabilities", "lane")),
        path_field="name",
        description="Replace an existing agent definition.",
    ),
    Operation(
        "agent.delete",
        "DELETE",
        "/v2/agents/{name}",
        obj({"name": NAME}, ("name",)),
        path_field="name",
        description="Delete an unreferenced agent definition.",
    ),
    Operation(
        "run.create",
        "POST",
        "/v2/runs",
        obj({"agent": NAME, "prompt": TEXT, "budget": BUDGET, "lease": RUN_LEASE}, ("agent", "prompt")),
        description="Admit a budgeted, leased run through the governed runner.",
    ),
    Operation(
        "run.list",
        "GET",
        "/v2/runs",
        obj({"agent": NAME, "limit": integer(1, 200)}),
        True,
        query_fields=("agent", "limit"),
        description="List run summaries.",
    ),
    Operation(
        "run.get",
        "GET",
        "/v2/runs/{run_id}",
        obj({"run_id": ID}, ("run_id",)),
        True,
        "run_id",
        description="Read a run, proof and budget settlement receipt.",
    ),
    Operation(
        "run.stop",
        "POST",
        "/v2/runs/{run_id}/stop",
        obj({"run_id": ID}, ("run_id",)),
        path_field="run_id",
        description="Cancel queued work or request a cooperative stop.",
    ),
    Operation("bot.list", "GET", "/v2/bots", obj({}), True, description="List bots and their current schedules, leases and budgets."),
    Operation(
        "bot.create",
        "POST",
        "/v2/bots",
        obj({**BOT_MUTABLE, "agent": NAME}, ("name", "agent")),
        description="Create a paused bot with its own logical memory scope.",
    ),
    Operation(
        "bot.get", "GET", "/v2/bots/{bot_id}", obj({"bot_id": ID}, ("bot_id",)), True, "bot_id", description="Read current bot state."
    ),
    Operation(
        "bot.update",
        "PUT",
        "/v2/bots/{bot_id}",
        obj({"bot_id": ID, **BOT_MUTABLE}, ("bot_id",), minimum=2),
        path_field="bot_id",
        description="Update future configuration after pausing and draining the bot.",
    ),
    Operation(
        "bot.pause",
        "POST",
        "/v2/bots/{bot_id}/pause",
        obj({"bot_id": ID}, ("bot_id",)),
        path_field="bot_id",
        description="Pause future bot work.",
    ),
    Operation(
        "bot.resume",
        "POST",
        "/v2/bots/{bot_id}/resume",
        obj({"bot_id": ID}, ("bot_id",)),
        path_field="bot_id",
        description="Resume the bot's already configured work.",
    ),
    Operation(
        "bot.stop",
        "POST",
        "/v2/bots/{bot_id}/stop",
        obj({"bot_id": ID}, ("bot_id",)),
        path_field="bot_id",
        description="Pause the bot and request cancellation of its current work.",
    ),
    Operation(
        "bot.delete",
        "DELETE",
        "/v2/bots/{bot_id}",
        obj({"bot_id": ID}, ("bot_id",)),
        path_field="bot_id",
        description="Delete eligible bot-owned data while preserving fleet accounting.",
    ),
    Operation(
        "bot.message",
        "POST",
        "/v2/bots/{bot_id}/message",
        obj({"bot_id": ID, "text": TEXT}, ("bot_id", "text")),
        path_field="bot_id",
        description="Admit a bot inbox message without resuming a paused bot.",
    ),
    Operation(
        "bot.messages",
        "GET",
        "/v2/bots/{bot_id}/messages",
        obj({"bot_id": ID, "since": ID, "limit": integer(1, 200)}, ("bot_id",)),
        True,
        "bot_id",
        ("since", "limit"),
        "Read a page of untrusted inbox messages and event provenance.",
    ),
    Operation(
        "bot.event",
        "POST",
        "/v2/bots/{bot_id}/events",
        obj(
            {
                "bot_id": ID,
                "event_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"},
                "source": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_.-]{0,63}$"},
                "text": {**TEXT, "maxLength": 10000},
            },
            ("bot_id", "event_id", "source", "text"),
        ),
        path_field="bot_id",
        description="Admit one bounded, durable, idempotent untrusted event.",
    ),
    Operation(
        "group.create",
        "POST",
        "/v2/groups",
        obj(
            {"name": {"type": "string", "minLength": 1, "maxLength": 64}, "bots": array(ID, 6, minimum=2, unique=True), "prompt": TEXT},
            ("name", "bots", "prompt"),
        ),
        description="Reserve 2–6 bots and admit a bounded sequential group task.",
    ),
    Operation(
        "group.get",
        "GET",
        "/v2/groups/{group_id}",
        obj({"group_id": ID}, ("group_id",)),
        True,
        "group_id",
        description="Read a group's actual lifecycle and shared transcript.",
    ),
]
from .workspace import operations as workspace_operations  # noqa: E402

_ROWS.extend(workspace_operations())
OPERATIONS = {operation.name: operation for operation in _ROWS}


def operation_catalog() -> list[dict[str, Any]]:
    """Application operation specs, not a claim that any SDK capability is registered."""
    return [
        {
            "operation": op.name,
            "method": op.method,
            "path": op.path,
            "read_only": op.read_only,
            "description": op.description,
            "input_schema_json": copy.deepcopy(op.schema),
        }
        for op in OPERATIONS.values()
    ]


def valid_payload(operation: str, params: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    """A successful transport must still carry the actual operation's receipt."""
    from .workspace import valid_payload as workspace_payload

    workspace_result = workspace_payload(operation, dict(params), dict(payload))
    if workspace_result is not None:
        return workspace_result

    def hex_id(value: Any) -> bool:
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None

    run_status = {"queued", "running", "gated", "done", "failed"}
    status = payload.get("status")
    valid_status = isinstance(status, str)
    if operation == "health":
        return payload.get("ok") is True and payload.get("profile") == "ECHO_ADK" and payload.get("contract_version") == 2
    if operation.endswith(".list"):
        key = {"agent.list": "agents", "bot.list": "bots", "run.list": "runs"}[operation]
        return isinstance(payload.get(key), list)
    if operation.startswith("agent."):
        return payload.get("name") == params["name"] and (operation != "agent.delete" or payload.get("deleted") is True)
    if operation.startswith("run."):
        return (
            hex_id(payload.get("run_id"))
            and ("run_id" not in params or payload["run_id"] == params["run_id"])
            and valid_status
            and status in run_status
        )
    if operation == "bot.messages":
        return isinstance(payload.get("messages"), list)
    if operation in ("bot.message", "bot.event"):
        receipt = hex_id(payload.get("message_id")) and hex_id(payload.get("run_id"))
        if operation == "bot.event":
            return (
                receipt
                and payload.get("event_id") == params["event_id"]
                and isinstance(payload.get("duplicate"), bool)
                and valid_status
                and status in (run_status | {"unavailable"} if payload["duplicate"] else {"queued"})
            )
        return receipt
    if operation.startswith("bot."):
        receipt = hex_id(payload.get("bot_id")) and ("bot_id" not in params or payload["bot_id"] == params["bot_id"])
        if operation == "bot.delete":
            return receipt and payload.get("deleted") is True
        if operation == "bot.stop":
            return (
                receipt
                and payload.get("stop_requested") is True
                and isinstance(payload.get("run_ids"), list)
                and all(hex_id(value) for value in payload["run_ids"])
            )
        return receipt and valid_status and status in {"paused", "idle", "running", "error"}
    if operation == "group.create":
        return (
            hex_id(payload.get("group_id"))
            and isinstance(payload.get("run_ids"), list)
            and len(payload["run_ids"]) == len(params["bots"])
            and all(hex_id(value) for value in payload["run_ids"])
        )
    if operation == "group.get":
        return (
            payload.get("group_id") == params["group_id"]
            and valid_status
            and status in run_status
            and isinstance(payload.get("messages"), list)
        )
    return False


class InvalidParams(ValueError):
    pass


def validate(value: Any, schema: Mapping[str, Any], path: str = "params", depth: int = 0) -> None:
    """Validate the closed JSON-schema subset used above, without a runtime dependency."""
    if depth > 16:
        raise InvalidParams("parameters exceed the nesting limit")
    if "anyOf" in schema:
        for choice in schema["anyOf"]:
            try:
                validate(value, choice, path, depth + 1)
                return
            except InvalidParams:
                pass
        raise InvalidParams(f"{path} does not match an allowed shape")
    kind = schema.get("type")
    if kind == "null":
        if value is not None:
            raise InvalidParams(f"{path} must be null")
    elif kind == "object":
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise InvalidParams(f"{path} must be an object")
        properties = schema["properties"]
        if set(value) - set(properties):
            raise InvalidParams(f"{path} contains unknown fields")
        if any(key not in value for key in schema.get("required", [])) or len(value) < schema.get("minProperties", 0):
            raise InvalidParams(f"{path} is missing required fields")
        for key, item in value.items():
            validate(item, properties[key], f"{path}.{key}", depth + 1)
    elif kind == "array":
        if not isinstance(value, list) or not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 256):
            raise InvalidParams(f"{path} must be a bounded array")
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            raise InvalidParams(f"{path} must not contain duplicates")
        for item in value:
            validate(item, schema["items"], path + "[]", depth + 1)
    elif kind == "string":
        if not isinstance(value, str) or not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 12000):
            raise InvalidParams(f"{path} must be bounded text")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise InvalidParams(f"{path} has an invalid format")
        try:
            value.encode("utf-8")
        except UnicodeError as exc:
            raise InvalidParams(f"{path} must be valid Unicode") from exc
    elif kind in ("integer", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (kind == "integer" and not isinstance(value, int)):
            raise InvalidParams(f"{path} must be a {kind}")
        if not schema.get("minimum", -1_000_000_000) <= value <= schema.get("maximum", 1_000_000_000) or not math.isfinite(value):
            raise InvalidParams(f"{path} is outside its allowed range")
    else:
        raise InvalidParams("unsupported internal schema")
    if "enum" in schema and value not in schema["enum"]:
        raise InvalidParams(f"{path} is not an allowed value")


@dataclass(frozen=True)
class OperationResult:
    status_code: int
    body: dict[str, Any]


def failure(operation: str, status: int, code: str, message: str, *, outcome: str = "not_started", details: Any = None) -> OperationResult:
    body = {"ok": False, "operation": operation, "error": code, "message": redact(message)[0][:600], "outcome": outcome, "untrusted": True}
    if details:
        body["details"] = redact_value(details)[0]
    return OperationResult(status, body)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class SdkOperations:
    """Native client used by an authenticated SDK host; there is no permissive auth default.

    ``authorize`` is a host-owned callback. Only its literal True return admits
    the call; caller context must come from verified SDK authentication, not the
    params body or unverified identity headers. The installable host adapter
    supplies this callback from the real SDK auth integration.
    """

    def __init__(
        self,
        worker_origin: str,
        *,
        authorize: Callable[[Operation, Any], bool],
        timeout_s: float = 30,
        continuity_timeout_s: float = 55,
    ) -> None:
        parsed = urllib.parse.urlsplit(worker_origin)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("worker origin has an invalid port") from exc
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or not is_loopback_host(parsed.hostname)
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or port == 0
        ):
            raise ValueError("worker origin must be a credential-free native HTTP loopback origin")
        if not callable(authorize):
            raise ValueError("a trusted host authorizer is required")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not 0 < timeout_s <= 30:
            raise ValueError("timeout must be greater than zero and at most 30 seconds")
        if (
            isinstance(continuity_timeout_s, bool)
            or not isinstance(continuity_timeout_s, (int, float))
            or not 0 < continuity_timeout_s <= 55
        ):
            raise ValueError("continuity timeout must be greater than zero and at most 55 seconds")
        self.origin = worker_origin.rstrip("/")
        self.authorize = authorize
        self.timeout_s = timeout_s
        self.continuity_timeout_s = continuity_timeout_s
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def invoke(self, operation: str, params: Any, *, context: Any) -> OperationResult:
        spec = OPERATIONS.get(operation) if isinstance(operation, str) else None
        if spec is None:
            return failure("unknown", 404, "not_found", "Unknown SDK operation")
        try:
            if self.authorize(spec, context) is not True:
                return failure(operation, 403, "denied", "Verified owner and operation scope are required")
        except Exception:
            return failure(operation, 503, "unavailable", "SDK authorization could not be verified")
        try:
            validate(params, spec.schema)
            if len(json.dumps(params, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_BYTES:
                raise InvalidParams("parameters exceed 1 MiB")
        except (InvalidParams, ValueError, TypeError, OverflowError, RecursionError) as exc:
            return failure(operation, 400, "invalid", str(exc))
        route, body = spec.project(params)
        request = urllib.request.Request(
            self.origin + route,
            data=None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            method=spec.method,
            headers={"Accept": "application/json", **({"Content-Type": "application/json"} if body is not None else {})},
        )
        try:
            try:
                timeout = (
                    self.continuity_timeout_s if operation in ("bot.continuity.capture", "bot.continuity.reconcile") else self.timeout_s
                )
                response = self._opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                status = response.status
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES or response.headers.get_content_type() != "application/json":
                    return failure(
                        operation,
                        502,
                        "unavailable",
                        "Worker returned an oversized or non-JSON response",
                        outcome="failed" if spec.read_only else "unknown",
                    )
                payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                return failure(
                    operation,
                    502,
                    "unavailable",
                    "Worker returned an invalid response shape",
                    outcome="failed" if spec.read_only else "unknown",
                )
            payload = redact_value(payload)[0]
            if status >= 400 or payload.get("ok") is False:
                code = payload.get("error") if isinstance(payload.get("error"), str) else "unavailable"
                message = payload.get("message") if isinstance(payload.get("message"), str) else "Worker refused the operation"
                return failure(
                    operation,
                    status if status >= 400 else 502,
                    code,
                    message,
                    outcome="unknown" if not spec.read_only and status >= 500 else "failed",
                    details={key: value for key, value in payload.items() if key not in ("error", "message")},
                )
            if not 200 <= status < 300:
                return failure(
                    operation,
                    502,
                    "unavailable",
                    "Worker redirect or unexpected status was refused",
                    outcome="failed" if spec.read_only else "unknown",
                )
            if not valid_payload(operation, params, payload):
                return failure(
                    operation,
                    502,
                    "unavailable",
                    "Worker did not return a valid operation receipt",
                    outcome="failed" if spec.read_only else "unknown",
                )
            return OperationResult(status, {"ok": True, "operation": operation, "data": payload, "untrusted": True})
        except (OSError, ValueError, UnicodeError, RecursionError):
            return failure(
                operation,
                502,
                "unavailable",
                "Worker response was unavailable; no automatic retry was made",
                outcome="failed" if spec.read_only else "unknown",
            )
