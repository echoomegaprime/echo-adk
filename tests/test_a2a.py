from __future__ import annotations

import pytest

from echo_adk import __version__
from echo_adk.a2a import a2a_to_echo_card, agent_card, echo_to_a2a_card, from_echo_a2a, to_echo_a2a, validate_a2a_card
from echo_adk.core.types import ValidationError


def test_agent_card_shape(scout, registry):
    card = agent_card(scout, base_url="http://127.0.0.1:8378", registry=registry)
    assert card["name"] == "knowledge-scout" and card["version"] == __version__
    assert card["supportedInterfaces"] == [
        {"url": "http://127.0.0.1:8378/a2a/knowledge-scout", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ]
    assert card["capabilities"]["streaming"] is False and card["capabilities"]["pushNotifications"] is False
    ext = card["capabilities"]["extensions"][0]
    assert (
        ext["uri"] == "urn:echo:adk:agent:v1"
        and ext["params"]["role"] == "researcher"
        and set(ext["params"]["invariants"]) == {"R1", "R2", "R3", "R4", "R5"}
    )
    assert card["defaultInputModes"] == ["text/plain"] and card["securitySchemes"] == {}
    skill_ids = [s["id"] for s in card["skills"]]
    assert skill_ids == ["echo.knowledge.search", "echo.knowledge.get", "echo.caps.search", "skill:echo-researcher-power"]
    assert "tier-0" in card["skills"][0]["tags"] and card["provider"]["organization"] == "Echo Prime Tech LLC"
    assert validate_a2a_card(card) == card
    with pytest.raises(ValidationError):
        agent_card(scout, base_url="not a url")


def test_echo_a2a_card_and_translation_round_trip(scout, registry):
    echo = to_echo_a2a(scout, endpoint="http://127.0.0.1:8378", registry=registry)
    assert echo["protocol"] == "echo-a2a/1.0" and echo["url"] == "http://127.0.0.1:8378"
    assert [c["id"] for c in echo["capabilities"]] == list(scout.capabilities) and echo["agents"][0]["agent_id"] == "knowledge-scout"
    parsed = from_echo_a2a(echo)
    assert parsed["capabilities"] == list(scout.capabilities) and parsed["skills"] == ["echo-researcher-power"]
    a2a = echo_to_a2a_card(echo)
    assert validate_a2a_card(a2a)["name"] == "knowledge-scout"
    assert [s["id"] for s in a2a["skills"]] == list(scout.capabilities) + ["skill:echo-researcher-power"]
    back = a2a_to_echo_card(a2a)
    assert [c["id"] for c in back["capabilities"]] == list(scout.capabilities) and back["skills"][0]["id"] == "echo-researcher-power"
    assert back["protocol"] == "echo-a2a/1.0"
    native = a2a_to_echo_card(agent_card(scout, registry=registry))
    assert [c["id"] for c in native["capabilities"]] == list(scout.capabilities)


def test_translator_refuses_wrong_shapes():
    with pytest.raises(ValidationError):
        from_echo_a2a({"name": "x", "protocol": "agent2agent/1.0"})
    with pytest.raises(ValidationError):
        validate_a2a_card({"name": "x"})
    with pytest.raises(ValidationError):
        from_echo_a2a("nope")
