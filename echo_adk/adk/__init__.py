"""Google ADK adapter (optional extra: ``pip install echo-adk[adk]``, Python >= 3.11 for google-adk).

Import-guarded: the core never imports ``google.adk``. Symbols are grounded in google-adk 2.13.0
(``BaseLlm.generate_content_async``, ``BaseToolset.get_tools``, ``BaseTool.run_async``,
``BasePlugin`` keyword-only hooks, ``App(name, root_agent, plugins)``).
"""

from __future__ import annotations

import importlib

INSTALL_HINT = "echo_adk.adk needs google-adk: pip install 'echo-adk[adk]' (requires Python >= 3.11); the core works without it"

try:
    importlib.import_module("google.adk")
except Exception:  # ImportError, or a broken partial install: either way the adapter is unavailable
    ADK_AVAILABLE = False
else:
    ADK_AVAILABLE = True

if ADK_AVAILABLE:
    from .app import app
    from .model import EchoModel, build_prompt
    from .plugin import EchoGovernancePlugin
    from .session import GovernedSession
    from .toolset import EchoCapabilityTool, EchoToolset, tool_name

    __all__ = [
        "ADK_AVAILABLE",
        "EchoCapabilityTool",
        "EchoGovernancePlugin",
        "EchoModel",
        "EchoToolset",
        "GovernedSession",
        "app",
        "build_prompt",
        "tool_name",
    ]
else:
    __all__ = ["ADK_AVAILABLE", "INSTALL_HINT"]

    def __getattr__(name: str):  # pragma: no cover - exercised in a subprocess test
        raise ImportError(INSTALL_HINT)
