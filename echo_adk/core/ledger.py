"""Append-only JSONL ledger, Proof files and scoped memory under a state directory.

Layout::

    <state_dir>/ledger/<run_id>.jsonl   one Envelope per line, append-only
    <state_dir>/proofs/<run_id>.json    the Proof of the run
    <state_dir>/memory/<scope>.jsonl    memory records for one scope (``bot:<id>``, ``agent:<name>``)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Optional

from .redact import redact_value
from .types import Envelope, Proof, ValidationError, is_id, new_id, now_iso

_SCOPE = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,96}$")
MAX_MEMORY_RECORD_BYTES = 128 * 1024
MAX_MEMORY_SCAN_BYTES = 4 * 1024 * 1024


def _sync_directory(path: Path) -> None:
    """Persist directory entries on POSIX; Windows supports file flush only."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def safe_scope(scope: str) -> str:
    if not isinstance(scope, str) or not _SCOPE.fullmatch(scope):
        raise ValidationError("memory scope must match [a-z0-9][a-z0-9:_-]{0,96}")
    # '%' cannot occur in a valid scope, so this is injective. The previous
    # colon -> double-underscore encoding aliased distinct scopes.
    return scope.replace(":", "%3A")


class Ledger:
    def __init__(self, state_dir: str | Path) -> None:
        self.root = Path(state_dir)
        (self.root / "ledger").mkdir(parents=True, exist_ok=True)
        (self.root / "proofs").mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _ledger_path(self, run_id: str) -> Path:
        if not is_id(run_id):
            raise ValidationError("run_id must be 32 lowercase hex characters")
        return self.root / "ledger" / f"{run_id}.jsonl"

    def append(self, envelope: Envelope) -> None:
        line = json.dumps(envelope.to_dict(), ensure_ascii=False, separators=(",", ":"))
        path = self._ledger_path(envelope.run_id)
        with self._lock:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def read(self, run_id: str) -> list[Envelope]:
        path = self._ledger_path(run_id)
        if not path.exists():
            return []
        out: list[Envelope] = []
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    out.append(Envelope.from_dict(json.loads(line)))
        return out

    def write_proof(self, proof: Proof) -> Path:
        path = self.root / "proofs" / f"{proof.run_id}.json"
        with self._lock:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(proof.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        return path

    def read_proof(self, run_id: str) -> Optional[Proof]:
        if not is_id(run_id):
            raise ValidationError("run_id must be 32 lowercase hex characters")
        path = self.root / "proofs" / f"{run_id}.json"
        if not path.exists():
            return None
        return Proof.from_dict(json.loads(path.read_text(encoding="utf-8")))


class MemoryStore:
    """Durable local memory with exact scope filtering, including legacy files.

    This is local storage, not a Crystal/ECF acknowledgement. A write receipt
    binds the redacted record bytes after flush/fsync; it grants no authority.
    """

    def __init__(self, state_dir: str | Path) -> None:
        self.root = Path(state_dir) / "memory"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, scope: str, record: dict[str, Any]) -> dict[str, Any]:
        path = self.root / f"{safe_scope(scope)}.jsonl"
        cleaned, redacted = redact_value(dict(record))
        cleaned.pop("sha256", None)
        entry = {**cleaned, "ts": now_iso(), "scope": scope, "redacted": redacted, "record_id": new_id()}
        entry["sha256"] = self._digest(entry)
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
        if len(line.encode("utf-8")) > MAX_MEMORY_RECORD_BYTES:
            raise ValidationError("memory record exceeds 128 KiB")
        with self._lock:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            _sync_directory(self.root)
        return entry

    @staticmethod
    def _digest(entry: dict[str, Any]) -> str:
        body = {k: v for k, v in entry.items() if k != "sha256"}
        return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()

    def _paths(self, scope: str) -> list[Path]:
        encoded = safe_scope(scope)
        return list(dict.fromkeys((self.root / f"{scope.replace(':', '__')}.jsonl", self.root / f"{encoded}.jsonl")))

    @staticmethod
    def _entries(path: Path, *, tail: bool = False) -> Iterable[dict[str, Any]]:
        if not path.exists():
            return
        with path.open("rb") as handle:
            if tail:
                start = max(0, os.fstat(handle.fileno()).st_size - MAX_MEMORY_SCAN_BYTES)
                if start:
                    handle.seek(start - 1)
                    if handle.read(1) != b"\n":
                        # The scan starts inside a record. Drop that oldest
                        # partial record, never pass partial JSON to the model.
                        if len(handle.readline(MAX_MEMORY_RECORD_BYTES + 1)) > MAX_MEMORY_RECORD_BYTES:
                            raise ValidationError("memory record exceeds 128 KiB")
            while True:
                line = handle.readline(MAX_MEMORY_RECORD_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_MEMORY_RECORD_BYTES:
                    raise ValidationError("memory record exceeds 128 KiB")
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if not isinstance(entry, dict) or not isinstance(entry.get("scope"), str):
                        raise ValueError("unscoped record")
                    safe_scope(entry["scope"])
                    if "sha256" in entry and entry["sha256"] != MemoryStore._digest(entry):
                        raise ValueError("record digest mismatch")
                except (ValueError, TypeError, UnicodeError):
                    raise ValidationError("local memory contains an invalid record; repair it before resuming") from None
                yield entry

    def read(self, scope: str, limit: int = 50) -> list[dict[str, Any]]:
        paths = self._paths(scope)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValidationError("memory limit must be a positive integer")
        out: deque[dict[str, Any]] = deque(maxlen=min(limit, 1000))
        with self._lock:
            for path in paths:
                out.extend(entry for entry in self._entries(path, tail=True) if entry["scope"] == scope)
        return list(out)

    def scopes(self) -> Iterable[str]:
        found: set[str] = set()
        with self._lock:
            for path in self.root.glob("*.jsonl"):
                for entry in self._entries(path):
                    scope = entry["scope"]
                    if path in self._paths(scope):
                        found.add(scope)
        return iter(sorted(found))

    def delete(self, scope: str) -> bool:
        """Remove a scope's memory entirely (bot deletion leaves nothing behind)."""
        paths = self._paths(scope)
        removed = False
        with self._lock:
            for path in paths:
                if not path.exists():
                    continue
                # A legacy filename can contain two distinct scopes. Preserve
                # the other scope's records when erasing only this bot.
                tmp = path.with_suffix(f".{new_id()}.tmp")
                kept = 0
                try:
                    with tmp.open("w", encoding="utf-8") as handle:
                        for entry in self._entries(path):
                            if entry["scope"] == scope:
                                removed = True
                                continue
                            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                            kept += 1
                        handle.flush()
                        os.fsync(handle.fileno())
                    if kept:
                        os.replace(tmp, path)
                    else:
                        path.unlink()
                finally:
                    tmp.unlink(missing_ok=True)
            _sync_directory(self.root)
        return removed
