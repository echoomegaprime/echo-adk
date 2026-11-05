"""Default-deny capability policy (R3 scoped lease, R5 fail-closed).

Order of checks in :meth:`Policy.evaluate`:

1. capability id shape and registry membership (unknown capability -> denied)
2. known tier 0..3 and an object input schema (unknown tier / missing schema -> denied)
3. agent allowlist (not listed -> denied)
4. lease present, unexpired, with uses left and the exact cap id in scope (else -> gated)
5. tier >= 2 needs a lease ``grant_ref`` (signed ESH grant / SOL broker grant) (else -> gated)
6. budget: calls and wall clock (exceeded -> denied, code ``budget_exceeded``)
7. params: required fields present, no secret-shaped field names (else -> denied)
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .redact import is_secret_field
from .types import CAP_ID, Budget, Capability, Lease, Spent, ValidationError

ALLOWED = "allowed"
GATED = "gated"
DENIED = "denied"

TIER_NAMES = {0: "open", 1: "standard", 2: "privileged", 3: "exploit"}


@dataclass(frozen=True)
class Decision:
    verdict: str
    tier: Optional[int]
    reason: str
    needs: Optional[str] = None
    how: Optional[str] = None
    code: Optional[str] = None  # denied|gated|budget_exceeded|unknown_capability|invalid

    @property
    def allowed(self) -> bool:
        return self.verdict == ALLOWED


# Verified live on 2026-11-04 via ``echo.caps.detail`` (tier, scope, verb, params). Ship as data;
# load the rest of the catalogue at runtime with ``CapabilityRegistry.load_json`` or
# ``Capability.from_caps_detail``.
BUILTIN_CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "echo.knowledge.search",
        0,
        "knowledge.read",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "alpha": {"type": "number"},
                "corpus_ids": {"type": "array"},
                "limit": {"type": "integer"},
                "snippet_chars": {"type": "integer"},
            },
            "required": ["query"],
        },
        "green",
        "search",
        "Hybrid semantic+FTS search over ingested docs (Knowledge Forge)",
    ),
    Capability(
        "echo.knowledge.get",
        0,
        "docs.read",
        {
            "type": "object",
            "properties": {"doc_id": {"type": "string"}, "include_body": {"type": "boolean"}},
            "required": ["doc_id"],
        },
        "green",
        "get",
        "Fetch one knowledge document by doc_id",
    ),
    Capability(
        "echo.caps.search",
        0,
        "caps.read",
        {"type": "object", "properties": {"q": {"type": "string"}, "limit": {"type": "integer"}}},
        "green",
        "search",
        "Substring search over registered SDK capabilities",
    ),
    Capability(
        "echo.caps.list",
        0,
        "caps.read",
        {"type": "object", "properties": {"prefix": {"type": "string"}, "limit": {"type": "integer"}}},
        "green",
        "list",
        "List registered SDK capabilities by id prefix",
    ),
    Capability(
        "echo.caps.detail",
        0,
        "caps.read",
        {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        "green",
        "detail",
        "Full detail (tier, scope, params, examples) for one capability",
    ),
    Capability(
        "echo.caps.namespaces",
        0,
        "caps.read",
        {"type": "object", "properties": {}},
        "green",
        "namespaces",
        "List capability namespaces with counts",
    ),
    Capability(
        "echo.monitor.health.node",
        0,
        "monitor.read",
        {
            "type": "object",
            "properties": {"node": {"type": "string"}, "per_node_timeout_ms": {"type": "integer"}},
            "required": ["node"],
        },
        "green",
        "node",
        "Health snapshot for one cluster node",
    ),
    Capability(
        "echo.monitor.health.cluster",
        0,
        "monitor.read",
        {"type": "object", "properties": {}},
        "green",
        "cluster",
        "Health snapshot for the whole cluster",
    ),
    Capability(
        "echo.context.recall",
        0,
        "context.read",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "q": {"type": "string"},
                "text": {"type": "string"},
                "kind": {"type": "string"},
                "limit": {"type": "integer"},
                "min_importance": {"type": "integer"},
                "workspace": {"type": "string"},
            },
        },
        "green",
        "recall",
        "Recall decisions and context across all Claude Code instances",
    ),
    Capability(
        "echo.context.remember",
        1,
        "context.write",
        {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "kind": {"type": "string"},
                "importance": {"type": "number"},
                "tags": {"type": "string"},
                "source": {"type": "string"},
                "workspace": {"type": "string"},
                "expires_at": {"type": "string"},
                "value": {"type": "string"},
            },
        },
        "green",
        "remember",
        "Persist a decision or fact into shared context",
    ),
    Capability(
        "echo.builds.log",
        1,
        "builds.write",
        {"type": "object", "properties": {}},
        "green",
        "log",
        "Register a build/fix/edit in the fleet build log",
    ),
    Capability(
        "echo.shell.run",
        1,
        "shell.exec",
        {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        "green",
        "run",
        "Run a shell command on a cluster node through the gate",
    ),
)


class CapabilityRegistry:
    """Known capabilities. Anything not registered is denied (default-deny)."""

    def __init__(self, capabilities: Iterable[Capability] = ()) -> None:
        self._caps: dict[str, Capability] = {}
        for cap in capabilities:
            self.register(cap)

    @classmethod
    def builtin(cls) -> CapabilityRegistry:
        return cls(BUILTIN_CAPABILITIES)

    def register(self, cap: Capability) -> None:
        if not isinstance(cap, Capability):
            raise ValidationError("registry accepts Capability records only")
        self._caps[cap.id] = cap

    def get(self, cap_id: str) -> Optional[Capability]:
        return self._caps.get(cap_id)

    def __contains__(self, cap_id: object) -> bool:
        return cap_id in self._caps

    def __len__(self) -> int:
        return len(self._caps)

    def ids(self) -> list[str]:
        return sorted(self._caps)

    def capabilities(self) -> list[Capability]:
        return [self._caps[k] for k in self.ids()]

    def load_json(self, path: str | Path) -> int:
        """Load ``[Capability...]`` or ``{"capabilities": [...]}`` or ``{"caps": [caps.detail...]}``."""
        text = Path(path).read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"capabilities file is not valid JSON: {exc.msg}") from exc
        items: Any
        if isinstance(data, dict) and "capabilities" in data:
            items = data["capabilities"]
        elif isinstance(data, dict) and "caps" in data:
            items = [Capability.from_caps_detail(c) for c in data["caps"]]
        else:
            items = data
        if not isinstance(items, list):
            raise ValidationError("capabilities file must hold a list")
        count = 0
        for item in items:
            cap = item if isinstance(item, Capability) else Capability.from_dict(item)
            self.register(cap)
            count += 1
        return count

    def tier_of(self, cap_id: str) -> Optional[int]:
        cap = self._caps.get(cap_id)
        return cap.tier if cap else None


def classify_tier(cap_id: str, registry: CapabilityRegistry) -> Optional[int]:
    """Tier for a capability id from the registry only. ``None`` means unknown -> refuse."""
    if not isinstance(cap_id, str) or not CAP_ID.match(cap_id):
        return None
    return registry.tier_of(cap_id)


class Policy:
    """Default-deny verdicts for one agent's allowlist and budget."""

    def __init__(self, registry: CapabilityRegistry, allowlist: Iterable[str], budget: Budget) -> None:
        self.registry = registry
        self.allowlist = tuple(dict.fromkeys(allowlist))
        self.budget = budget
        for cap_id in self.allowlist:
            if not CAP_ID.match(cap_id):
                raise ValidationError(f"allowlist entry {cap_id!r} is not a capability id")

    def allowed_capabilities(self) -> list[Capability]:
        """Allowlisted capabilities that the registry knows (what a model may be told about)."""
        out = []
        for cap_id in self.allowlist:
            cap = self.registry.get(cap_id)
            if cap is not None:
                out.append(cap)
        return out

    def evaluate(
        self,
        cap_id: Any,
        params: Any,
        lease: Optional[Lease],
        spent: Spent = Spent(),
        now: Optional[datetime] = None,
    ) -> Decision:
        if not isinstance(cap_id, str) or not CAP_ID.match(cap_id):
            return Decision(DENIED, None, "capability id is not valid", code="unknown_capability")
        cap = self.registry.get(cap_id)
        if cap is None:
            return Decision(DENIED, None, f"unknown capability {cap_id}", code="unknown_capability")
        if cap.tier not in TIER_NAMES:
            return Decision(DENIED, None, f"unknown tier for {cap_id}", code="denied")
        schema = cap.input_schema
        if not isinstance(schema, dict) or schema.get("type") != "object":
            return Decision(DENIED, cap.tier, f"capability {cap_id} has no input schema", code="denied")
        if cap_id not in self.allowlist:
            return Decision(DENIED, cap.tier, f"{cap_id} is not in the agent allowlist", code="denied")

        if lease is None:
            return Decision(
                GATED,
                cap.tier,
                "no lease for this run",
                needs="lease",
                how=f"mint a Lease whose scopes include {cap_id} (exact id) for this run_id",
                code="gated",
            )
        if lease.expired(now or datetime.now(timezone.utc)):
            return Decision(GATED, cap.tier, "lease expired", needs="lease renewal", how="mint a fresh Lease", code="gated")
        if lease.exhausted():
            return Decision(
                GATED,
                cap.tier,
                "lease uses exhausted",
                needs="lease with more max_uses",
                how="mint a fresh Lease with a higher max_uses",
                code="gated",
            )
        if not lease.permits(cap_id):
            return Decision(
                GATED,
                cap.tier,
                f"{cap_id} is not in the lease scopes",
                needs=f"lease scope {cap_id}",
                how=f"re-mint the Lease with {cap_id} listed in scopes",
                code="gated",
            )
        if cap.tier >= 2 and not lease.grant_ref:
            return Decision(
                GATED,
                cap.tier,
                f"tier {cap.tier} ({TIER_NAMES[cap.tier]}) needs a signed grant",
                needs=f"tier-{cap.tier} grant",
                how="issue an ESH signed grant (esh grant issue) or a SOL broker grant and set lease.grant_ref",
                code="gated",
            )

        if spent.calls + 1 > self.budget.max_calls:
            return Decision(
                DENIED,
                cap.tier,
                f"budget max_calls {self.budget.max_calls} reached",
                needs="budget",
                how="start a new run with a higher max_calls (service cap 120)",
                code="budget_exceeded",
            )
        if spent.wall_s > self.budget.max_wall_s:
            return Decision(
                DENIED,
                cap.tier,
                f"budget max_wall_s {self.budget.max_wall_s} exceeded",
                needs="budget",
                how="start a new run with a higher max_wall_s (service cap 4800)",
                code="budget_exceeded",
            )

        problem = validate_params(params, schema)
        if problem:
            return Decision(DENIED, cap.tier, problem, code="invalid")
        return Decision(ALLOWED, cap.tier, "allowed")


def validate_params(params: Any, schema: Mapping[str, Any]) -> Optional[str]:
    """Light JSON-schema check: object, required keys, declared primitive types, no secret-shaped keys."""
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return "params must be an object"
    for key in params:
        if not isinstance(key, str):
            return "params keys must be strings"
        if is_secret_field(key):
            return f"secret-shaped param {key!r} refused"
    for key in schema.get("required", []) or []:
        if key not in params:
            return f"missing required param {key!r}"
    properties = schema.get("properties") or {}
    checks = {
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
    }
    for key, value in params.items():
        spec = properties.get(key)
        if not isinstance(spec, dict):
            continue
        expected = spec.get("type")
        check = checks.get(str(expected)) if expected else None
        if check and value is not None and not check(value):
            return f"param {key!r} must be {expected}"
        enum = spec.get("enum")
        if isinstance(enum, list) and value not in enum:
            return f"param {key!r} must be one of {enum}"
    try:
        encoded = json.dumps(params, ensure_ascii=False)
    except (TypeError, ValueError):
        return "params must be JSON-serialisable"
    if len(encoded) > 65536:
        return "params larger than 64 KiB"
    return None
