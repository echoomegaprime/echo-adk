"""Structured JSON logging to stderr. ``ECHO_ADK_DEBUG=1`` enables debug records.

Nothing here ever receives a credential: callers log capability ids, status codes and durations,
never request headers or raw bodies.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

from .redact import redact_value

_CONFIGURED = False


def debug_enabled() -> bool:
    return os.environ.get("ECHO_ADK_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "+00:00",
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            cleaned, _ = redact_value(extra)
            payload.update(cleaned)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)[-2000:]
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure(stream: Any = None, *, force: bool = False) -> None:
    """Install the JSON handler on the ``echo_adk`` logger (idempotent)."""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return
    root = logging.getLogger("echo_adk")
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.propagate = False
    root.setLevel(logging.DEBUG if debug_enabled() else logging.INFO)
    _CONFIGURED = True


def get_logger(name: str) -> EchoLogger:
    configure()
    return EchoLogger(logging.getLogger(f"echo_adk.{name}"))


class EchoLogger:
    """Thin wrapper: ``log.info("event", key=value)`` emits one JSON line."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _emit(self, level: int, event: str, exc_info: bool = False, **fields: Any) -> None:
        if self._logger.isEnabledFor(level):
            self._logger.log(level, event, extra={"fields": fields}, exc_info=exc_info)

    def debug(self, event: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit(logging.WARNING, event, **fields)

    def error(self, event: str, exc_info: bool = False, **fields: Any) -> None:
        self._emit(logging.ERROR, event, exc_info=exc_info, **fields)
