"""Immutable, host-owned browser policy. No caller can supply launch options."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .errors import BrowserError

PLAYWRIGHT_VERSION = "1.62.0"
MAX_TIMEOUT_MS = 30_000
MAX_SESSIONS = 4
MAX_TEXT_CHARS = 12_000
MAX_ELEMENTS = 100
MAX_FILL_CHARS = 4_096
MAX_ARTIFACT_BYTES = 512 * 1024
MAX_ARTIFACTS = 8
MAX_PROFILE_BYTES = 128 * 1024 * 1024
VIEWPORT = {"width": 1280, "height": 720}
PROBE_TTL_S = 60


def origin(value: str) -> str:
    """Canonicalize an exact http(s) origin; no wildcard, credentials or paths."""
    try:
        if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value) or "\\" in value:
            raise ValueError
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment or "*" in parsed.hostname:
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if not host or "%" in host:
            raise ValueError
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError
        authority = f"[{host}]" if ":" in host else host
        if port != (443 if parsed.scheme == "https" else 80):
            authority += f":{port}"
        return f"{parsed.scheme}://{authority}"
    except (ValueError, UnicodeError, AttributeError):
        raise BrowserError("invalid_config", 400, "Browser origins must be exact http(s) origins without credentials or paths") from None


@dataclass(frozen=True)
class BrowserConfig:
    enabled: bool = False
    allowed_origins: tuple[str, ...] = ()
    private_origins: tuple[str, ...] = ()
    executable: str | None = None
    chromium_sandbox: bool = True

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.chromium_sandbox) is not bool:
            raise BrowserError("invalid_config", 400, "Browser enablement and sandbox flags must be booleans")
        for field in ("allowed_origins", "private_origins"):
            values = getattr(self, field)
            if not isinstance(values, (tuple, list)) or len(values) > 64:
                raise BrowserError("invalid_config", 400, "Browser origin lists must contain at most 64 exact origins")
            object.__setattr__(self, field, tuple(dict.fromkeys(origin(v) for v in values)))
        if not set(self.private_origins).issubset(self.allowed_origins):
            raise BrowserError("invalid_config", 400, "Private browser origins must be a subset of allowed origins")
        if self.executable is not None and (not isinstance(self.executable, str) or not Path(self.executable).is_absolute()):
            raise BrowserError("invalid_config", 400, "Browser executable must be an absolute host-owned path")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> BrowserConfig:
        env = os.environ if env is None else env

        def flag(key: str, default: str) -> bool:
            value = env.get(key, default)
            if value not in ("0", "1"):
                raise BrowserError("invalid_config", 400, f"{key} must be 0 or 1")
            return value == "1"

        def origins(key: str) -> tuple:
            try:
                value = json.loads(env.get(key, "[]"))
            except (ValueError, TypeError):
                raise BrowserError("invalid_config", 400, f"{key} must be a JSON list of exact origins") from None
            if not isinstance(value, list):
                raise BrowserError("invalid_config", 400, f"{key} must be a JSON list of exact origins")
            return tuple(value)

        return cls(
            enabled=flag("ECHO_ADK_BROWSER_ENABLED", "0"),
            allowed_origins=origins("ECHO_ADK_BROWSER_ALLOWED_ORIGINS"),
            private_origins=origins("ECHO_ADK_BROWSER_PRIVATE_ORIGINS"),
            executable=env.get("ECHO_ADK_BROWSER_EXECUTABLE") or None,
            chromium_sandbox=flag("ECHO_ADK_BROWSER_CHROMIUM_SANDBOX", "1"),
        )
