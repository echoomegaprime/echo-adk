"""Portable bot definitions, local templates and explicit interrupted-import recovery.

Bundles contain definitions, never runtime identities or authority. Imports use
the service's ordinary validators and create paused bots with empty leases.
Agent files and the notes database cannot share a SQLite commit: a durable
journal makes that boundary visible and supports conservative owner recovery.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from contextlib import ExitStack, nullcontext
from typing import Any

from ..core.redact import redact
from ..core.types import API_AGENT_NAME, MAX_PROMPT_CHARS, EchoAgent, ValidationError, canonical_json, is_id, new_id, now_iso
from .memory import BotMemoryRevisions, clean_memory_text
from .schedules import validate_schedule

FORMAT = "echo-bots-portable"
VERSION = 1
MAX_BUNDLE_BYTES = 1024 * 1024
MAX_MEMBERS = 32
MAX_CAPABILITIES = 64
_REF = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_AGENT_FIELDS = {"name", "role", "skills", "capabilities", "lane", "max_steps", "instruction"}
_BOT_FIELDS = {"ref", "name", "agent", "mode", "schedule", "instruction", "plugins", "daily_cost_cap_usd", "memory_text"}
_PENDING = ("prepared", "notes_pending", "recovery_required")


class PortabilityError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 and c not in "\r\n\t" for c in value):
        raise PortabilityError(f"{name} must be a non-empty string of at most {maximum} characters")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise PortabilityError(f"{name} must be valid UTF-8") from None
    return value


def _object(value: Any, allowed: set[str], required: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - allowed or required - set(value):
        raise PortabilityError(f"{name} has missing or unsupported fields")
    return value


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise PortabilityError("include_memory must be a boolean")
    return value


def _strings(value: Any) -> Any:
    # Structural fields have already been allowlisted. Do not use secret-field
    # heuristics to rename structural references or capability definitions.
    if isinstance(value, str):
        return redact(value)[0]
    if isinstance(value, dict):
        return {name: _strings(item) for name, item in value.items()}
    if isinstance(value, list):
        return [_strings(item) for item in value]
    return value


def _agent(agent: EchoAgent) -> dict[str, Any]:
    result = {name: value for name, value in agent.to_dict().items() if name in _AGENT_FIELDS}
    result["lane"]["concurrency"] = agent.lane.concurrency
    return result


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise PortabilityError("bundle contains duplicate JSON fields")
        result[name] = value
    return result


def _parse(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        raise PortabilityError("bundle_json must be a serialized JSON object")
    try:
        if len(value.encode("utf-8")) > MAX_BUNDLE_BYTES:
            raise PortabilityError("bundle_json exceeds 1 MiB")
        bundle = json.loads(value, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, PortabilityError):
            raise
        raise PortabilityError("bundle_json must be finite, valid JSON") from None
    return _object(
        bundle, {"format", "version", "name", "kind", "agents", "bots"}, {"format", "version", "name", "kind", "agents", "bots"}, "bundle"
    )


def _envelope(bundle: dict[str, Any], *, memory_included: bool) -> dict[str, Any]:
    raw = canonical_json(bundle)
    if len(raw.encode("utf-8")) > MAX_BUNDLE_BYTES:
        raise PortabilityError("portable definitions exceed 1 MiB; export fewer bots")
    return {
        "bundle_json": raw,
        "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "format": FORMAT,
        "version": VERSION,
        "name": bundle["name"],
        "kind": bundle["kind"],
        "counts": {"agents": len(bundle["agents"]), "bots": len(bundle["bots"])},
        "memory_included": memory_included,
    }


def _templates() -> dict[str, dict[str, Any]]:
    definitions = {
        "research-team": (
            "Research and review",
            "Summarize supplied evidence and have a second bot check unsupported claims.",
            [
                ("researcher", "Summarize the evidence supplied by the owner. Identify uncertainty and cite provided sources."),
                ("reviewer", "Review supplied research for unsupported claims, omissions and contradictions. Ask for missing evidence."),
            ],
        ),
        "task-decomposition": (
            "Plan and review",
            "Split an owner-provided task into bounded steps and review their dependencies.",
            [
                ("planner", "Break the owner's task into concrete, bounded steps with dependencies and acceptance criteria."),
                ("reviewer", "Check the supplied plan for missing prerequisites, unsafe assumptions and verifiable completion criteria."),
            ],
        ),
    }
    catalog = {}
    for template_id, (name, description, members) in definitions.items():
        agents, bots = [], []
        for index, (role, instruction) in enumerate(members, start=1):
            agent_name = f"{template_id}-{role}"
            agents.append(
                {
                    "name": agent_name,
                    "role": role,
                    "skills": [],
                    "capabilities": [],
                    "lane": {"node": "forge", "model": "qwen-local", "budget": {"max_calls": 6, "max_cost_usd": 0, "max_wall_s": 120}},
                    "max_steps": 4,
                    "instruction": instruction,
                }
            )
            bots.append(
                {
                    "ref": f"bot-{index}",
                    "name": role.title(),
                    "agent": agent_name,
                    "mode": "team",
                    "schedule": None,
                    "plugins": [],
                    "daily_cost_cap_usd": 0,
                }
            )
        catalog[template_id] = {
            "description": description,
            "bundle": {"format": FORMAT, "version": VERSION, "name": name, "kind": "template", "agents": agents, "bots": bots},
        }
    return catalog


class PortabilityManager:
    def __init__(self, service: Any, *, memory: BotMemoryRevisions | None = None) -> None:
        self.service = service
        self.store = service.store
        self.memory = memory
        with self.store.transaction():
            self.store._db.execute(
                "CREATE TABLE IF NOT EXISTS portable_imports (import_id TEXT PRIMARY KEY,plan_sha256 TEXT NOT NULL,"
                "status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,doc TEXT NOT NULL)"
            )

    def _resource_guard(self, bot_ids: list[str]):
        guard = getattr(getattr(self.service, "workspace", None), "resource_guard", None)
        return guard(bot_ids) if callable(guard) else nullcontext()

    def list_templates(self) -> dict[str, Any]:
        return {
            "templates": [
                {
                    "template_id": template_id,
                    "name": item["bundle"]["name"],
                    "description": item["description"],
                    "kind": "template",
                    "bot_count": len(item["bundle"]["bots"]),
                    "model": "qwen-local",
                    "permissions": "none",
                }
                for template_id, item in _templates().items()
            ]
        }

    def get_template(self, template_id: str) -> dict[str, Any]:
        item = _templates().get(template_id) if isinstance(template_id, str) else None
        if item is None:
            raise PortabilityError("local template not found", code="not_found", status=404)
        return {"template_id": template_id, **_envelope(item["bundle"], memory_included=False)}

    def export_bundle(self, bot_ids: list[str], *, name: str, kind: str = "team", include_memory: bool = False) -> dict[str, Any]:
        include_memory = _bool(include_memory)
        name = _text(name, "name", 128)
        if kind not in ("team", "department", "template"):
            raise PortabilityError("kind must be team, department or template")
        if (
            not isinstance(bot_ids, list)
            or not 1 <= len(bot_ids) <= MAX_MEMBERS
            or not all(is_id(bot_id) for bot_id in bot_ids)
            or len(set(bot_ids)) != len(bot_ids)
        ):
            raise PortabilityError("bot_ids must contain 1 through 32 distinct bot identifiers")
        if include_memory and self.memory is None:
            raise PortabilityError("editable memory is unavailable", code="unavailable", status=503)
        with self.service.bots._lock, self.service._lock:
            agents, bots = {}, []
            for index, bot_id in enumerate(bot_ids, start=1):
                bot = self.service.bots._require(bot_id)
                agent = self.service.get_agent(bot["agent"])
                agents[agent.name] = _agent(agent)
                item = {field: copy.deepcopy(bot[field]) for field in _BOT_FIELDS - {"ref", "memory_text"} if field in bot}
                item["ref"] = f"bot-{index}"
                if include_memory:
                    item["memory_text"] = self.memory.read(bot_id)["text"]
                bots.append(item)
        bundle = _strings({"format": FORMAT, "version": VERSION, "name": name, "kind": kind, "agents": list(agents.values()), "bots": bots})
        return _envelope(bundle, memory_included=include_memory)

    @staticmethod
    def _renames(value: Any, known: set[str], *, agents: bool) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict) or set(value) - known:
            raise PortabilityError("rename map contains an unknown portable reference")
        for item in value.values():
            _text(item, "rename", 63 if agents else 64)
            if agents and API_AGENT_NAME.fullmatch(item) is None:
                raise PortabilityError("renamed agent must be a valid agent name")
        return dict(value)

    def _plan(
        self, bundle_json: str, *, conflict: str, rename_agents: Any, rename_bots: Any, include_memory: bool
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        include_memory = _bool(include_memory)
        if conflict not in ("reject", "reuse_identical"):
            raise PortabilityError("conflict must be reject or reuse_identical")
        bundle = _parse(bundle_json)
        if (
            bundle["format"] != FORMAT
            or type(bundle["version"]) is not int
            or bundle["version"] != VERSION
            or bundle["kind"] not in ("team", "department", "template")
        ):
            raise PortabilityError("unsupported portable format, version or kind")
        _text(bundle["name"], "bundle.name", 128)
        for field in ("agents", "bots"):
            if not isinstance(bundle[field], list) or not 1 <= len(bundle[field]) <= MAX_MEMBERS:
                raise PortabilityError(f"bundle.{field} must contain 1 through 32 definitions")
        source_agents = {}
        for raw in bundle["agents"]:
            _object(raw, _AGENT_FIELDS, {"name", "role", "capabilities", "lane"}, "portable agent")
            agent_name = _text(raw["name"], "agent.name", 63)
            if API_AGENT_NAME.fullmatch(agent_name) is None or agent_name in source_agents:
                raise PortabilityError("portable agent names must be distinct valid agent names")
            source_agents[agent_name] = raw
        source_bots = {}
        for raw in bundle["bots"]:
            _object(raw, _BOT_FIELDS, {"ref", "name", "agent"}, "portable bot")
            ref = raw["ref"]
            if not isinstance(ref, str) or _REF.fullmatch(ref) is None or ref in source_bots:
                raise PortabilityError("portable bot refs must be distinct lowercase identifiers")
            if not isinstance(raw["agent"], str) or raw["agent"] not in source_agents:
                raise PortabilityError("portable bot refers to an undefined agent")
            source_bots[ref] = raw
        if {raw["agent"] for raw in source_bots.values()} != set(source_agents):
            raise PortabilityError("every portable agent must be referenced by a bot")
        agent_renames = self._renames(rename_agents, set(source_agents), agents=True)
        bot_renames = self._renames(rename_bots, set(source_bots), agents=False)
        if include_memory and self.memory is None:
            raise PortabilityError("editable memory is unavailable", code="unavailable", status=503)
        definitions, parsed_agents, actions = {}, {}, []
        all_caps: set[str] = set()
        for original, raw in source_agents.items():
            body = _strings(copy.deepcopy(raw))
            body["name"] = agent_renames.get(original, original)
            if body["name"] in definitions:
                raise PortabilityError("renaming maps multiple agents to the same name")
            if not isinstance(body["capabilities"], list) or not all(isinstance(cap, str) for cap in body["capabilities"]):
                raise PortabilityError("agent capabilities must be a list of exact identifiers")
            all_caps.update(body["capabilities"])
            if len(all_caps) > MAX_CAPABILITIES:
                raise PortabilityError("a bundle may reference at most 64 distinct capabilities")
            parsed = self.service._agent_from_body(body)
            normalized = _agent(parsed)
            definitions[parsed.name], parsed_agents[original] = normalized, parsed
            existing = self.service.agents.get(parsed.name)
            file_exists = any((self.service.agents_dir / f"{parsed.name}{suffix}").exists() for suffix in (".json", ".yaml", ".yml"))
            if existing is not None or file_exists:
                if conflict != "reuse_identical" or existing is None or _agent(existing) != normalized:
                    raise PortabilityError(
                        "an agent name conflicts with an existing definition; rename it or reuse an identical definition",
                        code="conflict",
                        status=409,
                    )
                action = "reuse"
            else:
                action = "create"
            actions.append({"name": parsed.name, "action": action, "definition_sha256": _hash(normalized)})
        names = {bot["name"] for bot in self.store.list_bots()}
        bots, warnings = (
            [],
            ["Imported bots remain paused with empty lease scopes; permissions and provider configuration require owner review."],
        )
        for ref, raw in source_bots.items():
            agent = parsed_agents[raw["agent"]]
            name = _text(redact(_text(bot_renames.get(ref, raw["name"]), "bot.name", 64))[0], "bot.name", 64)
            if name in names:
                raise PortabilityError("a bot display name conflicts; provide an explicit rename_bots entry", code="conflict", status=409)
            names.add(name)
            mode = raw.get("mode", "personal")
            if mode not in ("personal", "team"):
                raise PortabilityError("bot.mode must be personal or team")
            instruction = raw.get("instruction")
            if instruction is not None:
                instruction = _text(instruction, "bot.instruction", MAX_PROMPT_CHARS)
            plugins = raw.get("plugins", [])
            if (
                not isinstance(plugins, list)
                or len(plugins) > 32
                or not all(isinstance(item, str) and 0 < len(item) <= 64 for item in plugins)
            ):
                raise PortabilityError("bot.plugins must list at most 32 skill names")
            self.service.bots._validate_skill_context(agent, plugins)
            cap = raw.get("daily_cost_cap_usd", 0.0)
            if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap) or not 0 <= cap <= 5:
                raise PortabilityError("bot.daily_cost_cap_usd must be between 0 and 5")
            try:
                schedule = validate_schedule(raw.get("schedule"))
            except ValidationError as error:
                raise PortabilityError(str(error)) from None
            body = _strings(
                {
                    "name": name,
                    "agent": agent.name,
                    "mode": mode,
                    "schedule": schedule,
                    "instruction": instruction,
                    "plugins": list(dict.fromkeys(plugins)),
                    "daily_cost_cap_usd": round(float(cap), 2),
                }
            )
            body["lease"] = {"scopes": [], "max_uses": agent.lane.budget.max_calls, "ttl_s": agent.lane.budget.max_wall_s}
            memory_text = None
            if "memory_text" in raw:
                cleaned, _ = clean_memory_text(raw["memory_text"])
                if include_memory:
                    memory_text = cleaned
                elif "Editable notes in the bundle were excluded by import settings." not in warnings:
                    warnings.append("Editable notes in the bundle were excluded by import settings.")
            bots.append({"ref": ref, "body": body, "memory_text": memory_text})
        plan = {
            "name": bundle["name"],
            "kind": bundle["kind"],
            "definitions": definitions,
            "actions": actions,
            "bots": bots,
            "conflict": conflict,
            "include_memory": include_memory,
            "warnings": warnings,
        }
        public = {
            "plan_sha256": _hash(plan),
            "agents": [
                {**definitions[item["name"]], "action": item["action"], "definition_sha256": item["definition_sha256"]} for item in actions
            ],
            "bots": [
                {
                    "ref": item["ref"],
                    "name": item["body"]["name"],
                    "agent": item["body"]["agent"],
                    "status": "paused",
                    "lease_scopes": [],
                    "memory_included": item["memory_text"] is not None,
                    "schedule": item["body"]["schedule"],
                    "daily_cost_cap_usd": item["body"]["daily_cost_cap_usd"],
                    "plugins": item["body"]["plugins"],
                    "instruction": item["body"]["instruction"],
                }
                for item in bots
            ],
            "warnings": warnings,
        }
        return public, plan

    def preview_import(
        self,
        bundle_json: str,
        *,
        conflict: str = "reject",
        rename_agents: Any = None,
        rename_bots: Any = None,
        include_memory: bool = False,
    ) -> dict[str, Any]:
        with self.service.bots._lock, self.service._lock:
            return self._plan(
                bundle_json, conflict=conflict, rename_agents=rename_agents, rename_bots=rename_bots, include_memory=include_memory
            )[0]

    def _journal(self, record: dict[str, Any], status: str) -> None:
        record["status"], record["updated_at"] = status, now_iso()
        with self.store.transaction():
            self.store._db.execute(
                "INSERT OR REPLACE INTO portable_imports VALUES (?,?,?,?,?,?)",
                (record["import_id"], record["plan_sha256"], status, record["created_at"], record["updated_at"], canonical_json(record)),
            )

    def _record(self, import_id: str) -> dict[str, Any]:
        if not is_id(import_id):
            raise PortabilityError("import_id must be an exact import identifier")
        with self.store._lock:
            row = self.store._db.execute("SELECT doc FROM portable_imports WHERE import_id=?", (import_id,)).fetchone()
        if row is None:
            raise PortabilityError("import record not found", code="not_found", status=404)
        return json.loads(row[0])

    def list_imports(self) -> dict[str, Any]:
        with self.store._lock:
            rows = self.store._db.execute(
                "SELECT import_id,plan_sha256,status,created_at,updated_at FROM portable_imports "
                "ORDER BY status IN ('prepared','notes_pending','recovery_required') DESC,created_at DESC,import_id LIMIT 100"
            ).fetchall()
        return {
            "imports": [dict(zip(("import_id", "plan_sha256", "status", "created_at", "updated_at"), row, strict=True)) for row in rows]
        }

    def import_bundle(
        self,
        bundle_json: str,
        *,
        expected_plan_sha256: str,
        conflict: str = "reject",
        rename_agents: Any = None,
        rename_bots: Any = None,
        include_memory: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(expected_plan_sha256, str) or _HASH.fullmatch(expected_plan_sha256) is None:
            raise PortabilityError("expected_plan_sha256 must come from an import preview")
        with self.service.bots._lock, self.service._lock, ExitStack() as resources:
            with self.store._lock:
                pending = self.store._db.execute(
                    "SELECT import_id FROM portable_imports WHERE status IN (?,?,?) LIMIT 1", _PENDING
                ).fetchone()
            if pending:
                raise PortabilityError(
                    f"Interrupted import {pending[0]} requires recovery review", code="import_recovery_required", status=409
                )
            public, plan = self._plan(
                bundle_json, conflict=conflict, rename_agents=rename_agents, rename_bots=rename_bots, include_memory=include_memory
            )
            if public["plan_sha256"] != expected_plan_sha256:
                raise PortabilityError("import preview changed; review a fresh plan before applying", code="conflict", status=409)
            record = {
                "import_id": new_id(),
                "plan_sha256": expected_plan_sha256,
                "created_at": now_iso(),
                "intended_agents": [item for item in plan["actions"] if item["action"] == "create"],
                "created_agents": [],
                "bots": [],
            }
            self._journal(record, "prepared")
            try:
                for action in record["intended_agents"]:
                    name = action["name"]
                    self.service.create_agent(plan["definitions"][name])
                    path = self.service.agents_dir / f"{name}.json"
                    record["created_agents"].append({"name": name, "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
                    self._journal(record, "prepared")
                with self.store.transaction():
                    for item in plan["bots"]:
                        bot = self.service.create_bot(item["body"])
                        stored = self.store.get_bot(bot["bot_id"])
                        stored["portable_import_id"] = record["import_id"]
                        self.store.put_bot(stored)
                        record["bots"].append(
                            {
                                "ref": item["ref"],
                                "bot_id": bot["bot_id"],
                                "name": bot["name"],
                                "snapshot_sha256": _hash(stored),
                                "memory_revision_sha256": None,
                            }
                        )
                    # New identifiers are not visible until this Store commit.
                    # Take their resource locks before an owner can attach a
                    # profile, connection or note to this incomplete import.
                    resources.enter_context(self._resource_guard([item["bot_id"] for item in record["bots"]]))
                    self._journal(record, "notes_pending")
                for item, bot in zip(plan["bots"], record["bots"], strict=True):
                    if item["memory_text"] is not None:
                        notes = self.memory.edit(
                            bot["bot_id"], item["memory_text"], expected_revision=0, actor="owner", reason="Portable import"
                        )
                        bot["memory_revision_sha256"] = notes["revision_sha256"]
                        self._journal(record, "notes_pending")
                self._journal(record, "imported")
            except Exception:
                # The bot transaction may already have rolled back. Recovery
                # checks actual hashes and never deletes unrelated definitions.
                self._journal(record, "recovery_required")
                inspected = self._inspect(record)
                if inspected["rollback_available"]:
                    self._rollback(record)
                raise
            return {
                "import_id": record["import_id"],
                "plan_sha256": expected_plan_sha256,
                "status": "imported",
                "created_agents": [item["name"] for item in record["created_agents"]],
                "reused_agents": [item["name"] for item in plan["actions"] if item["action"] == "reuse"],
                "bots": [
                    {"ref": item["ref"], "bot_id": item["bot_id"], "name": item["name"], "status": "paused"} for item in record["bots"]
                ],
                "warnings": plan["warnings"],
            }

    def _inspect(self, record: dict[str, Any]) -> dict[str, Any]:
        resources, safe = [], True
        created = {item["name"]: item for item in record["created_agents"]}
        owned_bot_ids = {item["bot_id"] for item in record["bots"]}
        for intended in record["intended_agents"]:
            name = intended["name"]
            path = self.service.agents_dir / f"{name}.json"
            exists = path.exists() or name in self.service.agents
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else None
            tracked = created.get(name)
            referenced = any(bot["agent"] == name and bot["bot_id"] not in owned_bot_ids for bot in self.store.list_bots())
            referenced = referenced or bool(self.store.list_runs(agent=name, limit=1))
            unchanged = not exists or (tracked is not None and digest == tracked["file_sha256"] and not referenced)
            safe = safe and unchanged
            resources.append(
                {
                    "kind": "agent",
                    "name": name,
                    "exists": exists,
                    "sha256": digest,
                    "rollback_safe": bool(unchanged),
                    "tracked": tracked is not None,
                }
            )
        for item in record["bots"]:
            bot_id = item["bot_id"]
            bot = self.store.get_bot(bot_id)
            note = self.memory.read(bot_id) if self.memory is not None else None
            note_safe = note is None or (
                note["revision"] == 0
                if item["memory_revision_sha256"] is None
                else note["revision_sha256"] == item["memory_revision_sha256"]
            )
            attached = False
            if bot is not None:
                check = getattr(getattr(self.service, "workspace", None), "has_bot_resources", None)
                try:
                    attached = not callable(check) or bool(check(bot_id))
                except Exception:
                    attached = True  # An unavailable resource inventory cannot authorize deletion.
            unchanged = bot is None or (
                bot["status"] == "paused"
                and _hash(bot) == item["snapshot_sha256"]
                and bot.get("portable_import_id") == record["import_id"]
                and not self.store.list_runs(bot_id=bot_id, limit=1)
                and not self.store.count_messages(bot_id)
            )
            unchanged = unchanged and note_safe and not attached
            safe = safe and unchanged
            resources.append(
                {
                    "kind": "bot",
                    "bot_id": bot_id,
                    "ref": item["ref"],
                    "exists": bot is not None,
                    "sha256": _hash(bot) if bot else None,
                    "memory_revision_sha256": note["revision_sha256"] if note else None,
                    "attached_resources": attached,
                    "rollback_safe": bool(unchanged),
                }
            )
        result = {
            "import_id": record["import_id"],
            "plan_sha256": record["plan_sha256"],
            "status": record["status"],
            "resources": resources,
            "rollback_available": bool(safe and record["status"] in _PENDING),
            "preserve_available": record["status"] in _PENDING,
        }
        result["recovery_sha256"] = _hash(result)
        return result

    def inspect_import(self, import_id: str) -> dict[str, Any]:
        observed = self._record(import_id)
        ids = [item["bot_id"] for item in observed["bots"]]
        with self._resource_guard(ids), self.service.bots._lock, self.service._lock:
            current = self._record(import_id)
            if ids != [item["bot_id"] for item in current["bots"]]:
                raise PortabilityError("import resources changed during inspection; retry the read", code="conflict", status=409)
            return self._inspect(current)

    def _rollback(self, record: dict[str, Any]) -> None:
        for item in reversed(record["bots"]):
            if self.store.get_bot(item["bot_id"]) is not None:
                self.service.bots.delete(item["bot_id"])
            elif self.memory is not None:
                self.memory.delete(item["bot_id"])
        for item in reversed(record["created_agents"]):
            if item["name"] in self.service.agents:
                self.service.delete_agent(item["name"])
        self._journal(record, "rolled_back")

    def recover_import(self, import_id: str, *, expected_recovery_sha256: str, action: str) -> dict[str, Any]:
        if action not in ("rollback", "preserve"):
            raise PortabilityError("recovery action must be rollback or preserve")
        observed = self._record(import_id)
        ids = [item["bot_id"] for item in observed["bots"]]
        with self._resource_guard(ids), self.service.bots._lock, self.service._lock:
            record = self._record(import_id)
            if ids != [item["bot_id"] for item in record["bots"]]:
                raise PortabilityError("import resources changed; review a fresh recovery plan", code="conflict", status=409)
            preview = self._inspect(record)
            if expected_recovery_sha256 != preview["recovery_sha256"] or record["status"] not in _PENDING:
                raise PortabilityError("recovery preview changed or import is already resolved", code="conflict", status=409)
            if action == "rollback":
                if not preview["rollback_available"]:
                    raise PortabilityError(
                        "unproven, changed or active resources cannot be rolled back automatically", code="conflict", status=409
                    )
                self._rollback(record)
            else:
                # This acknowledges a partial import; it never resumes work or
                # claims all definitions/notes were successfully installed.
                self._journal(record, "recovered_preserved")
            return {"import_id": import_id, "status": record["status"], "resources_preserved": action == "preserve"}
