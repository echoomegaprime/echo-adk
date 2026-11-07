"""Generate reviewable SDK registrations; never submit or activate them."""

from __future__ import annotations

import argparse
import copy
import json
import urllib.parse
from typing import Any

from ..api.server import is_loopback_host
from .operations import OPERATIONS

ROUTE_PREFIX = "/adk-sdk"
PROPOSED_IDS = {
    name: ("echo.echobots." + (name[4:] if name.startswith("bot.") else name.replace(".", "_")))
    if name.startswith(("bot.", "group."))
    else "echo.adk." + name.replace(".", "_")
    for name in OPERATIONS
}
OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok", "operation", "untrusted"],
    "properties": {
        "ok": {"type": "boolean"},
        "operation": {"type": "string"},
        "untrusted": {"const": True},
        "data": {"type": "object"},
        "error": {"type": "string"},
        "message": {"type": "string"},
        "outcome": {"type": "string", "enum": ["not_started", "failed", "unknown"]},
        "details": {"type": "object"},
    },
    "oneOf": [
        {"properties": {"ok": {"const": True}}, "required": ["data"], "not": {"required": ["error"]}},
        {"properties": {"ok": {"const": False}}, "required": ["error", "message", "outcome"], "not": {"required": ["data"]}},
    ],
    "x-echo-enforce-output-contract": True,
}


def proposed_manifest(gate_origin: str) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(gate_origin)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid SDK gate port") from exc
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
        raise ValueError("SDK router targets must use the existing native loopback gate origin")
    return {
        "registration_status": "proposed_not_registered",
        "requires": (
            "Installed real SDK host adapter, verified owner mapping, existing Tier2 HMAC admission, and operator-reviewed registration."
        ),
        "capabilities": [
            {
                "id": PROPOSED_IDS[op.name],
                "description": op.description + " Owner-only control plane; does not grant child workers recursive management authority.",
                "handler_kind": "router",
                "target_url": gate_origin.rstrip("/") + ROUTE_PREFIX + "/" + op.name,
                "target_method": "POST",
                "args_mode": "json_body",
                "input_schema_json": copy.deepcopy(op.schema),
                "output_schema_json": copy.deepcopy(OUTPUT_SCHEMA),
                "required_scope": "tier:2",
                "danger_tier": 2,
                "schema_version": 1,
                "lifecycle_status": "active",
            }
            for op in OPERATIONS.values()
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Print proposed ADK/echoBots SDK specs; makes no network calls or registry changes")
    parser.add_argument("--gate-origin", required=True, help="Existing native loopback SDK gate origin, including its actual port")
    args = parser.parse_args()
    print(json.dumps(proposed_manifest(args.gate_origin), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
