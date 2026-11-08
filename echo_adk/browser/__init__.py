"""Opt-in per-bot Chromium sessions; core imports remain standard-library only."""

from .config import BrowserConfig
from .errors import BrowserError
from .runtime import BrowserManager

__all__ = ["BrowserConfig", "BrowserError", "BrowserManager"]
