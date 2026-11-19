"""Core primitives (spec section 2) and the Envelope (spec section 3).

Every record is a dataclass with strict ``from_dict`` (unknown keys refused, ids are 32 lowercase
hex characters, budgets are bounded) and a stable ``to_dict`` for JSON transport.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

HEX32 = re.compile(r"^[0-9a-f]{32}$")
AGENT_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Contract rule for agent definitions (POST /agents): 2..63 chars, lowercase, digits, hyphens.
API_AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
ORIGIN_BY = ("echo", "owner")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
CAP_ID = re.compile(r"^echo\.[a-z0-9_]+(?:\.[a-z0-9_]+)*$")
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")

BUDGET_CAPS: dict[str, float] = {"max_calls": 120, "max_cost_usd": 5.0, "max_wall_s": 4800}
BUDGET_MIN: dict[str, float] = {"max_calls": 1, "max_cost_usd": 0.0, "max_wall_s": 1}
BUDGET_KEYS = ("max_calls", "max_cost_usd", "max_wall_s")
MAX_STEPS_CAP = 64
MAX_PROMPT_CHARS = 12000

ENVELOPE_KINDS = ("cap", "model", "shell", "memory", "artifact", "final")
VERDICTS = ("allowed", "gated", "denied")
PROOF_VERDICTS = ("PASS", "FAIL", "UNKNOWN")


class ValidationError(ValueError):
    """Raised when a record does not match the spec shape."""


def new_id() -> str:
    """A fresh 32-lowercase-hex identifier (run_id, lease_id, bot_id, ...)."""
    return secrets.token_hex(16)


def is_id(value: Any) -> bool:
    return isinstance(value, str) and bool(HEX32.match(value))


def now_iso(now: Optional[datetime] = None) -> str:
    """UTC timestamp with an explicit ``+00:00`` offset (parses on Python 3.10)."""
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError("timestamp must be a string")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def canonical_json(value: Any) -> str:
    """Deterministic JSON used for hashes: sorted keys, compact separators, no NaN."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mapping(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{what} must be an object")
    return dict(value)


def _only(data: Mapping[str, Any], allowed: Iterable[str], what: str) -> None:
    extra = sorted(set(data) - set(allowed))
    if extra:
        raise ValidationError(f"{what}: unknown field(s) {', '.join(extra)}")


def _require(data: Mapping[str, Any], keys: Iterable[str], what: str) -> None:
    missing = [k for k in keys if k not in data]
    if missing:
        raise ValidationError(f"{what}: missing field(s) {', '.join(missing)}")


