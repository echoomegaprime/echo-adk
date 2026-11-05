"""SQLite-backed state for the API service (runs, bots, messages, groups, daily spend).

One file under ``--state-dir``: ``echo-adk.sqlite3``. Records are JSON documents with a few
indexed columns; the store is thread-safe behind one lock (the service is a single process).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from ..core.types import now_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    seq INTEGER NOT NULL,
    bot_id TEXT,
    doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_agent_seq ON runs(agent, seq DESC);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status);
CREATE TABLE IF NOT EXISTS bots (
    bot_id TEXT PRIMARY KEY,
    created_seq INTEGER NOT NULL,
    doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_bot_seq ON messages(bot_id, seq);
CREATE TABLE IF NOT EXISTS groups (
    group_id TEXT PRIMARY KEY,
    created_seq INTEGER NOT NULL,
    doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spend (
    day TEXT NOT NULL,
    scope TEXT NOT NULL,
    usd REAL NOT NULL,
    PRIMARY KEY (day, scope)
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, state_dir: str | Path) -> None:
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "echo-adk.sqlite3"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA secure_delete=ON")  # deleted rows are overwritten, never left in free pages
        with self._lock:
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ----------------------------------------------------------------- helpers
    def _next_seq(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT value FROM counters WHERE name='seq'").fetchone()
            value = (row[0] if row else 0) + 1
            self._db.execute("INSERT OR REPLACE INTO counters(name, value) VALUES ('seq', ?)", (value,))
            return value

    @staticmethod
    def _dump(doc: dict[str, Any]) -> str:
        return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))

    # ----------------------------------------------------------------- runs
    def put_run(self, doc: dict[str, Any]) -> None:
        with self._lock:
            existing = self._db.execute("SELECT seq FROM runs WHERE run_id=?", (doc["run_id"],)).fetchone()
            seq = existing[0] if existing else self._next_seq()
            self._db.execute(
                "INSERT OR REPLACE INTO runs(run_id, agent, status, started_at, seq, bot_id, doc) VALUES (?,?,?,?,?,?,?)",
                (doc["run_id"], doc["agent"], doc["status"], doc["started_at"], seq, doc.get("bot_id"), self._dump(doc)),
            )

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT doc FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def update_run(self, run_id: str, **fields: Any) -> Optional[dict[str, Any]]:
        with self._lock:
            doc = self.get_run(run_id)
            if doc is None:
                return None
            doc.update(fields)
            self.put_run(doc)
            return doc

    def list_runs(self, agent: Optional[str] = None, limit: int = 50, *, bot_id: Optional[str] = None) -> list[dict[str, Any]]:
        sql = "SELECT doc FROM runs"
        clauses, args = [], []
        if agent:
            clauses.append("agent=?")
            args.append(agent)
        if bot_id:
            clauses.append("bot_id=?")
            args.append(bot_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [json.loads(r[0]) for r in rows]

    def count_runs(self, status: Optional[str] = None, statuses: Optional[tuple[str, ...]] = None) -> int:
        with self._lock:
            if statuses:
                marks = ",".join("?" for _ in statuses)
                row = self._db.execute(f"SELECT COUNT(*) FROM runs WHERE status IN ({marks})", statuses).fetchone()
            elif status:
                row = self._db.execute("SELECT COUNT(*) FROM runs WHERE status=?", (status,)).fetchone()
            else:
                row = self._db.execute("SELECT COUNT(*) FROM runs").fetchone()
        return int(row[0])

    def delete_runs_for_bot(self, bot_id: str) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM runs WHERE bot_id=?", (bot_id,))
            return int(cur.rowcount)

    # ----------------------------------------------------------------- bots
    def put_bot(self, doc: dict[str, Any]) -> None:
        with self._lock:
            existing = self._db.execute("SELECT created_seq FROM bots WHERE bot_id=?", (doc["bot_id"],)).fetchone()
            seq = existing[0] if existing else self._next_seq()
            self._db.execute("INSERT OR REPLACE INTO bots(bot_id, created_seq, doc) VALUES (?,?,?)", (doc["bot_id"], seq, self._dump(doc)))

    def get_bot(self, bot_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT doc FROM bots WHERE bot_id=?", (bot_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_bots(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT doc FROM bots ORDER BY created_seq ASC").fetchall()
        return [json.loads(r[0]) for r in rows]

    def delete_bot(self, bot_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM bots WHERE bot_id=?", (bot_id,))
            self._db.execute("DELETE FROM messages WHERE bot_id=?", (bot_id,))
            self._db.execute("DELETE FROM spend WHERE scope=?", (f"bot:{bot_id}",))
            self.compact()
            return cur.rowcount > 0

    def compact(self) -> None:
        """Checkpoint the WAL and shrink it to zero so deleted content does not linger on disk."""
        with self._lock:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # ----------------------------------------------------------------- messages
    def put_message(self, doc: dict[str, Any]) -> None:
        with self._lock:
            existing = self._db.execute("SELECT seq FROM messages WHERE message_id=?", (doc["message_id"],)).fetchone()
            seq = existing[0] if existing else self._next_seq()
            self._db.execute(
                "INSERT OR REPLACE INTO messages(message_id, bot_id, seq, doc) VALUES (?,?,?,?)",
                (doc["message_id"], doc["bot_id"], seq, self._dump(doc)),
            )

    def get_message(self, message_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT doc FROM messages WHERE message_id=?", (message_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_messages(self, bot_id: str, since: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            since_seq = -1
            if since:
                row = self._db.execute("SELECT seq FROM messages WHERE message_id=? AND bot_id=?", (since, bot_id)).fetchone()
                if row is None:
                    return []
                since_seq = row[0]
            rows = self._db.execute(
                "SELECT doc FROM messages WHERE bot_id=? AND seq>? ORDER BY seq ASC LIMIT ?", (bot_id, since_seq, int(limit))
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def count_messages(self, bot_id: str, *, pending_only: bool = False) -> int:
        docs = self.list_messages(bot_id, limit=100000)
        if pending_only:
            return sum(1 for d in docs if d.get("role") == "user" and d.get("state") == "pending")
        return len(docs)

    def pending_messages(self, bot_id: str) -> list[dict[str, Any]]:
        return [d for d in self.list_messages(bot_id, limit=100000) if d.get("role") == "user" and d.get("state") == "pending"]

    # ----------------------------------------------------------------- groups
    def put_group(self, doc: dict[str, Any]) -> None:
        with self._lock:
            existing = self._db.execute("SELECT created_seq FROM groups WHERE group_id=?", (doc["group_id"],)).fetchone()
            seq = existing[0] if existing else self._next_seq()
            self._db.execute(
                "INSERT OR REPLACE INTO groups(group_id, created_seq, doc) VALUES (?,?,?)", (doc["group_id"], seq, self._dump(doc))
            )

    def get_group(self, group_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT doc FROM groups WHERE group_id=?", (group_id,)).fetchone()
        return json.loads(row[0]) if row else None

    # ----------------------------------------------------------------- spend
    def add_spend(self, scope: str, usd: float, day: Optional[str] = None) -> float:
        day = day or now_iso()[:10]
        with self._lock:
            row = self._db.execute("SELECT usd FROM spend WHERE day=? AND scope=?", (day, scope)).fetchone()
            total = (row[0] if row else 0.0) + float(usd)
            self._db.execute("INSERT OR REPLACE INTO spend(day, scope, usd) VALUES (?,?,?)", (day, scope, total))
            return total

    def spend_today(self, scope: str, day: Optional[str] = None) -> float:
        day = day or now_iso()[:10]
        with self._lock:
            row = self._db.execute("SELECT usd FROM spend WHERE day=? AND scope=?", (day, scope)).fetchone()
        return float(row[0]) if row else 0.0
