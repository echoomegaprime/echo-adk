"""Editable, untrusted per-bot notes with optimistic revisions and a local audit chain.

These notes are distinct from append-only runtime memories. The API host must
verify the owner, bot existence and paused/drained state before a mutation.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..core.redact import redact
from ..core.types import canonical_json, is_id, now_iso

MAX_MEMORY_CHARS = 16000
MAX_MEMORY_BYTES = 32768
MAX_REVISIONS = 1000
_EMPTY_HASH = hashlib.sha256(b"").hexdigest()


class BotMemoryError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _bot(value: Any) -> str:
    if not is_id(value):
        raise BotMemoryError("bot_id must be an exact bot identifier")
    return value


def _revision(value: Any, *, maximum: int = MAX_REVISIONS) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise BotMemoryError(f"revision must be an integer from 0 through {maximum}")
    return value


def clean_memory_text(value: Any) -> tuple[str, int]:
    """Validate before and after redaction, without retaining the raw input."""
    if not isinstance(value, str) or len(value) > MAX_MEMORY_CHARS:
        raise BotMemoryError(f"memory text must be at most {MAX_MEMORY_CHARS} characters")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise BotMemoryError("memory text must be valid UTF-8") from None
    if size > MAX_MEMORY_BYTES or any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise BotMemoryError(f"memory text must be at most {MAX_MEMORY_BYTES} UTF-8 bytes without control characters")
    text, count = redact(value)
    if len(text) > MAX_MEMORY_CHARS or len(text.encode("utf-8")) > MAX_MEMORY_BYTES:
        raise BotMemoryError("redacted memory text exceeds the memory bound")
    return text, count


def _audit_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise BotMemoryError(f"{name} must be a non-empty string of at most {maximum} characters")
    text, _ = redact(value)
    if len(text) > maximum:
        raise BotMemoryError(f"redacted {name} exceeds its bound")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise BotMemoryError(f"{name} must be valid UTF-8") from None
    return text


class BotMemoryRevisions:
    def __init__(self, state_dir: str | Path) -> None:
        root = Path(state_dir)
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "bot-memory.sqlite3"
        if self.path.is_symlink():
            raise BotMemoryError("memory store cannot be a symbolic link", code="unavailable", status=503)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA secure_delete=ON")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS revisions ("
            "bot_id TEXT NOT NULL,revision INTEGER NOT NULL,text TEXT NOT NULL,sha256 TEXT NOT NULL,"
            "revision_sha256 TEXT NOT NULL,previous_revision_sha256 TEXT NOT NULL,"
            "updated_at TEXT NOT NULL,actor TEXT NOT NULL,reason TEXT,redacted INTEGER NOT NULL,"
            "restored_from INTEGER,PRIMARY KEY(bot_id,revision))"
        )

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    @staticmethod
    def _public(bot_id: str, row: Any) -> dict[str, Any]:
        return {
            "bot_id": bot_id,
            "scope": f"bot:{bot_id}",
            "text": row["text"] if row else "",
            "revision": row["revision"] if row else 0,
            "sha256": row["sha256"] if row else _EMPTY_HASH,
            "revision_sha256": row["revision_sha256"] if row else _EMPTY_HASH,
            "updated_at": row["updated_at"] if row else None,
            "redacted": row["redacted"] if row else 0,
            "untrusted": True,
        }

    def _latest(self, bot_id: str):
        return self._db.execute("SELECT * FROM revisions WHERE bot_id=? ORDER BY revision DESC LIMIT 1", (bot_id,)).fetchone()

    def read(self, bot_id: str) -> dict[str, Any]:
        bot_id = _bot(bot_id)
        with self._lock:
            return self._public(bot_id, self._latest(bot_id))

    def _append(self, bot_id: str, text: str, count: int, expected: int, actor: str, reason: Any, restored_from: Any) -> dict[str, Any]:
        latest = self._latest(bot_id)
        current = latest["revision"] if latest else 0
        if current != expected:
            raise BotMemoryError("memory revision changed; read the current notes before editing", code="conflict", status=409)
        if current >= MAX_REVISIONS:
            raise BotMemoryError("memory revision journal is full; no older audit entries were removed", code="revision_limit", status=409)
        record = {
            "bot_id": bot_id,
            "revision": current + 1,
            "text": text,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "previous_revision_sha256": latest["revision_sha256"] if latest else _EMPTY_HASH,
            "updated_at": now_iso(),
            "actor": actor,
            "reason": reason,
            "redacted": count,
            "restored_from": restored_from,
        }
        record["revision_sha256"] = hashlib.sha256(canonical_json(record).encode("utf-8")).hexdigest()
        columns = tuple(record)
        self._db.execute(
            f"INSERT INTO revisions ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(record[column] for column in columns),
        )
        return self._public(bot_id, record)

    def edit(self, bot_id: str, text: str, *, expected_revision: int, actor: str = "owner", reason: str | None = None) -> dict[str, Any]:
        bot_id, expected = _bot(bot_id), _revision(expected_revision)
        cleaned, count = clean_memory_text(text)
        actor = _audit_text(actor, "actor", 64)
        reason = _audit_text(reason, "reason", 256) if reason is not None else None
        with self._transaction():
            return self._append(bot_id, cleaned, count, expected, actor, reason, None)

    def revisions(self, bot_id: str, *, limit: int = 50, before: int | None = None) -> dict[str, Any]:
        bot_id = _bot(bot_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise BotMemoryError("limit must be an integer from 1 through 100")
        before = _revision(before, maximum=MAX_REVISIONS + 1) if before is not None else MAX_REVISIONS + 1
        with self._lock:
            rows = self._db.execute(
                "SELECT revision,sha256,revision_sha256,previous_revision_sha256,updated_at,actor,reason,redacted,restored_from "
                "FROM revisions WHERE bot_id=? AND revision<? ORDER BY revision DESC LIMIT ?",
                (bot_id, before, limit + 1),
            ).fetchall()
        return {
            "bot_id": bot_id,
            "scope": f"bot:{bot_id}",
            "revisions": [dict(row) for row in rows[:limit]],
            "next_before": rows[limit - 1]["revision"] if len(rows) > limit else None,
            "untrusted": True,
        }

    def restore(self, bot_id: str, revision: int, *, expected_revision: int, actor: str = "owner") -> dict[str, Any]:
        bot_id, revision, expected = _bot(bot_id), _revision(revision), _revision(expected_revision)
        actor = _audit_text(actor, "actor", 64)
        with self._transaction():
            row = self._db.execute("SELECT text FROM revisions WHERE bot_id=? AND revision=?", (bot_id, revision)).fetchone()
            if revision != 0 and row is None:
                raise BotMemoryError("memory revision not found for this bot", code="not_found", status=404)
            # Reapply current redaction rules when restoring older content.
            text, count = clean_memory_text(row["text"] if row else "")
            return self._append(bot_id, text, count, expected, actor, f"Restore revision {revision}", revision)

    def delete(self, bot_id: str) -> None:
        bot_id = _bot(bot_id)
        with self._transaction():
            self._db.execute("DELETE FROM revisions WHERE bot_id=?", (bot_id,))
        with self._lock:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._db.close()