def _str(value: Any, what: str, *, max_len: int = 4096, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{what} must be a string")
    if not allow_empty and not value.strip():
        raise ValidationError(f"{what} must not be empty")
    if len(value) > max_len:
        raise ValidationError(f"{what} is longer than {max_len} characters")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise ValidationError(f"{what} contains control characters")
    return value


def _str_list(value: Any, what: str, *, pattern: Optional[re.Pattern[str]] = None, max_items: int = 256) -> list[str]:
    if not isinstance(value, list):
        raise ValidationError(f"{what} must be a list")
    if len(value) > max_items:
        raise ValidationError(f"{what} has more than {max_items} items")
    out: list[str] = []
    for item in value:
        text = _str(item, f"{what} item", max_len=256)
        if pattern and not pattern.match(text):
            raise ValidationError(f"{what} item {text!r} is not valid")
        if text in out:
            raise ValidationError(f"{what} contains duplicate {text!r}")
        out.append(text)
    return out


def _int(value: Any, what: str, *, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{what} must be an integer")
    if value < lo or value > hi:
        raise ValidationError(f"{what} must be between {lo} and {hi}")
    return value


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{what} must be a number")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValidationError(f"{what} must be finite")
    return float(value)


# --------------------------------------------------------------------------- Budget / Lane


@dataclass(frozen=True)
class Budget:
    """Lane budget. Service caps: max_calls 120, max_cost_usd 5, max_wall_s 4800."""

    max_calls: int = 40
    max_cost_usd: float = 0.0
    max_wall_s: int = 600

    def __post_init__(self) -> None:
        _int(self.max_calls, "budget.max_calls", lo=int(BUDGET_MIN["max_calls"]), hi=int(BUDGET_CAPS["max_calls"]))
        cost = _number(self.max_cost_usd, "budget.max_cost_usd")
        if cost < BUDGET_MIN["max_cost_usd"] or cost > BUDGET_CAPS["max_cost_usd"]:
            raise ValidationError("budget.max_cost_usd must be between 0 and 5")
        _int(self.max_wall_s, "budget.max_wall_s", lo=int(BUDGET_MIN["max_wall_s"]), hi=int(BUDGET_CAPS["max_wall_s"]))

    def to_dict(self) -> dict[str, Any]:
        return {"max_calls": self.max_calls, "max_cost_usd": round(self.max_cost_usd, 2), "max_wall_s": self.max_wall_s}

    @classmethod
    def from_dict(cls, data: Any, *, defaults: Optional[Budget] = None, what: str = "budget") -> Budget:
        """Strict parse. Missing keys fall back to ``defaults``; values are clamped into the service caps."""
        given = _mapping(data, what)
        _only(given, BUDGET_KEYS, what)
        base = defaults or cls()
        values: dict[str, float] = {}
        for key in BUDGET_KEYS:
            raw = given.get(key, getattr(base, key))
            number = _number(raw, f"{what}.{key}")
            clamped = min(BUDGET_CAPS[key], max(BUDGET_MIN[key], number))
            values[key] = round(clamped, 2) if key == "max_cost_usd" else int(clamped)
        return cls(int(values["max_calls"]), float(values["max_cost_usd"]), int(values["max_wall_s"]))

    def clamp_to(self, ceiling: Budget) -> Budget:
        """Never exceed another budget (the agent lane maxima)."""
        return Budget(
            min(self.max_calls, ceiling.max_calls),
            round(min(self.max_cost_usd, ceiling.max_cost_usd), 2),
            min(self.max_wall_s, ceiling.max_wall_s),
        )


@dataclass(frozen=True)
class Lane:
    """Where and how a run executes."""

    node: str
    model: str
    budget: Budget = field(default_factory=Budget)
    concurrency: int = 1

    def __post_init__(self) -> None:
        _str(self.node, "lane.node", max_len=64)
        if not MODEL_ID.match(self.model):
            raise ValidationError("lane.model is not a valid model id")
        _int(self.concurrency, "lane.concurrency", lo=1, hi=16)

    def to_dict(self) -> dict[str, Any]:
        return {"node": self.node, "model": self.model, "budget": self.budget.to_dict()}

    @classmethod
    def from_dict(cls, data: Any) -> Lane:
        given = _mapping(data, "lane")
        _only(given, ("node", "model", "budget", "concurrency"), "lane")
        _require(given, ("node", "model"), "lane")
        budget = Budget.from_dict(given.get("budget", {}))
        return cls(given["node"], given["model"], budget, given.get("concurrency", 1))


@dataclass(frozen=True)
class Spent:
    calls: int = 0
    cost_usd: float = 0.0
    wall_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"calls": self.calls, "cost_usd": round(self.cost_usd, 4), "wall_s": round(self.wall_s, 3)}


# --------------------------------------------------------------------------- Role / Skill / Capability


@dataclass(frozen=True)
class Role:
    """Operating specialization. Routing metadata, never an authorization grant."""

    name: str
    tier: int = 7
    primary_skill: str = ""
    composed_skills: tuple[str, ...] = ()
    capability_families: tuple[str, ...] = ()
    boot_prompt: str = ""

    def __post_init__(self) -> None:
        if not AGENT_NAME.match(self.name) or len(self.name) > 64:
            raise ValidationError("role.name must be kebab-case, 64 characters or fewer")
        _int(self.tier, "role.tier", lo=0, hi=11)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tier": self.tier,
            "primary_skill": self.primary_skill,
            "composed_skills": list(self.composed_skills),
            "capability_families": list(self.capability_families),
            "boot_prompt": self.boot_prompt,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Role:
        given = _mapping(data, "role")
        _only(given, ("name", "tier", "primary_skill", "composed_skills", "capability_families", "boot_prompt"), "role")
        _require(given, ("name",), "role")
        return cls(
            given["name"],
            given.get("tier", 7),
            given.get("primary_skill", ""),
            tuple(_str_list(given.get("composed_skills", []), "role.composed_skills")),
            tuple(_str_list(given.get("capability_families", []), "role.capability_families")),
            given.get("boot_prompt", ""),
        )


@dataclass(frozen=True)
class Skill:
    """Agent-Skills-spec metadata (L1). ``echo_adk.core.skills`` loads bodies and references."""

    name: str
    description: str
    path: Optional[str] = None

    def __post_init__(self) -> None:
        if not AGENT_NAME.match(self.name) or len(self.name) > 64:
            raise ValidationError("skill.name must be lowercase kebab-case, 64 characters or fewer")
        _str(self.description, "skill.description", max_len=1024)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "path": self.path}


