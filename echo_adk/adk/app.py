"""``app(agent) -> App``: the same EchoAgent file, run by Google ADK (``adk web/run/api_server``)."""

from __future__ import annotations

from typing import Any, Optional

from google.adk.agents.llm_agent import LlmAgent
from google.adk.apps.app import App

from ..core.broker import Broker
from ..core.ledger import Ledger
from ..core.models import Model
from ..core.policy import CapabilityRegistry
from ..core.types import EchoAgent, Lease
from .model import EchoModel
from .plugin import EchoGovernancePlugin
from .session import GovernedSession
from .toolset import EchoToolset


def app(
    agent: EchoAgent,
    *,
    model: Model,
    broker: Broker,
    registry: Optional[CapabilityRegistry] = None,
    lease: Optional[Lease] = None,
    ledger: Optional[Ledger] = None,
    session: Optional[GovernedSession] = None,
) -> Any:
    """Build an ADK ``App`` whose root ``LlmAgent`` uses EchoModel + EchoToolset under EchoGovernancePlugin."""
    governed = session or GovernedSession(agent, broker=broker, registry=registry, lease=lease, ledger=ledger)
    root = LlmAgent(
        name=agent.name.replace("-", "_"),
        description=f"Echo agent {agent.name} (role {agent.role})",
        model=EchoModel(model, session=governed),
        instruction=agent.instruction or f"You are {agent.name}, an Echo agent with role {agent.role}.",
        tools=[EchoToolset(governed)],
    )
    return App(name=agent.name.replace("-", "_"), root_agent=root, plugins=[EchoGovernancePlugin(governed)])
