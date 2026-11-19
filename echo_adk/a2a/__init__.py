"""Agent cards: A2A (Agent2Agent, ``/.well-known/agent-card.json``) and ``echo-a2a/1.0`` (``agent.json``)."""

from __future__ import annotations

from .card import (
    A2A_PROTOCOL_VERSION,
    ECHO_A2A_PROTOCOL,
    a2a_to_echo_card,
    agent_card,
    echo_to_a2a_card,
    from_echo_a2a,
    to_echo_a2a,
    validate_a2a_card,
)

__all__ = [
    "A2A_PROTOCOL_VERSION",
    "ECHO_A2A_PROTOCOL",
    "a2a_to_echo_card",
    "agent_card",
    "echo_to_a2a_card",
    "from_echo_a2a",
    "to_echo_a2a",
    "validate_a2a_card",
]
