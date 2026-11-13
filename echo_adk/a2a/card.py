"""Agent card generator and translator.

* :func:`agent_card` builds an Agent2Agent (A2A v1, a2a-sdk 1.x field names, camelCase JSON) card for an
  EchoAgent, the shape served at ``/.well-known/agent-card.json``.
* :func:`to_echo_a2a` / :func:`from_echo_a2a` produce and read Echo's own ``echo-a2a/1.0`` card
  (``/.well-known/agent.json``; types in ``echo-a2a-protocol/src/types.ts``).
* :func:`echo_to_a2a_card` / :func:`a2a_to_echo_card` translate between the two wires.

Same acronym, different protocols (research doc B.1.5); this module is the adapter.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Optional

from .. import __version__
from ..core.policy import CapabilityRegistry
from ..core.types import CAP_ID, EchoAgent, ValidationError

A2A_PROTOCOL_VERSION = "1.0"
ECHO_A2A_PROTOCOL = "echo-a2a/1.0"
ECHO_EXTENSION_URI = "urn:echo:adk:agent:v1"
PROVIDER = {"organization": "Echo Prime Tech LLC", "url": "https://github.com/echoomegaprime/echo-adk"}
INVARIANTS = {
    "R1": "agents never hold tokens",
    "R2": "no silent provider fallback",
    "R3": "lease lists exact capability ids",
    "R4": "every run emits a Proof",
    "R5": "fail-closed default-deny tool registry",
}
_URL = re.compile(r"^https?://[^\s/]+(?:/[^\s]*)?$")


def _description(agent: EchoAgent) -> str:
    return (agent.instruction or f"Echo agent {agent.name} (role {agent.role})").strip()[:1024]


def _skills(agent: EchoAgent, registry: Optional[CapabilityRegistry]) -> list[dict[str, Any]]:
    reg = registry or CapabilityRegistry.builtin()
    skills: list[dict[str, Any]] = []
    for cap_id in agent.capabilities:
        cap = reg.get(cap_id)
        skills.append(
            {
                "id": cap_id,
                "name": cap_id,
                "description": (cap.description if cap and cap.description else f"Echo capability {cap_id}")[:1024],
                "tags": ["echo", "capability", f"tier-{cap.tier}" if cap else "tier-unknown", cap.scope if cap else "unregistered"],
                "examples": [],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            }
        )
    for skill in agent.skills:
        skills.append(
            {
                "id": f"skill:{skill}",
                "name": skill,
                "description": f"Agent Skill {skill} (Agent Skills specification directory)",
                "tags": ["echo", "agent-skill"],
                "examples": [],
            }
        )
    return skills


def agent_card(
    agent: EchoAgent,
    *,
    base_url: str = "http://127.0.0.1:8378",
    version: str = __version__,
    registry: Optional[CapabilityRegistry] = None,
) -> dict[str, Any]:
    """The Agent2Agent card served at ``/.well-known/agent-card.json``."""
    if not _URL.match(base_url):
        raise ValidationError("base_url must be an http(s) URL")
    base = base_url.rstrip("/")
    return {
        "name": agent.name,
        "description": _description(agent),
        "version": version,
        "provider": dict(PROVIDER),
        "documentationUrl": "https://github.com/echoomegaprime/echo-adk/blob/main/README.md",
        "supportedInterfaces": [
            {"url": f"{base}/a2a/{agent.name}", "protocolBinding": "JSONRPC", "protocolVersion": A2A_PROTOCOL_VERSION},
        ],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extendedAgentCard": False,
            "extensions": [
                {
                    "uri": ECHO_EXTENSION_URI,
                    "description": "Echo ADK governance metadata (role, lane, budget, invariants)",
                    "required": False,
                    "params": {
                        "role": agent.role,
                        "lane": agent.lane.to_dict(),
                        "max_steps": agent.max_steps,
                        "capabilities": list(agent.capabilities),
                        "invariants": dict(INVARIANTS),
                        "untrusted_output": True,
                    },
                }
            ],
        },
        "securitySchemes": {},
        "securityRequirements": [],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": _skills(agent, registry),
    }


def validate_a2a_card(card: Any) -> dict[str, Any]:
    if not isinstance(card, Mapping):
        raise ValidationError("agent card must be an object")
    for key in ("name", "description", "version", "capabilities", "skills"):
        if key not in card:
            raise ValidationError(f"agent card is missing {key!r}")
    if not isinstance(card["skills"], list):
        raise ValidationError("agent card skills must be a list")
    if not isinstance(card["capabilities"], Mapping):
        raise ValidationError("agent card capabilities must be an object")
    return dict(card)


# --------------------------------------------------------------------------- echo-a2a/1.0


def to_echo_a2a(
    agent: EchoAgent, *, endpoint: str = "http://127.0.0.1:8378", version: str = __version__, registry: Optional[CapabilityRegistry] = None
) -> dict[str, Any]:
    """Echo's own card (``AgentCard`` in echo-a2a-protocol ``types.ts``)."""
    if not _URL.match(endpoint):
        raise ValidationError("endpoint must be an http(s) URL")
    reg = registry or CapabilityRegistry.builtin()
    caps = []
    for cap_id in agent.capabilities:
        cap = reg.get(cap_id)
        caps.append(
            {"id": cap_id, "name": cap_id, "description": cap.description if cap and cap.description else f"Echo capability {cap_id}"}
        )
    return {
        "name": agent.name,
        "description": _description(agent),
        "url": endpoint.rstrip("/"),
        "version": version,
        "capabilities": caps,
        "skills": [{"id": s, "name": s, "description": f"Agent Skill {s}"} for s in agent.skills],
        "agents": [
            {
                "agent_id": agent.name,
                "name": agent.name,
                "status": "online",
                "capabilities": list(agent.capabilities),
                "endpoint": f"{endpoint.rstrip('/')}/agents/{agent.name}",
            }
        ],
        "protocol": ECHO_A2A_PROTOCOL,
    }


