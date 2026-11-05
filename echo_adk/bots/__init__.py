"""echoBots: always-on governed agents (per-bot Lease, per-bot memory scope, daily cost cap)."""

from __future__ import annotations

from .manager import BotManager, bot_lease_template

__all__ = ["BotManager", "bot_lease_template"]