@dataclass(frozen=True)
class Capability:
    """One gate capability: the only kind of tool an Echo agent has."""

    id: str
    tier: int
    scope: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    health: str = "unknown"
    verb: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        if not CAP_ID.match(self.id) or len(self.id) > 128:
            raise ValidationError(f"capability id {self.id!r} is not valid")
        _int(self.tier, f"capability {self.id} tier", lo=0, hi=3)
        _str(self.scope, f"capability {self.id} scope", max_len=128)
        if not isinstance(self.input_schema, dict) or self.input_schema.get("type") != "object":
            raise ValidationError(f"capability {self.id} input_schema must be an object schema")
        if self.health not in ("green", "amber", "red", "unknown"):
            raise ValidationError(f"capability {self.id} health must be green|amber|red|unknown")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tier": self.tier,
            "scope": self.scope,
            "input_schema": self.input_schema,
            "health": self.health,
            "verb": self.verb,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Capability:
        given = _mapping(data, "capability")
        _only(given, ("id", "tier", "scope", "input_schema", "health", "verb", "description"), "capability")
        _require(given, ("id", "tier", "scope"), "capability")
        return cls(
            given["id"],
            given["tier"],
            given["scope"],
            given.get("input_schema") or {"type": "object", "properties": {}},
            given.get("health", "unknown"),
            given.get("verb", ""),
            given.get("description", ""),
        )

    @classmethod
    def from_caps_detail(cls, detail: Mapping[str, Any]) -> Capability:
        """Build from the ``cap`` object returned by ``echo.caps.detail``."""
        cap = detail.get("cap", detail)
        if not isinstance(cap, Mapping) or "id" not in cap:
            raise ValidationError("caps.detail payload has no cap.id")
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in cap.get("params") or []:
            if not isinstance(param, Mapping) or not isinstance(param.get("name"), str):
                continue
            prop: dict[str, Any] = {"type": _json_type(param.get("type"))}
            if param.get("description"):
                prop["description"] = str(param["description"])[:512]
            if isinstance(param.get("enum"), list):
                prop["enum"] = list(param["enum"])
            properties[param["name"]] = prop
            if param.get("required"):
                required.append(param["name"])
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        tier = cap.get("tier")
        if not isinstance(tier, int) or isinstance(tier, bool):
            raise ValidationError(f"caps.detail for {cap['id']} has no integer tier")
        health = cap.get("health") or cap.get("health_status") or "unknown"
        if health not in ("green", "amber", "red"):
            health = "unknown"
        return cls(
            str(cap["id"]),
            tier,
            str(cap.get("scope") or "unknown"),
            schema,
            health,
            str(cap.get("verb") or ""),
            str(cap.get("description") or "")[:512],
        )


def _json_type(value: Any) -> str:
    mapping = {
        "string": "string",
        "str": "string",
        "integer": "integer",
        "int": "integer",
        "number": "number",
        "float": "number",
        "boolean": "boolean",
        "bool": "boolean",
        "array": "array",
        "list": "array",
        "object": "object",
        "dict": "object",
    }
    return mapping.get(str(value or "").lower(), "string")


# --------------------------------------------------------------------------- Lease


