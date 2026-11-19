"""Durable intake and result storage for the sealed synthetic FieldDesk staging trial."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from echo_adk.api.service import ApiError

FIXTURE_SHA = "f5e587e5288dfa79148b1ed453033a098b111c5a756bb0ae2de05f0e79544f32"
MODEL = "echo-prometheus:27b-256k"
MODEL_DIGEST = "ba8a5b4e4a1579b249fb38f82a54aa74d991981dbd35f5561b60e31ba09278f0"
AGENT = "lyra-test-goal-loop"
BASE = "/pilots/fielddesk-followups/goals"


class Jobs:
    def __init__(self, state_dir, fixture):
        data = Path(fixture).read_bytes()
        if hashlib.sha256(data).hexdigest() != FIXTURE_SHA:
            raise ValueError("unrecognized synthetic fixture")
        source = json.loads(data)
        self.items = {row["id"]: row for row in source["appointments"]}
        if set(self.items) != {f"FD-{i:02d}" for i in range(1, 21)} or len(source["appointments"]) != 20:
            raise ValueError("expected twenty unique synthetic appointments")
        self.goal = source["goal"]
        self.path = Path(state_dir) / "fielddesk-goals.sqlite3"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(self.db()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS goals(id TEXT PRIMARY KEY, client_key TEXT NOT NULL UNIQUE, fixture TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS items(goal TEXT NOT NULL REFERENCES goals(id), appointment TEXT NOT NULL,
                    run_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending', result TEXT, error TEXT,
                    PRIMARY KEY(goal, appointment));
            """)

    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def create(self, body):
        if not isinstance(body, dict) or set(body) != {"client_request_id"}:
            raise ApiError(400, "invalid", "Supply only client_request_id.")
        key = body["client_request_id"]
        try:
            if not isinstance(key, str) or str(uuid.UUID(key)) != key:
                raise ValueError
        except (ValueError, AttributeError):
            raise ApiError(400, "invalid", "The client request ID must be a canonical UUID.") from None
        goal_id = uuid.uuid5(uuid.NAMESPACE_URL, "echo:fielddesk-synthetic:v1:" + key).hex
        with closing(self.db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT fixture FROM goals WHERE id=?", (goal_id,)).fetchone()
            if old and old["fixture"] != FIXTURE_SHA:
                raise ApiError(409, "conflict", "This request ID belongs to a different fixture.")
            if not old:
                db.execute("INSERT INTO goals VALUES(?,?,?)", (goal_id, key, FIXTURE_SHA))
                for appointment in self.items:
                    run_id = uuid.uuid5(uuid.UUID(goal_id), appointment).hex
                    db.execute("INSERT INTO items(goal,appointment,run_id) VALUES(?,?,?)", (goal_id, appointment, run_id))
        return (200 if old else 202), self.result(goal_id)

    def next_item(self):
        with closing(self.db()) as db:
            return db.execute(
                "SELECT i.* FROM items i JOIN goals g ON g.id=i.goal WHERE g.fixture=? AND i.status='pending' "
                "ORDER BY g.rowid,i.appointment LIMIT 1",
                (FIXTURE_SHA,),
            ).fetchone()

    def item(self, goal, appointment):
        with closing(self.db()) as db:
            item = db.execute(
                "SELECT i.* FROM items i JOIN goals g ON g.id=i.goal WHERE g.fixture=? AND goal=? AND appointment=?",
                (FIXTURE_SHA, goal, appointment),
            ).fetchone()
        if item is None:
            raise ApiError(404, "not_found", "Unknown goal or appointment.")
        return item

    def finish(self, item, result=None, error=None):
        with closing(self.db()) as db, db:
            db.execute(
                "UPDATE items SET status=?,result=?,error=? WHERE goal=? AND appointment=? AND status='pending'",
                ("failed" if error else "done", json.dumps(result) if result else None, error, item["goal"], item["appointment"]),
            )

    def result(self, goal_id):
        with closing(self.db()) as db:
            rows = db.execute(
                "SELECT i.* FROM items i JOIN goals g ON g.id=i.goal WHERE goal=? AND g.fixture=? ORDER BY appointment",
                (goal_id, FIXTURE_SHA),
            ).fetchall()
        if not rows:
            raise ApiError(404, "not_found", "Unknown synthetic goal.")
        done = [json.loads(row["result"]) for row in rows if row["status"] == "done"]
        errors = [{"appointment_id": r["appointment"], "run_id": r["run_id"], "error": r["error"]} for r in rows if r["status"] == "failed"]
        return {
            "kind": "synthetic_fielddesk_goal",
            "goal_id": goal_id,
            "goal": self.goal,
            "fixture_sha256": FIXTURE_SHA,
            "status": ("completed_with_errors" if errors else "completed") if len(done) + len(errors) == len(rows) else "pending",
            "completed": len(done),
            "total": len(rows),
            "decisions": done,
            "errors": errors,
        }