def from_echo_a2a(card: Any) -> dict[str, Any]:
    """Read an ``echo-a2a/1.0`` card into ``{name, description, url, version, capabilities, skills}``."""
    if not isinstance(card, Mapping):
        raise ValidationError("echo-a2a card must be an object")
    protocol = card.get("protocol")
    if not isinstance(protocol, str) or not protocol.startswith("echo-a2a/"):
        raise ValidationError("echo-a2a card must declare protocol echo-a2a/<version>")
    name = card.get("name")
    if not isinstance(name, str) or not name:
        raise ValidationError("echo-a2a card needs a name")
    capabilities = []
    for item in card.get("capabilities") or []:
        cap_id = item.get("id") if isinstance(item, Mapping) else item
        if isinstance(cap_id, str) and CAP_ID.match(cap_id):
            capabilities.append(cap_id)
    skills = []
    for item in card.get("skills") or []:
        skill_id = item.get("id") if isinstance(item, Mapping) else item
        if isinstance(skill_id, str) and skill_id:
            skills.append(skill_id)
    return {
        "name": name,
        "description": str(card.get("description") or ""),
        "url": str(card.get("url") or ""),
        "version": str(card.get("version") or ""),
        "capabilities": capabilities,
        "skills": skills,
        "protocol": protocol,
    }


def echo_to_a2a_card(echo_card: Any) -> dict[str, Any]:
    """Translate an ``echo-a2a/1.0`` card to an Agent2Agent card."""
    info = from_echo_a2a(echo_card)
    url = info["url"] or "http://127.0.0.1:8378"
    skills = [
        {"id": cap_id, "name": cap_id, "description": f"Echo capability {cap_id}", "tags": ["echo", "capability"], "examples": []}
        for cap_id in info["capabilities"]
    ] + [
        {"id": f"skill:{s}", "name": s, "description": f"Agent Skill {s}", "tags": ["echo", "agent-skill"], "examples": []}
        for s in info["skills"]
    ]
    return {
        "name": info["name"],
        "description": info["description"] or f"Echo agent {info['name']}",
        "version": info["version"] or "0",
        "provider": dict(PROVIDER),
        "supportedInterfaces": [
            {"url": f"{url.rstrip('/')}/a2a/{info['name']}", "protocolBinding": "JSONRPC", "protocolVersion": A2A_PROTOCOL_VERSION}
        ],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extendedAgentCard": False,
            "extensions": [
                {
                    "uri": ECHO_EXTENSION_URI,
                    "description": "translated from echo-a2a/1.0",
                    "required": False,
                    "params": {"source_protocol": info["protocol"]},
                }
            ],
        },
        "securitySchemes": {},
        "securityRequirements": [],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": skills,
    }


def a2a_to_echo_card(card: Any) -> dict[str, Any]:
    """Translate an Agent2Agent card to Echo's ``echo-a2a/1.0`` card (capabilities = skill ids that are cap ids)."""
    a2a = validate_a2a_card(card)
    url = ""
    interfaces = a2a.get("supportedInterfaces") or []
    if interfaces and isinstance(interfaces[0], Mapping) and isinstance(interfaces[0].get("url"), str):
        url = interfaces[0]["url"]
    elif isinstance(a2a.get("url"), str):  # a2a-sdk 0.3 cards carry a top-level url
        url = a2a["url"]
    caps, skills = [], []
    for skill in a2a["skills"]:
        if not isinstance(skill, Mapping):
            continue
        skill_id = str(skill.get("id") or "")
        entry = {"id": skill_id, "name": str(skill.get("name") or skill_id), "description": str(skill.get("description") or "")}
        if CAP_ID.match(skill_id):
            caps.append(entry)
        elif skill_id.startswith("skill:"):
            skills.append({**entry, "id": skill_id[len("skill:") :], "name": skill_id[len("skill:") :]})
        elif skill_id:
            skills.append(entry)
    return {
        "name": str(a2a["name"]),
        "description": str(a2a["description"]),
        "url": url,
        "version": str(a2a["version"]),
        "capabilities": caps,
        "skills": skills,
        "agents": [
            {
                "agent_id": str(a2a["name"]),
                "name": str(a2a["name"]),
                "status": "online",
                "capabilities": [c["id"] for c in caps],
                "endpoint": url,
            }
        ],
        "protocol": ECHO_A2A_PROTOCOL,
    }
