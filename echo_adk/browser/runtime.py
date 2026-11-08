"""Optional per-bot browser facade; the service owns auth and capability leases."""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from echo_adk.core.deadline import io_timeout
from echo_adk.core.redact import redact

from .config import MAX_FILL_CHARS, MAX_SESSIONS, MAX_TIMEOUT_MS, PLAYWRIGHT_VERSION, PROBE_TTL_S, BrowserConfig
from .errors import BrowserError
from .policy import NetworkPolicy
from .process import Worker
from .storage import BrowserStorage, identifier


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _command(value: dict) -> dict:
    if not isinstance(value, dict):
        raise BrowserError("invalid_request", 400, "Browser action must be an object")
    action = value.get("action")
    fields = {
        "navigate": {"url"},
        "snapshot": set(),
        "click": {"ref"},
        "fill": {"ref", "value"},
        "screenshot": set(),
    }
    if not isinstance(action, str) or action not in fields:
        raise BrowserError("invalid_action", 400, "Browser action must be navigate, snapshot, click, fill or screenshot")
    allowed = fields[action] | {"action", "timeout_ms"}
    if set(value) - allowed or not fields[action].issubset(value):
        raise BrowserError("invalid_request", 400, "Browser action has missing or unsupported fields")
    timeout = value.get("timeout_ms", MAX_TIMEOUT_MS)
    if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT_MS:
        raise BrowserError("invalid_timeout", 400, "Browser timeout_ms must be an integer from 1 to 30000")
    if "url" in value and (not isinstance(value["url"], str) or len(value["url"]) > 8192):
        raise BrowserError("invalid_url", 400, "Browser URL must be a string of at most 8192 characters")
    if "ref" in value:
        identifier(value["ref"], "reference")
    if "value" in value:
        if not isinstance(value["value"], str) or len(value["value"]) > MAX_FILL_CHARS or "\x00" in value["value"]:
            raise BrowserError("invalid_value", 400, "Browser fill value must be text of at most 4096 characters")
        if redact(value["value"])[0] != value["value"]:
            raise BrowserError("credential_value", 403, "Recognizable credentials cannot be supplied to browser fill")
    return {**value, "timeout_ms": timeout}


