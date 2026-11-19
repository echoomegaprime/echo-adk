"""echo-adk-api: the frozen loopback HTTP contract consumed by Echo Prime (docs/CONTRACT.md)."""

from __future__ import annotations

from .server import BackgroundServer, make_server, serve
from .service import AdkService, ApiError

__all__ = ["AdkService", "ApiError", "BackgroundServer", "make_server", "serve"]
