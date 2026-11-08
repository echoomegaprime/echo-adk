"""Explicit, owner-bound bot summaries through the existing ECF gate.

ECF is a shared canonical ledger, not a separate private store for each bot.
This local outbox fixes attribution and request identity; it never creates a
remote grant, chooses a destination, captures transcripts, or retries on boot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core.deadline import limit_io
from ..core.redact import redact
from ..core.types import Lease, is_id, new_id

CAPTURE = "echo.continuity.capture"
RECALL = "echo.continuity.recall"
SCOPES = [CAPTURE, RECALL]
MAX_RECORDS_PER_BOT = 128
MAX_RECORDS = 2048
MAX_ATTEMPTS = 8
WALL_S = 45
WORD = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
REMOTE_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


class ContinuityError(Exception):
    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code, self.status = code, status


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ContinuityError("invalid", 400, f"{name} must be nonempty text of at most {maximum} characters")
    if any((ord(c) < 32 and c not in "\n\r\t") or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ContinuityError("invalid", 400, f"{name} contains invalid characters")
    if redact(value)[1]:
        raise ContinuityError("invalid", 400, "Secret-like summary content is refused before storage or dispatch")
    return value.strip()


def _data(envelope: Any) -> dict | None:
    if not getattr(envelope, "ok", False):
        return None
    outer = envelope.data
    result = outer.get("result")
    body = result.get("body") if isinstance(result, dict) else None
    if (
        outer.get("status") != "ok"
        or not isinstance(result, dict)
        or result.get("status_code") != 200
        or not isinstance(body, dict)
        or body.get("ok") is not True
        or body.get("service") != "echo-continuity-fabric-mcp"
        or not isinstance(body.get("result"), dict)
    ):
        return None
    return body["result"]


class BotContinuity:
    def __init__(
        self,
        state_dir: str | Path,
        *,
        owner_id: str,
        require_bot: Callable,
        capabilities: Callable,
        broker: Callable,
        projects: dict[str, str] | None = None,
        enabled: bool = False,
    ) -> None:
        self.owner_id = _text(owner_id, "host owner", 200)
        self.require_bot, self.capabilities, self.broker = require_bot, capabilities, broker
        if type(enabled) is not bool or (projects is not None and not isinstance(projects, dict)) or len(projects or {}) > 1000:
            raise ContinuityError("invalid_config", 503, "Invalid host continuity configuration")
        self.projects = dict(projects or {})
        if any(not is_id(bot) or not isinstance(project, str) or not WORD.fullmatch(project) for bot, project in self.projects.items()):
            raise ContinuityError("invalid_config", 503, "Continuity requires exact bot ids and existing project ids")
        self.enabled = enabled
        directory = Path(state_dir) / "continuity"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(directory / "outbox.sqlite3", check_same_thread=False, timeout=10)
        os.chmod(directory / "outbox.sqlite3", 0o600)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS captures (owner TEXT NOT NULL, bot TEXT NOT NULL, request_ref TEXT NOT NULL, "
            "id TEXT NOT NULL UNIQUE, body TEXT NOT NULL, busy_until REAL NOT NULL DEFAULT 0, claim TEXT, "
            "PRIMARY KEY(owner,bot,request_ref))"
        )
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _bot(self, bot_id: str) -> dict:
        if not is_id(bot_id):
            raise ContinuityError("invalid", 400, "Invalid bot id")
        return self.require_bot(bot_id)

    def _admission(self, bot_id: str, project: str | None = None) -> dict:
        bot = self._bot(bot_id)
        if not self.enabled or bot_id not in self.projects:
            raise ContinuityError("unavailable", 503, "Continuity capture is not enabled with a host-owned project binding")
        if project is not None and project != self.projects[bot_id]:
            raise ContinuityError("conflict", 409, "The host project binding changed; retain the original receipt for owner reconciliation")
        lease = bot.get("lease", {})
        if not set(SCOPES).issubset(lease.get("scopes", [])) or not set(SCOPES).issubset(self.capabilities(bot_id)):
            raise ContinuityError("gated", 403, "The bot definition and owner-approved lease must permit continuity capture and recall")
        if type(lease.get("max_uses")) is not int or lease["max_uses"] < 2 or type(lease.get("ttl_s")) is not int or lease["ttl_s"] < 1:
            raise ContinuityError("gated", 403, "The bot lease must allow two bounded capability uses")
        return lease

    def _get(self, bot_id: str, capture_id: str) -> dict:
        if not is_id(capture_id):
            raise ContinuityError("invalid", 400, "Invalid capture id")
        row = self._db.execute("SELECT body FROM captures WHERE owner=? AND bot=? AND id=?", (self.owner_id, bot_id, capture_id)).fetchone()
        if row is None:
            raise ContinuityError("not_found", 404, "Capture not found for this bot")
        return json.loads(row[0])

    @staticmethod
    def _public(row: dict) -> dict:
        return {
            "bot_id": row["bot_id"],
            "capture_id": row["capture_id"],
            "request_ref": row["request_ref"],
            "title": row["payload"]["title"],
            "project_id": row["payload"]["projectId"],
            "request_id": row["payload"]["requestId"],
            "content_sha256": row["content_sha256"],
            "state": row["state"],
            "reason": row.get("reason"),
            "saved": row.get("receipt") is not None,
            "verified": row["state"] == "verified",
            "receipt": row.get("receipt"),
            "attempts": row["attempts"],
            "routing_ids": row.get("routing_ids", []),
            "untrusted": True,
            "shared_remote_ledger": True,
        }

    def status(self, bot_id: str) -> dict:
        self._bot(bot_id)
        with self._lock:
            rows = self._db.execute(
                "SELECT body FROM captures WHERE owner=? AND bot=? ORDER BY rowid DESC LIMIT ?",
                (self.owner_id, bot_id, MAX_RECORDS_PER_BOT),
            ).fetchall()
        try:
            self._admission(bot_id)
            admitted = True
        except ContinuityError:
            admitted = False
        return {
            "bot_id": bot_id,
            "configured": self.enabled and bot_id in self.projects,
            "lease_permits_capture": admitted,
            "shared_remote_ledger": True,
            "remote_authorization_verified": False,
            "captures": [self._public(json.loads(row[0])) for row in rows],
            "untrusted": True,
        }

    def has_resources(self, bot_id: str) -> bool:
        with self._lock:
            return (
                self._db.execute("SELECT 1 FROM captures WHERE owner=? AND bot=? LIMIT 1", (self.owner_id, bot_id)).fetchone() is not None
            )

    def ensure_deletable(self, bot_id: str) -> None:
        with self._lock:
            rows = self._db.execute("SELECT body,busy_until FROM captures WHERE owner=? AND bot=?", (self.owner_id, bot_id)).fetchall()
        if any(json.loads(row[0])["state"] not in ("verified", "blocked") or row[1] > time.time() for row in rows):
            raise ContinuityError(
                "cleanup_pending", 409, "Continuity reconciliation is pending; retain this bot and its durable request identities"
            )

    def delete_bot(self, bot_id: str) -> None:
        self.ensure_deletable(bot_id)
        with self._lock, self._db:
            self._db.execute("DELETE FROM captures WHERE owner=? AND bot=?", (self.owner_id, bot_id))

    def capture(self, bot_id: str, body: Any) -> dict:
        self._admission(bot_id)
        if not isinstance(body, dict) or set(body) != {"request_ref", "title", "detail"}:
            raise ContinuityError("invalid", 400, "Capture accepts only request_ref, title and an explicit summary detail")
        key = body["request_ref"]
        if not isinstance(key, str) or not WORD.fullmatch(key):
            raise ContinuityError("invalid", 400, "Invalid capture request key")
        key = _text(key, "request_ref", 100)
        title, detail = _text(body["title"], "title", 200), _text(body["detail"], "detail", 4000)
        payload = {
            "kind": "note",
            "title": title,
            "detail": f"Source: echoBots bot {bot_id}\n\n{detail}",
            "projectId": self.projects[bot_id],
            "requestId": "echobots:" + bot_id + ":" + _digest([self.owner_id, bot_id, key]),
        }
        text = (payload["title"] + "\n" + payload["detail"]).strip()
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            old = self._db.execute(
                "SELECT body,busy_until FROM captures WHERE owner=? AND bot=? AND request_ref=?", (self.owner_id, bot_id, key)
            ).fetchone()
            if old:
                row = json.loads(old[0])
                if row["payload"] != payload:
                    raise ContinuityError("conflict", 409, "This request key belongs to a different summary or project binding")
                if old[1] > time.time():
                    raise ContinuityError("resource_busy", 409, "A capture operation is active; inspect its receipt before retrying")
                return self._public(row)  # only explicit reconcile may retry uncertain work
            total = self._db.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
            count = self._db.execute("SELECT COUNT(*) FROM captures WHERE owner=? AND bot=?", (self.owner_id, bot_id)).fetchone()[0]
            if total >= MAX_RECORDS or count >= MAX_RECORDS_PER_BOT:
                raise ContinuityError("capacity", 429, "Continuity outbox capacity reached; preserve existing request identities")
            row = {
                "capture_id": new_id(),
                "bot_id": bot_id,
                "request_ref": key,
                "payload": payload,
                "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "state": "prepared",
                "attempts": 0,
            }
            self._db.execute(
                "INSERT INTO captures(owner,bot,request_ref,id,body) VALUES(?,?,?,?,?)",
                (self.owner_id, bot_id, key, row["capture_id"], json.dumps(row)),
            )
        return self.reconcile(bot_id, row["capture_id"])

    def _save(self, row: dict, claim: str, *, release: bool = False) -> None:
        with self._lock, self._db:
            updated = self._db.execute(
                "UPDATE captures SET body=?,busy_until=CASE WHEN ? THEN 0 ELSE busy_until END,"
                "claim=CASE WHEN ? THEN NULL ELSE claim END WHERE owner=? AND bot=? AND id=? AND claim=?",
                (json.dumps(row), release, release, self.owner_id, row["bot_id"], row["capture_id"], claim),
            )
            if updated.rowcount != 1:
                raise ContinuityError("conflict", 409, "This capture attempt no longer owns the pending receipt")

    def _invoke(self, broker: Any, row: dict, lease: Lease, cap: str, params: dict, step: int, tier: int) -> Any:
        self._admission(row["bot_id"], row["payload"]["projectId"])
        envelope = broker.invoke(
            cap,
            params,
            lease,
            run_id=row["capture_id"],
            step=step,
            tier=tier,
            context={"bot_id": row["bot_id"], "source": "echo-adk-continuity"},
        )
        routing = envelope.data.get("run_id")
        if isinstance(routing, str) and REMOTE_ID.fullmatch(routing):
            row["routing_ids"] = (row.get("routing_ids", []) + [routing])[-16:]
        return envelope

    def reconcile(self, bot_id: str, capture_id: str) -> dict:
        self._bot(bot_id)
        with self._lock:
            row = self._get(bot_id, capture_id)
        template = self._admission(bot_id, row["payload"]["projectId"])
        if row["state"] == "verified":
            return self._public(row)
        claim = new_id()
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._get(bot_id, capture_id)
            active = self._db.execute(
                "SELECT 1 FROM captures WHERE owner=? AND bot=? AND busy_until>?", (self.owner_id, bot_id, time.time())
            ).fetchone()
            if active:
                raise ContinuityError("resource_busy", 409, "A capture operation is active; inspect its receipt before retrying")
            if row["attempts"] >= MAX_ATTEMPTS or row["state"] == "conflict":
                raise ContinuityError("conflict", 409, "Capture requires owner reconciliation; automatic new identities are refused")
            row["attempts"] += 1
            row["state"] = "saved" if row.get("receipt") else "uncertain"
            row["reason"] = "owner_call_pending"
            self._db.execute(
                "UPDATE captures SET body=?,claim=?,busy_until=? WHERE owner=? AND bot=? AND id=?",
                (json.dumps(row), claim, time.time() + WALL_S + 15, self.owner_id, bot_id, capture_id),
            )
        try:
            deadline = time.monotonic() + min(WALL_S, template["ttl_s"])
            with limit_io(min(WALL_S, template["ttl_s"])):
                broker = self.broker()
                for capability, tier, scope, verb in (
                    (CAPTURE, 1, "continuity.write", "capture"),
                    (RECALL, 0, "continuity.read", "recall"),
                ):
                    actual = broker.resolve_capability(capability)
                    if actual.id != capability or actual.tier != tier or actual.scope != scope or actual.verb != verb:
                        raise ContinuityError("gated", 403, "Live continuity metadata no longer matches the reviewed contract")
                template = self._admission(bot_id, row["payload"]["projectId"])
                lease = Lease.mint(capture_id, SCOPES, ttl_s=min(WALL_S, template["ttl_s"]), max_uses=2)
                if time.monotonic() >= deadline:
                    raise TimeoutError("Catalog deadline")
                if not row.get("receipt"):
                    previously_unknown = row.get("write_outcome_unknown", False)
                    row["write_outcome_unknown"] = True
                    self._save(row, claim)  # crash before/after dispatch keeps the same durable identity
                    envelope = self._invoke(broker, row, lease, CAPTURE, row["payload"], 0, 1)
                    data = _data(envelope)
                    if data is None or not self._receipt(data, row):
                        body = envelope.data.get("result", {}).get("body", {})
                        error = body.get("error") if isinstance(body, dict) else None
                        denied = error in ("insufficient_scope", "scope_forbidden", "operator_required") or envelope.verdict == "gated"
                        row["state"] = (
                            "conflict"
                            if error == "request_id_conflict"
                            else "blocked"
                            if denied and not previously_unknown
                            else "uncertain"
                        )
                        if row["state"] == "blocked":
                            row["write_outcome_unknown"] = False
                        row["reason"] = "owner_receipt_unconfirmed"
                        return self._public(row)
                    row["receipt"] = {
                        key: data[key]
                        for key in ("id", "eventId", "kind", "requestId", "saved", "verified", "receiptState", "duplicate", "contentSha256")
                    }
                    row["write_outcome_unknown"] = False
                    row["state"], row["reason"] = "saved", "readback_pending"
                    self._save(row, claim)
                if time.monotonic() >= deadline:
                    raise TimeoutError("Capture deadline")
                recall = self._invoke(broker, row, lease, RECALL, {"query": row["receipt"]["eventId"]}, 1, 0)
                if time.monotonic() < deadline and self._recalled(_data(recall), row):
                    row["state"], row["reason"] = "verified", None
                else:
                    row["reason"] = "exact_readback_unconfirmed"
        except Exception:
            # A timeout may follow a committed remote write. Keep the original
            # payload/requestId; no exception/body/credential reaches the UI.
            row["reason"] = "owner_transport_unconfirmed"
            if not row.get("receipt") and not row.get("write_outcome_unknown"):
                row["state"] = "blocked"
        finally:
            self._save(row, claim, release=True)
        return self._public(row)

    @staticmethod
    def _receipt(data: dict, row: dict) -> bool:
        return (
            all(isinstance(data.get(k), str) and REMOTE_ID.fullmatch(data[k]) for k in ("id", "eventId"))
            and data.get("kind") == "note"
            and data.get("requestId") == row["payload"]["requestId"]
            and data.get("saved") is True
            and data.get("verified") is True
            and data.get("receiptState") == "capture_verified"
            and type(data.get("duplicate")) is bool
            and data.get("contentSha256") == row["content_sha256"]
        )

    @staticmethod
    def _recalled(data: dict | None, row: dict) -> bool:
        if not isinstance(data, dict) or not isinstance(data.get("events"), list) or len(data["events"]) > 20:
            return False
        text = (row["payload"]["title"] + "\n" + row["payload"]["detail"]).strip()
        # ECF clips displayed text using JavaScript UTF-16 code units. Its
        # independently returned content hash still covers the complete note.
        encoded = text.encode("utf-16-le")

        def same_content(event: dict) -> bool:
            value = event.get("content_text")
            if value == text:
                return True
            if not isinstance(value, str) or event.get("excerpt_truncated") is not True:
                return False
            shown = value.encode("utf-16-le", errors="surrogatepass")
            units = len(shown) // 2
            # The live owner fits all recall sections to a shared budget and
            # may shorten any excerpt from 1200 down to 160 UTF-16 units.
            return 160 <= units <= 1200 and len(encoded) > len(shown) and shown == encoded[: (units - 1) * 2] + "…".encode("utf-16-le")

        return any(
            isinstance(event, dict)
            and event.get("id") == row["receipt"]["eventId"]
            and event.get("content_sha256") == row["content_sha256"]
            and event.get("project_id") == row["payload"]["projectId"]
            and same_content(event)
            for event in data["events"]
        )


def from_env(state_dir: str | Path, *, owner_id: str, require_bot: Callable, capabilities: Callable, broker: Callable) -> BotContinuity:
    config: dict = {}
    configured = os.environ.get("ECHO_ADK_CONTINUITY_CONFIG")
    if configured:
        try:
            path = Path(configured)
            if not path.is_absolute() or path.stat().st_size > 131072:
                raise ValueError
            config = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(config, dict) or set(config) != {"enabled", "projects"}:
                raise ValueError
        except (OSError, ValueError, TypeError):
            raise ContinuityError("invalid_config", 503, "The host continuity binding file is unavailable or invalid") from None
    return BotContinuity(state_dir, owner_id=owner_id, require_bot=require_bot, capabilities=capabilities, broker=broker, **config)
