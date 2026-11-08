"""Closed SDK contracts for actual bot workspace functions.

OAuth authorization/consent belongs to the admitted owner. Worker tools never
receive authorization codes, URLs, state, profile files, or token material.
"""

from __future__ import annotations

import re
from typing import Any


def operations() -> list[Any]:
    from .operations import ID, Operation, array, integer, obj

    word = {"type": "string", "pattern": "^[a-z0-9][a-z0-9_.-]{0,79}$"}
    text = {"type": "string", "maxLength": 16000}
    json_text = {"type": "string", "minLength": 2, "maxLength": 262144}
    digest = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    revision = integer(0, 1000000)
    boolean = {"type": "boolean"}
    base = "/v2/bots"
    bot = base + "/{bot_id}"
    rows: list[Any] = []

    def add(
        name: str,
        method: str,
        path: str,
        properties: dict[str, Any],
        required: tuple[str, ...] = (),
        *,
        read: bool = False,
        query: tuple[str, ...] = (),
        description: str,
    ) -> None:
        per_bot = "{bot_id}" in path
        rows.append(
            Operation(
                "bot." + name,
                method,
                path,
                obj({**({"bot_id": ID} if per_bot else {}), **properties}, (("bot_id",) if per_bot else ()) + required),
                read,
                "bot_id" if per_bot else None,
                query,
                description,
            )
        )

    add(
        "continuity.status",
        "GET",
        bot + "/continuity",
        {},
        read=True,
        description="Inspect this bot's host-bound ECF capture outbox; local availability is not remote write authorization.",
    )
    add(
        "continuity.capture",
        "POST",
        bot + "/continuity/capture",
        {
            "request_ref": {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,100}$"},
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "detail": {"type": "string", "minLength": 1, "maxLength": 4000},
        },
        ("request_ref", "title", "detail"),
        description=(
            "Capture one explicit bounded summary through the existing gate and this bot's continuity.write lease; "
            "the host fixes project attribution."
        ),
    )
    add(
        "continuity.reconcile",
        "POST",
        bot + "/continuity/reconcile",
        {"capture_id": ID},
        ("capture_id",),
        description=(
            "Explicitly reconcile the original durable ECF request; an uncertain write may be resubmitted "
            "with the same payload and requestId only."
        ),
    )
    add(
        "marketplace",
        "GET",
        base + "/marketplace",
        {},
        read=True,
        description="Read host-configured OAuth provider and operation availability.",
    )
    add("connection.list", "GET", bot + "/connections", {}, read=True, description="List this bot's connection metadata without tokens.")
    for name in ("get", "refresh", "disconnect"):
        add(
            "connection." + name,
            "POST",
            bot + "/connections/" + name,
            {"connection_id": ID},
            ("connection_id",),
            read=name == "get",
            description={
                "get": "Inspect a connection bound to this bot.",
                "refresh": "Refresh this bot's existing OAuth grant with the configured provider.",
                "disconnect": "Disconnect this bot's OAuth grant and report provider revocation.",
            }[name],
        )
    add(
        "connection.authorize",
        "POST",
        bot + "/connections/authorize",
        {"provider_id": word, "operation_ids": array(word, 32, minimum=1, unique=True)},
        ("provider_id", "operation_ids"),
        description="Begin owner consent for explicitly selected operations and the fixed host redirect URI.",
    )
    add(
        "connection.invoke",
        "POST",
        bot + "/connections/invoke",
        {"connection_id": ID, "operation_id": word, "input_json": json_text},
        ("connection_id", "operation_id", "input_json"),
        description=(
            "Invoke one host-defined operation through this bot's own OAuth connection; "
            "input_json is checked against the operation's closed schema."
        ),
    )
    add(
        "subscription.list",
        "GET",
        bot + "/subscriptions",
        {},
        read=True,
        description="List this bot's authenticated polling subscriptions, delivery receipts and precheck status.",
    )
    add(
        "subscription.create",
        "POST",
        bot + "/subscriptions",
        {
            "connection_id": ID,
            "operation_id": word,
            "params_json": {**json_text, "maxLength": 65536},
            "interval_s": integer(60, 86400),
            "predicate_json": {**json_text, "maxLength": 4096},
        },
        ("connection_id", "operation_id", "params_json", "interval_s"),
        description=(
            "Create a disabled polling subscription to an already authorized read-only operation with an optional deterministic precheck."
        ),
    )
    for action in ("enable", "disable", "delete"):
        add(
            "subscription." + action,
            "POST",
            bot + "/subscriptions/" + action,
            {"subscription_id": ID},
            ("subscription_id",),
            description={
                "enable": "Enable this bot's configured event subscription; its lease and task do not change.",
                "disable": "Disable future polling and event admission for this subscription.",
                "delete": "Delete this bot's subscription and its pending local delivery state.",
            }[action],
        )
    add(
        "subscription.poll",
        "POST",
        bot + "/subscriptions/poll",
        {"limit": integer(1, 8)},
        description="Process already-due enabled subscriptions for this bot without bypassing intervals or replaying unknown delivery.",
    )
    add(
        "browser.status",
        "GET",
        bot + "/browser",
        {},
        read=True,
        description="Read this bot's real browser availability and process/profile isolation status.",
    )
    command = {
        "anyOf": [
            obj({"action": {"type": "string", "enum": [action]}, **fields, "timeout_ms": integer(1, 30000)}, ("action",) + required)
            for action, fields, required in (
                ("navigate", {"url": {"type": "string", "minLength": 1, "maxLength": 2048}}, ("url",)),
                ("snapshot", {}, ()),
                ("screenshot", {}, ()),
                ("click", {"ref": {"type": "string", "minLength": 1, "maxLength": 100}}, ("ref",)),
                (
                    "fill",
                    {"ref": {"type": "string", "minLength": 1, "maxLength": 100}, "value": {"type": "string", "maxLength": 4096}},
                    ("ref", "value"),
                ),
            )
        ]
    }
    # Flatten command properties into the existing gate's application object.
    browser_fields = {key: value for shape in command["anyOf"] for key, value in shape["properties"].items()}
    browser_fields["action"] = {"type": "string", "enum": ["navigate", "snapshot", "screenshot", "click", "fill"]}
    browser_schema = obj({"bot_id": ID, **browser_fields}, ("bot_id", "action"))
    rows.append(
        Operation(
            "bot.browser.act",
            "POST",
            bot + "/browser/actions",
            browser_schema,
            False,
            "bot_id",
            description="Operate this bot's isolated browser using current opaque refs and host-owned egress policy.",
        )
    )
    for action in ("close", "reset"):
        add(
            "browser." + action,
            "POST",
            bot + "/browser/" + action,
            {},
            description="Close the bot browser"
            + (" and remove its durable profile/artifacts." if action == "reset" else " while retaining its profile."),
        )
    add(
        "browser.artifact",
        "GET",
        bot + "/browser/artifacts",
        {"artifact_id": ID},
        ("artifact_id",),
        read=True,
        query=("artifact_id",),
        description="Read one bounded screenshot owned by this bot; no filesystem paths accepted.",
    )
    add("memory.read", "GET", bot + "/memory", {}, read=True, description="Read this bot's editable memory notes and current revision.")
    add(
        "memory.edit",
        "PUT",
        bot + "/memory",
        {"text": text, "expected_revision": revision},
        ("text", "expected_revision"),
        description="Save redacted memory notes using optimistic revision control.",
    )
    add(
        "memory.revisions",
        "GET",
        bot + "/memory/revisions",
        {"limit": integer(1, 100), "before": revision},
        read=True,
        query=("limit", "before"),
        description="Page through this bot's immutable memory revision metadata.",
    )
    add(
        "memory.restore",
        "POST",
        bot + "/memory/restore",
        {"revision": revision, "expected_revision": revision},
        ("revision", "expected_revision"),
        description="Restore an exact memory revision by appending an audited new revision.",
    )
    add("team.templates", "GET", base + "/templates", {}, read=True, description="List installed portable team templates.")
    add(
        "team.template",
        "GET",
        base + "/templates/get",
        {"template_id": word},
        ("template_id",),
        read=True,
        query=("template_id",),
        description="Read a validated portable team template.",
    )
    add(
        "team.export",
        "POST",
        base + "/export",
        {
            "bot_ids": array(ID, 32, minimum=1, unique=True),
            "name": {"type": "string", "minLength": 1, "maxLength": 80},
            "kind": {"type": "string", "enum": ["team", "department", "template"]},
            "include_memory": boolean,
        },
        ("bot_ids", "name", "kind"),
        read=True,
        description="Export portable definitions without credentials, leases, sessions or run histories. Notes require explicit opt-in.",
    )
    importing = {
        "bundle_json": json_text,
        "conflict": {"type": "string", "enum": ["reject", "reuse_identical"]},
        "rename_agents_json": json_text,
        "rename_bots_json": json_text,
        "include_memory": boolean,
    }
    add(
        "team.preview",
        "POST",
        base + "/import/preview",
        importing,
        ("bundle_json",),
        read=True,
        description="Validate an import and return the digest of its exact creation plan.",
    )
    add(
        "team.import",
        "POST",
        base + "/import",
        {**importing, "expected_plan_sha256": digest},
        ("bundle_json", "expected_plan_sha256"),
        description="Commit a matching import plan, creating paused bots with empty leases and no provider connections.",
    )
    add(
        "team.imports",
        "GET",
        base + "/imports",
        {},
        read=True,
        description="List import journals including interrupted work awaiting review.",
    )
    add(
        "team.inspect",
        "POST",
        base + "/imports/inspect",
        {"import_id": ID},
        ("import_id",),
        read=True,
        description="Inspect exact resources and recovery eligibility for an import journal.",
    )
    add(
        "team.recover",
        "POST",
        base + "/imports/recover",
        {"import_id": ID, "expected_recovery_sha256": digest, "action": {"type": "string", "enum": ["rollback", "preserve"]}},
        ("import_id", "expected_recovery_sha256", "action"),
        description="Resolve a reviewed interrupted import using its exact recovery digest; rollback refuses changed resources.",
    )
    return rows


