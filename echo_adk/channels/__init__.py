"""Chat channels for echoBots. OpenClaw is a transport (messages in and out), never a runtime."""

from __future__ import annotations

from .openclaw import (
    ChannelError,
    CliTransport,
    GatewayProbe,
    Health,
    OpenClawChannels,
    OpenClawConfig,
    SendResult,
    parse_target,
    resolve_cli,
    scan_wedges,
    validate_channels,
)

__all__ = [
    "ChannelError",
    "CliTransport",
    "GatewayProbe",
    "Health",
    "OpenClawChannels",
    "OpenClawConfig",
    "SendResult",
    "parse_target",
    "resolve_cli",
    "scan_wedges",
    "validate_channels",
]