class BrowserManager:
    def __init__(self, state_dir: Path, config: BrowserConfig | None = None):
        self.config = config or BrowserConfig()
        self.storage = BrowserStorage(state_dir)
        self._guard = threading.RLock()
        self._probe_lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._sessions: dict[str, Worker] = {}
        self._starting: set[str] = set()
        self._cancel: dict[str, threading.Event] = {}
        self._last: dict[str, dict] = {}
        self._closed = False
        self._probe_at = 0.0
        self._verified_at = None
        self._checked_at = None
        self._browser_version = None
        self._available = False
        self._reason = self._dependency_reason() or "Browser runtime has not yet been verified by a real launch"

    def _dependency_reason(self):
        if not self.config.enabled:
            return "Browser provider is disabled by host configuration"
        if os.name != "nt" and not sys.platform.startswith("linux"):
            return "Browser process cleanup is supported only on Linux and Windows"
        try:
            version = importlib.metadata.version("playwright")
        except importlib.metadata.PackageNotFoundError:
            return f"Install the optional browser extra (playwright=={PLAYWRIGHT_VERSION}) and Chromium"
        if version != PLAYWRIGHT_VERSION:
            return f"Browser provider requires the tested playwright=={PLAYWRIGHT_VERSION} extra"
        if self.config.executable and not Path(self.config.executable).is_file():
            return "Configured browser executable is unavailable"
        return None

    def health(self) -> dict:
        """Nonblocking cached state. This method NEVER launches Chromium."""
        with self._guard:
            stale = bool(self._probe_at and time.monotonic() - self._probe_at >= PROBE_TTL_S)
            return {
                "enabled": self.config.enabled,
                "available": self._available and not self._closed and not stale,
                "provider": "playwright",
                "browser": "chromium",
                "browser_version": self._browser_version,
                "isolation": "process_and_profile",
                "os_isolation": False,
                "chromium_sandbox": self.config.chromium_sandbox,
                "verified_at": self._verified_at,
                "checked_at": self._checked_at,
                "probe_ttl_s": PROBE_TTL_S,
                "reason": "Browser manager is shut down"
                if self._closed
                else "Browser verification expired; request status to recheck"
                if stale
                else self._reason,
                "verification_stale": stale,
                "network_policy": {
                    "mode": "exact_origin_allowlist",
                    "allowed_origins": list(self.config.allowed_origins),
                    "private_origins": list(self.config.private_origins),
                    "idle_network": False,
                },
                "limits": {
                    "open_sessions": MAX_SESSIONS,
                    "timeout_ms": MAX_TIMEOUT_MS,
                    "screenshot_bytes": 512 * 1024,
                    "artifacts_per_bot": 8,
                },
            }

    def _verified(self, response: dict):
        with self._guard:
            self._available = True
            self._reason = None
            self._probe_at = time.monotonic()
            self._verified_at = self._checked_at = _now()
            self._browser_version = response.get("browser_version", self._browser_version)

    def probe(self) -> dict:
        """A real, inert Chromium launch, cached for 60 seconds; at most 10s."""
        if not self._probe_lock.acquire(blocking=False):
            return self.health()
        try:
            with self._guard:
                if self._closed or self._probe_at and time.monotonic() - self._probe_at < PROBE_TTL_S:
                    return self.health()
                reason = self._dependency_reason()
                if reason:
                    self._available = False
                    self._reason = reason
                    self._checked_at = _now()
                    self._probe_at = time.monotonic()
                    return self.health()
            directory = Path(tempfile.mkdtemp(prefix="probe-", dir=self.storage.probes))
            worker = None
            deadline = time.monotonic() + 10
            try:
                worker = Worker(directory)
                response = worker.request(
                    {"action": "init", "profile": str(directory / "profile"), "config": asdict(self.config), "timeout_ms": 9000}, deadline
                )
                self._verified(response)
            except (BrowserError, OSError):
                with self._guard:
                    self._available = False
                    self._reason = "Chromium launch failed with the configured sandbox and egress policy; check host installation"
                    self._checked_at = _now()
                    self._probe_at = time.monotonic()
            finally:
                if worker:
                    worker.close(max(deadline, time.monotonic() + 0.1))
                shutil.rmtree(directory, ignore_errors=True)
            return self.health()
        finally:
            self._probe_lock.release()

    def status(self, bot_id: str) -> dict:
        identifier(bot_id)
        result = self.probe()
        with self._guard:
            worker = self._sessions.get(bot_id)
            active = bool(worker and not worker.closed and worker.process.poll() is None)
            return {
                **result,
                "bot_id": bot_id,
                "status": "ready" if active else "closed",
                "profile_persistent": True,
                **self._last.get(bot_id, {}),
            }

    def has_resources(self, bot_id: str) -> bool:
        """Metadata only: rollback must preserve owner-added browser resources.

        No directory is created, no provider is imported/launched, and uncertain
        filesystem state is conservatively treated as an existing resource.
        The caller coordinates this read with its own workspace mutation lock.
        """
        identifier(bot_id)
        with self._guard:
            if bot_id in self._sessions or bot_id in self._starting or bot_id in self._cancel:
                return True
        folder = self.storage.bots / bot_id
        try:
            if folder.is_symlink():
                return True
            if not folder.exists():
                return False
            if not folder.is_dir():
                return True
            for entry in folder.iterdir():
                if entry.name == "profile.lock" and entry.is_file() and not entry.is_symlink():
                    continue
                if entry.name not in ("profile", "artifacts") or entry.is_symlink() or not entry.is_dir():
                    return True
                if next(entry.iterdir(), None) is not None:
                    return True
            return False
        except OSError:
            return True

    def _acquire(self, bot_id: str, deadline: float, cancel: threading.Event | None = None):
        with self._guard:
            lock = self._locks.setdefault(bot_id, threading.Lock())
        while True:
            if cancel is not None and cancel.is_set():
                raise BrowserError("cancelled", 499, "Browser operation cancelled before dispatch")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserError("timeout", 504, "Browser operation expired while waiting for this bot")
            if lock.acquire(timeout=min(0.05, remaining)):
                return lock

    def execute(self, bot_id: str, command: dict, *, cancel: threading.Event | None = None) -> dict:
        identifier(bot_id)
        command = _command(command)
        if command["action"] == "navigate":
            NetworkPolicy(self.config).validate(command["url"], navigation=True)
        try:
            timeout = io_timeout(command["timeout_ms"] / 1000)
        except TimeoutError:
            raise BrowserError("timeout", 504, "Browser operation expired before dispatch") from None
        deadline = time.monotonic() + timeout
        lock = self._acquire(bot_id, deadline, cancel)
        worker = None
        interrupted = threading.Event()

        class _Cancel:
            def is_set(self):
                return interrupted.is_set() or (cancel is not None and cancel.is_set())

        combined = _Cancel()
        try:
            with self._guard:
                if self._closed:
                    raise BrowserError("provider_unavailable", 503, "Browser manager is shut down")
                reason = self._dependency_reason()
                if reason:
                    raise BrowserError("provider_unavailable", 503, reason)
                worker = self._sessions.get(bot_id)
                if worker and (worker.closed or worker.process.poll() is not None):
                    worker.kill()
                    self._sessions.pop(bot_id, None)
                    worker = None
                if worker is None and len(self._sessions) + len(self._starting) >= MAX_SESSIONS:
                    raise BrowserError("session_limit", 413, "The host browser session limit is reached; close a bot browser first")
                if worker is None:
                    self._starting.add(bot_id)
                self._cancel[bot_id] = interrupted
            if combined.is_set():
                raise BrowserError("cancelled", 499, "Browser operation cancelled before dispatch")
            self.storage.profile_size(bot_id)
            if worker is None:
                if combined.is_set() or time.monotonic() >= deadline:
                    raise BrowserError(
                        "cancelled" if combined.is_set() else "timeout",
                        499 if combined.is_set() else 504,
                        "Browser startup expired or was cancelled before dispatch",
                    )
                worker = Worker(self.storage.directory(bot_id))
                with self._guard:
                    if self._closed or combined.is_set():
                        worker.kill()
                        raise BrowserError("cancelled", 499, "Browser startup cancelled before browser launch")
                    self._sessions[bot_id] = worker
                    self._starting.discard(bot_id)
                response = worker.request(
                    {
                        "action": "init",
                        "profile": str(self.storage.profile(bot_id)),
                        "config": asdict(self.config),
                        "timeout_ms": max(1, int((deadline - time.monotonic()) * 1000)),
                    },
                    deadline,
                    combined,
                )
                self._verified(response)
            command["timeout_ms"] = max(1, int((deadline - time.monotonic()) * 1000))
            result = worker.request(command, deadline, combined)
            if combined.is_set() or time.monotonic() >= deadline:
                raise BrowserError(
                    "cancelled" if combined.is_set() else "timeout",
                    499 if combined.is_set() else 504,
                    "Browser result arrived after cancellation or deadline; outcome must be inspected",
                    unknown_outcome=True,
                )
            self.storage.profile_size(bot_id)
            self._verified({})
            if "png_base64" in result:
                result = {"status": "ready", "artifact": self.storage.save_artifact(bot_id, result["png_base64"])}
            with self._guard:
                self._last.setdefault(bot_id, {}).update({key: result[key] for key in ("url", "title") if key in result})
            return {**result, "bot_id": bot_id, "action": command["action"], "untrusted": True}
        except BrowserError as exc:
            if worker and (exc.unknown_outcome or exc.status in (413, 499, 503, 504)):
                worker.kill()
                with self._guard:
                    self._sessions.pop(bot_id, None)
            raise
        finally:
            with self._guard:
                self._cancel.pop(bot_id, None)
                self._starting.discard(bot_id)
            lock.release()

    def close(self, bot_id: str) -> dict:
        identifier(bot_id)
        with self._guard:
            event = self._cancel.get(bot_id)
            if event:
                event.set()
        deadline = time.monotonic() + MAX_TIMEOUT_MS / 1000
        lock = self._acquire(bot_id, deadline)
        try:
            with self._guard:
                worker = self._sessions.pop(bot_id, None)
            if worker:
                worker.close(min(deadline, time.monotonic() + 5))
            return {"bot_id": bot_id, "status": "closed", "profile_persistent": True}
        finally:
            lock.release()

    def reset(self, bot_id: str) -> dict:
        identifier(bot_id)
        self.close(bot_id)
        lock = self._acquire(bot_id, time.monotonic() + MAX_TIMEOUT_MS / 1000)
        try:
            self.storage.reset(bot_id)
            with self._guard:
                self._last.pop(bot_id, None)
            return {"bot_id": bot_id, "status": "reset", "profile_removed": True, "artifacts_removed": True}
        finally:
            lock.release()

    def read_artifact(self, bot_id: str, artifact_id: str) -> dict:
        identifier(bot_id)
        lock = self._acquire(bot_id, time.monotonic() + MAX_TIMEOUT_MS / 1000)
        try:
            return self.storage.read_artifact(bot_id, artifact_id)
        finally:
            lock.release()

    def shutdown(self):
        with self._guard:
            self._closed = True
            for event in self._cancel.values():
                event.set()
            ids = list(self._sessions)
        for bot_id in ids:
            self.close(bot_id)