def valid_payload(operation: str, params: dict[str, Any], payload: dict[str, Any]) -> bool | None:
    def identity(value: Any) -> bool:
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None

    if operation.startswith("bot.continuity."):
        if (
            payload.get("bot_id") != params.get("bot_id")
            or payload.get("untrusted") is not True
            or payload.get("shared_remote_ledger") is not True
        ):
            return False
        if operation.endswith(".status"):
            return (
                type(payload.get("configured")) is bool
                and type(payload.get("lease_permits_capture")) is bool
                and payload.get("remote_authorization_verified") is False
                and isinstance(payload.get("captures"), list)
                and len(payload["captures"]) <= 128
                and all(
                    valid_payload("bot.continuity.capture", params, row) is True for row in payload["captures"] if isinstance(row, dict)
                )
                and all(isinstance(row, dict) for row in payload["captures"])
            )
        if (
            not identity(payload.get("capture_id"))
            or ("capture_id" in params and payload["capture_id"] != params["capture_id"])
            or payload.get("state") not in ("prepared", "uncertain", "saved", "verified", "blocked", "conflict")
            or type(payload.get("saved")) is not bool
            or type(payload.get("verified")) is not bool
            or payload["verified"] != (payload["state"] == "verified")
            or payload["saved"] != (payload["state"] in ("saved", "verified"))
            or not isinstance(payload.get("request_id"), str)
            or not isinstance(payload.get("request_ref"), str)
            or re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", payload["request_ref"]) is None
            or ("request_ref" in params and payload["request_ref"] != params["request_ref"])
            or not isinstance(payload.get("content_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", payload["content_sha256"]) is None
        ):
            return False
        receipt = payload.get("receipt")
        if not payload["saved"]:
            return receipt is None and not payload["verified"]
        return (
            isinstance(receipt, dict)
            and receipt.get("saved") is True
            and receipt.get("verified") is True
            and all(isinstance(receipt.get(key), str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", receipt[key]) for key in ("id", "eventId"))
            and receipt.get("kind") == "note"
            and receipt.get("requestId") == payload["request_id"]
            and receipt.get("contentSha256") == payload["content_sha256"]
            and receipt.get("receiptState") == "capture_verified"
            and type(receipt.get("duplicate")) is bool
        )
    if operation == "bot.marketplace":
        return isinstance(payload.get("providers"), list) and isinstance(payload.get("configured"), bool)
    if operation.startswith("bot.browser."):
        if payload.get("bot_id") != params.get("bot_id"):
            return False
        action = operation.rsplit(".", 1)[1]
        if action == "status":
            return isinstance(payload.get("available"), bool)
        if action in ("close", "reset"):
            return payload.get("closed") is True and (action != "reset" or payload.get("reset") is True)
        if action == "artifact":
            return (
                payload.get("artifact_id") == params["artifact_id"]
                and payload.get("mime_type") == "image/png"
                and isinstance(payload.get("base64"), str)
            )
        return payload.get("action") == params.get("action") and payload.get("untrusted") is True
    if operation.startswith("bot.connection."):
        action = operation.rsplit(".", 1)[1]
        if payload.get("bot_id") != params.get("bot_id"):
            return False
        if action == "list":
            return isinstance(payload.get("connections"), list)
        if action == "authorize":
            connection = payload.get("connection", {})
            return (
                isinstance(connection, dict)
                and identity(connection.get("id"))
                and connection.get("bot_id") == params["bot_id"]
                and isinstance(payload.get("authorization_url"), str)
            )
        if action == "invoke":
            return (
                payload.get("connection_id") == params["connection_id"]
                and payload.get("operation_id") == params["operation_id"]
                and payload.get("ok") is True
            )
        return (payload.get("id") or payload.get("connection_id")) == params["connection_id"] and payload.get("status") in (
            "connected",
            "pending",
            "disconnected",
            "reauth_required",
            "error",
        )
    if operation.startswith("bot.memory."):
        if payload.get("bot_id") != params.get("bot_id"):
            return False
        if operation.endswith(".revisions"):
            return isinstance(payload.get("revisions"), list)
        return isinstance(payload.get("text"), str) and type(payload.get("revision")) is int
    if operation.startswith("bot.subscription."):
        if payload.get("bot_id") != params.get("bot_id"):
            return False
        action = operation.rsplit(".", 1)[1]
        if action == "list":
            return isinstance(payload.get("subscriptions"), list)
        if action == "poll":
            return isinstance(payload.get("receipts"), list)
        if not identity(payload.get("id")) or ("subscription_id" in params and params["subscription_id"] != payload["id"]):
            return False
        return payload.get("deleted") is True if action == "delete" else isinstance(payload.get("enabled"), bool)
    if operation.startswith("bot.team."):
        action = operation.rsplit(".", 1)[1]
        if action == "imports":
            return isinstance(payload.get("imports"), list)
        if action in ("inspect", "recover"):
            return payload.get("import_id") == params["import_id"] and isinstance(payload.get("status"), str)
        if action == "templates":
            return isinstance(payload.get("templates"), list)
        if action in ("template", "export"):
            return isinstance(payload.get("bundle_json"), str) and payload.get("format") == "echo-bots-portable"
        if action == "preview":
            return isinstance(payload.get("plan_sha256"), str) and isinstance(payload.get("bots"), list)
        return (
            payload.get("status") == "imported"
            and payload.get("plan_sha256") == params.get("expected_plan_sha256")
            and isinstance(payload.get("bots"), list)
        )
    return None


def owner_callback_contract() -> dict[str, Any]:
    """Native owner protocol only, deliberately absent from the SDK gate.

    General SDK audit pipelines may record input params; the one-use OAuth
    code/state must travel only through the owner callback transport.
    """
    from .operations import obj

    return {
        "method": "POST",
        "path": "/v2/bots/connections/callback",
        "schema": obj(
            {"state": {"type": "string", "minLength": 16, "maxLength": 512}, "code": {"type": "string", "minLength": 1, "maxLength": 4096}},
            ("state", "code"),
        ),
        "readOnly": False,
        "query": [],
    }