@dataclass
class Lease:
    """Time-boxed, scoped, use-capped permission for one agent run (R3: exact cap ids, no ``*``)."""

    lease_id: str
    run_id: str
    scopes: list[str]
    expires_at: str
    max_uses: int
    uses: int = 0
    grant_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if not is_id(self.lease_id):
            raise ValidationError("lease.lease_id must be 32 lowercase hex characters")
        if not is_id(self.run_id):
            raise ValidationError("lease.run_id must be 32 lowercase hex characters")
        self.scopes = _str_list(self.scopes, "lease.scopes", pattern=CAP_ID)
        if any("*" in scope for scope in self.scopes):
            raise ValidationError("lease.scopes must be exact capability ids (no wildcards)")
        parse_iso(self.expires_at)
        _int(self.max_uses, "lease.max_uses", lo=1, hi=int(BUDGET_CAPS["max_calls"]))
        _int(self.uses, "lease.uses", lo=0, hi=int(BUDGET_CAPS["max_calls"]))
        if self.grant_ref is not None:
            _str(self.grant_ref, "lease.grant_ref", max_len=256)

    @classmethod
    def mint(
        cls,
        run_id: str,
        scopes: Iterable[str],
        *,
        ttl_s: int,
        max_uses: int,
        grant_ref: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Lease:
        ttl = _int(ttl_s, "lease.ttl_s", lo=1, hi=int(BUDGET_CAPS["max_wall_s"]))
        start = now or datetime.now(timezone.utc)
        return cls(new_id(), run_id, list(scopes), now_iso(start + timedelta(seconds=ttl)), max_uses, 0, grant_ref)

    def permits(self, capability: str) -> bool:
        return capability in self.scopes

    def expired(self, now: Optional[datetime] = None) -> bool:
        return (now or datetime.now(timezone.utc)) >= parse_iso(self.expires_at)

    def exhausted(self) -> bool:
        return self.uses >= self.max_uses

    def consume(self) -> None:
        if self.exhausted():
            raise ValidationError("lease has no uses left")
        self.uses += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "run_id": self.run_id,
            "scopes": list(self.scopes),
            "expires_at": self.expires_at,
            "max_uses": self.max_uses,
            "uses": self.uses,
            "grant_ref": self.grant_ref,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Lease:
        given = _mapping(data, "lease")
        _only(given, ("lease_id", "run_id", "scopes", "expires_at", "max_uses", "uses", "grant_ref"), "lease")
        _require(given, ("lease_id", "run_id", "scopes", "expires_at", "max_uses"), "lease")
        return cls(
            given["lease_id"],
            given["run_id"],
            given["scopes"],
            given["expires_at"],
            given["max_uses"],
            given.get("uses", 0),
            given.get("grant_ref"),
        )


# --------------------------------------------------------------------------- Envelope


@dataclass
class Envelope:
    """One shape for every call and event (spec section 3). ``untrusted`` is always True."""

    run_id: str
    step: int
    kind: str
    verdict: str
    ok: bool
    capability: Optional[str] = None
    tier: Optional[int] = None
    redacted: int = 0
    audit_id: Optional[str] = None
    duration_ms: int = 0
    needs: Optional[str] = None
    how: Optional[str] = None
    data: dict[str, Any] = field(default_factory=dict)
    envelope_version: int = 1
    untrusted: bool = True

    def __post_init__(self) -> None:
        self.envelope_version = 1
        self.untrusted = True
        if not is_id(self.run_id):
            raise ValidationError("envelope.run_id must be 32 lowercase hex characters")
        _int(self.step, "envelope.step", lo=0, hi=10_000)
        if self.kind not in ENVELOPE_KINDS:
            raise ValidationError(f"envelope.kind must be one of {', '.join(ENVELOPE_KINDS)}")
        if self.verdict not in VERDICTS:
            raise ValidationError(f"envelope.verdict must be one of {', '.join(VERDICTS)}")
        if not isinstance(self.ok, bool):
            raise ValidationError("envelope.ok must be a boolean")
        if self.capability is not None and not CAP_ID.match(self.capability):
            raise ValidationError("envelope.capability is not a valid capability id")
        if self.tier is not None:
            _int(self.tier, "envelope.tier", lo=0, hi=3)
        _int(self.redacted, "envelope.redacted", lo=0, hi=1_000_000)
        _int(self.duration_ms, "envelope.duration_ms", lo=0, hi=10**9)
        if not isinstance(self.data, dict):
            raise ValidationError("envelope.data must be an object")

    def to_dict(self) -> dict[str, Any]:
        return {
            "envelope_version": 1,
            "run_id": self.run_id,
            "step": self.step,
            "kind": self.kind,
            "capability": self.capability,
            "tier": self.tier,
            "verdict": self.verdict,
            "ok": self.ok,
            "untrusted": True,
            "redacted": self.redacted,
            "audit_id": self.audit_id,
            "duration_ms": self.duration_ms,
            "needs": self.needs,
            "how": self.how,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Envelope:
        given = _mapping(data, "envelope")
        _only(
            given,
            (
                "envelope_version",
                "run_id",
                "step",
                "kind",
                "capability",
                "tier",
                "verdict",
                "ok",
                "untrusted",
                "redacted",
                "audit_id",
                "duration_ms",
                "needs",
                "how",
                "data",
            ),
            "envelope",
        )
        _require(given, ("run_id", "step", "kind", "verdict", "ok"), "envelope")
        if given.get("envelope_version", 1) != 1:
            raise ValidationError("envelope.envelope_version must be 1")
        return cls(
            run_id=given["run_id"],
            step=given["step"],
            kind=given["kind"],
            verdict=given["verdict"],
            ok=given["ok"],
            capability=given.get("capability"),
            tier=given.get("tier"),
            redacted=given.get("redacted", 0),
            audit_id=given.get("audit_id"),
            duration_ms=given.get("duration_ms", 0),
            needs=given.get("needs"),
            how=given.get("how"),
            data=given.get("data") or {},
        )


# --------------------------------------------------------------------------- Proof


@dataclass(frozen=True)
class Proof:
    """Required output of every run. A run without a Proof did not happen (R4)."""

    run_id: str
    lease_id: str
    events_sha256: str
    actions: tuple[dict[str, Any], ...]
    artifacts: tuple[Any, ...]
    receipts: tuple[Any, ...]
    verdict: str

    def __post_init__(self) -> None:
        if not is_id(self.run_id):
            raise ValidationError("proof.run_id must be 32 lowercase hex characters")
        if not is_id(self.lease_id):
            raise ValidationError("proof.lease_id must be 32 lowercase hex characters")
        if not re.match(r"^[0-9a-f]{64}$", self.events_sha256):
            raise ValidationError("proof.events_sha256 must be a sha256 hex digest")
        if self.verdict not in PROOF_VERDICTS:
            raise ValidationError("proof.verdict must be PASS, FAIL or UNKNOWN")

    @staticmethod
    def hash_events(envelopes: Iterable[Envelope]) -> str:
        return sha256_hex(canonical_json([env.to_dict() for env in envelopes]))

    @classmethod
    def from_envelopes(
        cls,
        run_id: str,
        lease_id: str,
        envelopes: Iterable[Envelope],
        verdict: str,
        *,
        artifacts: Iterable[Any] = (),
        receipts: Iterable[Any] = (),
    ) -> Proof:
        events = list(envelopes)
        actions = tuple(
            {"cap": env.capability, "tier": env.tier, "verdict": env.verdict, "audit_id": env.audit_id}
            for env in events
            if env.kind == "cap" and env.capability
        )
        return cls(run_id, lease_id, cls.hash_events(events), actions, tuple(artifacts), tuple(receipts), verdict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "lease_id": self.lease_id,
            "events_sha256": self.events_sha256,
            "actions": [dict(a) for a in self.actions],
            "artifacts": list(self.artifacts),
            "receipts": list(self.receipts),
            "verdict": self.verdict,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Proof:
        given = _mapping(data, "proof")
        _only(given, ("run_id", "lease_id", "events_sha256", "actions", "artifacts", "receipts", "verdict"), "proof")
        _require(given, ("run_id", "lease_id", "events_sha256", "actions", "verdict"), "proof")
        return cls(
            given["run_id"],
            given["lease_id"],
            given["events_sha256"],
            tuple(given["actions"]),
            tuple(given.get("artifacts", [])),
            tuple(given.get("receipts", [])),
            given["verdict"],
        )


# --------------------------------------------------------------------------- EchoAgent


def validate_origin(value: Any) -> dict[str, Any]:
    """``origin:{by:"echo"|"owner", prompt_sha256?, created_at, updated_at?}`` on definitions created through the API."""
    given = _mapping(value, "agent.origin")
    _only(given, ("by", "prompt_sha256", "created_at", "updated_at"), "agent.origin")
    _require(given, ("by",), "agent.origin")
    if given["by"] not in ORIGIN_BY:
        raise ValidationError("agent.origin.by must be echo or owner")
    out: dict[str, Any] = {"by": given["by"]}
    digest = given.get("prompt_sha256")
    if digest is not None:
        if not isinstance(digest, str) or not SHA256_HEX.match(digest):
            raise ValidationError("agent.origin.prompt_sha256 must be a sha256 hex digest")
        out["prompt_sha256"] = digest
    for key in ("created_at", "updated_at"):
        if given.get(key) is not None:
            parse_iso(given[key])
            out[key] = given[key]
    return out


@dataclass(frozen=True)
class EchoAgent:
    """The agent record: data (YAML/JSON) loadable by the core runner and every adapter."""

    name: str
    role: str
    skills: tuple[str, ...]
    capabilities: tuple[str, ...]
    lane: Lane
    max_steps: int = 12
    instruction: Optional[str] = None
    source: str = ""
    origin: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        if not API_AGENT_NAME.match(self.name):
            raise ValidationError("agent.name must match ^[a-z0-9][a-z0-9-]{1,62}$")
        if not AGENT_NAME.match(self.role) or len(self.role) > 64:
            raise ValidationError("agent.role must be lowercase kebab-case, 64 characters or fewer")
        _int(self.max_steps, "agent.max_steps", lo=1, hi=MAX_STEPS_CAP)
        if self.instruction is not None:
            _str(self.instruction, "agent.instruction", max_len=MAX_PROMPT_CHARS)
        if not isinstance(self.lane, Lane):
            raise ValidationError("agent.lane must be a Lane")
        if self.origin is not None:
            object.__setattr__(self, "origin", validate_origin(self.origin))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "role": self.role,
            "skills": list(self.skills),
            "capabilities": list(self.capabilities),
            "lane": self.lane.to_dict(),
            "max_steps": self.max_steps,
            "source": self.source,
        }
        if self.instruction is not None:
            out["instruction"] = self.instruction
        if self.origin is not None:
            out["origin"] = dict(self.origin)
        return out

    def to_json(self, indent: Optional[int] = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Any, *, source: str = "") -> EchoAgent:
        given = _mapping(data, "agent")
        _only(given, ("name", "role", "skills", "capabilities", "lane", "max_steps", "instruction", "source", "origin"), "agent")
        _require(given, ("name", "role", "capabilities", "lane"), "agent")
        if not isinstance(given["name"], str):
            raise ValidationError("agent.name must be a string")
        skills = tuple(_str_list(given.get("skills", []), "agent.skills", pattern=AGENT_NAME))
        capabilities = tuple(_str_list(given["capabilities"], "agent.capabilities", pattern=CAP_ID))
        instruction = given.get("instruction")
        if instruction is not None and not isinstance(instruction, str):
            raise ValidationError("agent.instruction must be a string")
        origin = given.get("origin")
        return cls(
            given["name"],
            given["role"],
            skills,
            capabilities,
            Lane.from_dict(given["lane"]),
            given.get("max_steps", 12),
            instruction,
            source or str(given.get("source") or ""),
            validate_origin(origin) if origin is not None else None,
        )

    @classmethod
    def from_json(cls, text: str, *, source: str = "") -> EchoAgent:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"agent file is not valid JSON: {exc.msg}") from exc
        return cls.from_dict(data, source=source)
