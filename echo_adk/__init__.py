"""Echo Agent Development Kit.

Echo-native agent core (Role, Skill, Capability, Lane, Lease, Proof) with Google ADK,
MCP and A2A adapters. The core is standard-library only; ``echo_adk.adk`` is an optional
extra (``pip install echo-adk[adk]``).
"""

from __future__ import annotations

__version__ = "0.1.0"
PROFILE = "ECHO_ADK"
SERVICE_NAME = "echo-adk-api"

__all__ = ["__version__", "PROFILE", "SERVICE_NAME"]
