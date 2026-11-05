"""Zero-dependency stdio MCP server exposing one EchoAgent's allowed capabilities as tools."""

from __future__ import annotations

from .server import PROTOCOL_VERSION, McpServer, cap_to_tool_name, serve_stdio

__all__ = ["McpServer", "PROTOCOL_VERSION", "cap_to_tool_name", "serve_stdio"]
