"""Durable, explicitly enabled connector polling into a bot's bounded inbox.

This is polling, not an internet-webhook authenticator. Only a previously
consented, read-only connector operation is eligible. The host owns one scheduler
and supplies the existing bot event admission callback; this module starts none.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..bots.triggers import EVENT_RECEIPT_TTL_S
from ..core.deadline import limit_io
from ..core.redact import redact_value
from .catalog import identifier
from .errors import ConnectionError
from .manager import ConnectionManager, _iso

PENDING_TTL_S = EVENT_RECEIPT_TTL_S - 60
MAX_SUBSCRIPTIONS_PER_BOT = 32
MAX_POLL_BATCH = 16
POLL_WALL_S = 25
CLAIM_TTL_S = 60
_MISSING = object()


def _bounded_json(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode()) > 65536:
            raise ValueError
        return encoded
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ConnectionError("invalid", "Subscription inputs must be valid JSON of at most 64 KiB.") from None


def validate_predicate(predicate: Any) -> dict | None:
    if predicate is None:
        return None
    if not isinstance(predicate, dict) or set(predicate) - {"path", "operator", "value"} or not {"path", "operator"} <= set(predicate):
        raise ConnectionError("invalid", "A precheck requires only path, operator, and an optional scalar value.")
    path, operator = predicate["path"], predicate["operator"]
    if not isinstance(path, list) or len(path) > 8:
        raise ConnectionError("invalid", "Precheck path must be at most eight literal object keys or array indices.")
    for part in path:
        if isinstance(part, str) and 1 <= len(part) <= 128 and not any(ord(c) < 32 for c in part):
            continue
        if type(part) is int and 0 <= part <= 1000:
            continue
        raise ConnectionError("invalid", "Invalid precheck path component.")
    if operator not in ("exists", "equals", "contains", "gt", "lt"):
        raise ConnectionError("invalid", "Unsupported deterministic precheck operator.")
    if operator == "exists":
        if "value" in predicate:
            raise ConnectionError("invalid", "The exists precheck does not accept a value.")
    else:
        if "value" not in predicate:
            raise ConnectionError("invalid", "This precheck requires a scalar value.")
        value = predicate["value"]
        if value is not None and not isinstance(value, (str, bool, int, float)):
            raise ConnectionError("invalid", "Precheck comparisons accept only JSON scalar values.")
        if isinstance(value, str) and len(value) > 1024:
            raise ConnectionError("invalid", "Precheck string exceeds its bound.")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not -1e12 <= value <= 1e12 or not math.isfinite(value):
                raise ConnectionError("invalid", "Precheck number exceeds its finite bound.")
        if operator in ("gt", "lt") and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ConnectionError("invalid", "Numeric prechecks require a finite number.")
    return json.loads(_bounded_json(predicate))


def _same_scalar(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


def matches(data: Any, predicate: dict | None) -> bool:
    if predicate is None:
        return True
    selected = data
    for part in predicate["path"]:
        if isinstance(part, str) and isinstance(selected, dict):
            selected = selected.get(part, _MISSING)
        elif type(part) is int and isinstance(selected, list) and part < len(selected):
            selected = selected[part]
        else:
            selected = _MISSING
        if selected is _MISSING:
            return False
    operator, value = predicate["operator"], predicate.get("value")
    if operator == "exists":
        return True
    if operator == "equals":
        return not isinstance(selected, (dict, list)) and _same_scalar(selected, value)
    if operator == "contains":
        return (isinstance(selected, str) and isinstance(value, str) and value in selected) or (
            isinstance(selected, list) and any(not isinstance(item, (dict, list)) and _same_scalar(item, value) for item in selected)
        )
    if isinstance(selected, bool) or not isinstance(selected, (int, float)) or not -1e12 <= selected <= 1e12 or not math.isfinite(selected):
        return False
    return selected > value if operator == "gt" else selected < value


class SubscriptionManager:
    """Owner API with durable cursors and an idempotent bot-inbox outbox.

    ``deliver_event(bot_id, envelope, still_current)`` must take the bot-manager
    lock, check the supplied guard, then call its normal ``deliver_event``. This
    keeps stop/delete atomic with admission without holding our DB lock over I/O.
    """

    def __init__(
        self,
        state_dir: str | Path,
        connections: ConnectionManager,
        require_bot: Callable[[str], Any],
        deliver_event: Callable[[str, dict, Callable[[], bool]], dict],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not callable(require_bot) or not callable(deliver_event):
            raise ConnectionError("invalid_configuration", "Subscription bot and event admission callbacks are required.")
        self.connections, self.require_bot, self.deliver_event, self.clock = connections, require_bot, deliver_event, clock
        self.owner_id = connections.owner_id
        self._lock, self._poll_lock = threading.RLock(), threading.Lock()
        path = Path(state_dir) / "connections" / "subscriptions.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink() or path.is_symlink() or (path.exists() and not path.is_file()):
            raise ConnectionError("invalid_configuration", "Subscription state must use a private regular file.")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        if os.name == "posix":
            path.parent.chmod(0o700)
            path.chmod(0o600)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA secure_delete=ON")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS subscriptions(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, bot TEXT NOT NULL,
                doc TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, next_due REAL NOT NULL,
                claim TEXT, claim_until REAL, pending TEXT
            );
            CREATE INDEX IF NOT EXISTS subscriptions_due ON subscriptions(owner,enabled,next_due);
            CREATE INDEX IF NOT EXISTS subscriptions_bot ON subscriptions(owner,bot);
        """)

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _bot(self, bot_id: str) -> None:
        identifier(bot_id, "bot")
        self.require_bot(bot_id)

    def _row(self, bot: str, subscription_id: str) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT * FROM subscriptions WHERE owner=? AND bot=? AND id=?", (self.owner_id, bot, subscription_id)
        ).fetchone()
        if row is None:
            raise ConnectionError("subscription_not_found", "Subscription not found for this bot.", 404)
        return row

    @staticmethod
    def _public(row: sqlite3.Row) -> dict:
        doc = json.loads(row["doc"])
        doc.pop("generation", None)
        doc["enabled"] = bool(row["enabled"])
        doc["busy"] = row["claim"] is not None
        doc["next_poll_at"] = _iso(row["next_due"]) if row["enabled"] else None
        pending = json.loads(row["pending"]) if row["pending"] else None
        doc["pending_delivery"] = pending is not None
        if pending:
            doc["pending_expires_at"] = _iso(pending["expires_at"])
        return doc

    def list(self, bot_id: str) -> list[dict]:
        self._bot(bot_id)
        with self._lock:
            rows = self._db.execute("SELECT * FROM subscriptions WHERE owner=? AND bot=? ORDER BY rowid", (self.owner_id, bot_id))
            return [self._public(row) for row in rows]

    def has_resources(self, bot_id: str) -> bool:
        return bool(self.list(bot_id))

    def get(self, bot_id: str, subscription_id: str) -> dict:
        self._bot(bot_id)
        identifier(subscription_id, "subscription")
        with self._lock:
            return self._public(self._row(bot_id, subscription_id))

    def _operation(self, bot_id: str, connection_id: str, operation_id: str, params: dict) -> dict:
        connection = self.connections.get(bot_id, connection_id)
        if connection["status"] != "connected" or operation_id not in connection["operation_ids"]:
            raise ConnectionError("connection_not_ready", "Polling needs a connected, explicitly bound operation.", 409)
        provider = self.connections.providers[connection["provider_id"]]
        _, binding = self.connections.store.get(self.owner_id, bot_id, connection_id)
        self.connections._binding(connection, binding)
        operation = provider.operation(identifier(operation_id, "operation"))
        if operation.read_only is not True or operation.method != "GET":
            raise ConnectionError("operation_not_allowed", "Subscriptions may poll only host-declared read-only GET operations.", 403)
        operation.request(params)  # closed schema validation, without any I/O
        return connection

    def create(
        self, bot_id: str, connection_id: str, operation_id: str, params: dict, interval_s: int, predicate: dict | None = None
    ) -> dict:
        self._bot(bot_id)
        identifier(connection_id, "connection")
        if not isinstance(interval_s, int) or isinstance(interval_s, bool) or not 60 <= interval_s <= 86400:
            raise ConnectionError("invalid", "Polling interval must be between 60 and 86400 seconds.")
        params = json.loads(_bounded_json(params))
        if redact_value(params)[1]:
            raise ConnectionError("invalid", "Subscription parameters must not contain credentials.")
        predicate = validate_predicate(predicate)
        if redact_value(predicate)[1]:
            raise ConnectionError("invalid", "Prechecks must not contain credentials.")
        connection = self._operation(bot_id, connection_id, operation_id, params)
        now, subscription_id = self.clock(), secrets.token_hex(16)
        doc = {
            "id": subscription_id,
            "bot_id": bot_id,
            "connection_id": connection_id,
            "provider_id": connection["provider_id"],
            "operation_id": operation_id,
            "params": params,
            "predicate": predicate,
            "interval_s": interval_s,
            "created_at": _iso(now),
            "updated_at": _iso(now),
            "generation": 0,
            "cursor": {"observation_sha256": None, "sequence": 0},
            "consecutive_failures": 0,
        }
        with self._transaction():
            count = self._db.execute("SELECT count(*) FROM subscriptions WHERE owner=? AND bot=?", (self.owner_id, bot_id)).fetchone()[0]
            total = self._db.execute("SELECT count(*) FROM subscriptions WHERE owner=?", (self.owner_id,)).fetchone()[0]
            if count >= MAX_SUBSCRIPTIONS_PER_BOT or total >= 4096:
                raise ConnectionError("subscription_limit", "Delete an existing subscription before creating another.", 409)
            self._db.execute(
                "INSERT INTO subscriptions(id,owner,bot,doc,next_due) VALUES(?,?,?,?,?)",
                (subscription_id, self.owner_id, bot_id, json.dumps(doc), now),
            )
        return self.get(bot_id, subscription_id)

    def set_enabled(self, bot_id: str, subscription_id: str, enabled: bool) -> dict:
        self._bot(bot_id)
        identifier(subscription_id, "subscription")
        if not isinstance(enabled, bool):
            raise ConnectionError("invalid", "Subscription enabled must be a boolean.")
        with self._lock:
            row = self._row(bot_id, subscription_id)
            doc = json.loads(row["doc"])
        if enabled:
            self._operation(bot_id, doc["connection_id"], doc["operation_id"], doc["params"])
        with self._transaction():
            row = self._row(bot_id, subscription_id)
            doc = json.loads(row["doc"])
            pending = json.loads(row["pending"]) if row["pending"] else None
            if enabled and pending and self.clock() >= pending["expires_at"]:
                raise ConnectionError(
                    "delivery_held",
                    "This unacknowledged event is beyond its deduplication horizon; inspect it before deleting the subscription.",
                    409,
                )
            if bool(row["enabled"]) != enabled:
                doc.update(generation=doc["generation"] + 1, updated_at=_iso(self.clock()))
                self._db.execute(
                    "UPDATE subscriptions SET enabled=?,doc=?,next_due=? WHERE id=?",
                    (int(enabled), json.dumps(doc), self.clock(), subscription_id),
                )
        return self.get(bot_id, subscription_id)

    def delete(self, bot_id: str, subscription_id: str) -> dict:
        self._bot(bot_id)
        identifier(subscription_id, "subscription")
        with self._transaction():
            self._row(bot_id, subscription_id)
            self._db.execute("DELETE FROM subscriptions WHERE owner=? AND bot=? AND id=?", (self.owner_id, bot_id, subscription_id))
        return {"id": subscription_id, "bot_id": bot_id, "deleted": True}

    def disable_bot(self, bot_id: str) -> None:
        self._bot(bot_id)
        with self._transaction():
            rows = self._db.execute("SELECT id,doc FROM subscriptions WHERE owner=? AND bot=?", (self.owner_id, bot_id)).fetchall()
            for row in rows:
                doc = json.loads(row["doc"])
                doc.update(generation=doc["generation"] + 1, updated_at=_iso(self.clock()))
                self._db.execute("UPDATE subscriptions SET enabled=0,doc=? WHERE id=?", (json.dumps(doc), row["id"]))

    def delete_bot(self, bot_id: str) -> None:
        self._bot(bot_id)
        with self._transaction():
            self._db.execute("DELETE FROM subscriptions WHERE owner=? AND bot=?", (self.owner_id, bot_id))

    def _claim_due(self, bot_id: str | None) -> tuple[dict, str, dict | None] | None:
        now = self.clock()
        with self._transaction():
            # Reclaim only a read-only poll or the SAME durable outbox event.
            # Rotating OAuth refresh claims belong to ConnectionManager and are
            # never reclaimed by this scheduler.
            self._db.execute("UPDATE subscriptions SET claim=NULL,claim_until=NULL WHERE owner=? AND claim_until<=?", (self.owner_id, now))
            sql = "SELECT * FROM subscriptions WHERE owner=? AND enabled=1 AND next_due<=? AND claim IS NULL"
            values = [self.owner_id, now]
            if bot_id is not None:
                sql += " AND bot=?"
                values.append(bot_id)
            row = self._db.execute(sql + " ORDER BY next_due,rowid LIMIT 1", values).fetchone()
            if row is None:
                return None
            doc, claim = json.loads(row["doc"]), secrets.token_hex(16)
            self._db.execute(
                "UPDATE subscriptions SET claim=?,claim_until=?,next_due=? WHERE id=?",
                (claim, now + CLAIM_TTL_S, now + doc["interval_s"], doc["id"]),
            )
            return doc, claim, json.loads(row["pending"]) if row["pending"] else None

    def _current(self, doc: dict, claim: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT doc,enabled,claim FROM subscriptions WHERE owner=? AND id=?", (self.owner_id, doc["id"])
            ).fetchone()
            return bool(row and row["enabled"] and row["claim"] == claim and json.loads(row["doc"])["generation"] == doc["generation"])

    def _save_current(self, doc: dict, claim: str, *, pending: dict | None, enabled: bool = True) -> bool:
        with self._transaction():
            if not self._current(doc, claim):
                return False
            if pending:
                # Receipt recovery does not repeat provider I/O. Even a daily
                # watch must recover within the event's deduplication horizon.
                delay = min(3600, 60 * 2 ** min(doc["consecutive_failures"], 5))
            else:
                delay = max(doc["interval_s"], min(3600, doc["interval_s"] * 2 ** min(doc["consecutive_failures"], 5)))
            self._db.execute(
                "UPDATE subscriptions SET doc=?,pending=?,enabled=?,next_due=? WHERE id=? AND claim=?",
                (
                    json.dumps(doc),
                    json.dumps(pending) if pending else None,
                    int(enabled),
                    self.clock() + delay,
                    doc["id"],
                    claim,
                ),
            )
            return True

    @staticmethod
    def _event(doc: dict, payload: Any, digest: str, now: float) -> dict:
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        text = json.dumps(
            {
                "subscription_id": doc["id"],
                "connection_id": doc["connection_id"],
                "provider_id": doc["provider_id"],
                "operation_id": doc["operation_id"],
                "observation_sha256": digest,
                "untrusted": True,
                "data_preview": serialized[:3000],
                "truncated": len(serialized) > 3000,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        sequence = doc["cursor"]["sequence"] + 1
        return {
            "event": {"event_id": f"connector-{doc['id']}-{sequence}", "source": "connector.poll", "text": redact_value(text)[0]},
            "digest": digest,
            "sequence": sequence,
            "created_at": now,
            "expires_at": now + PENDING_TTL_S,
        }

    def _deliver(self, doc: dict, claim: str, pending: dict, cancel: threading.Event | None = None) -> dict:
        if cancel is not None and cancel.is_set():
            return {"id": doc["id"], "status": "cancelled"}
        if self.clock() >= pending["expires_at"]:
            doc["last_error"] = {
                "code": "delivery_held",
                "message": "Unacknowledged delivery exceeded the safe retry horizon; owner review is required.",
            }
            self._save_current(doc, claim, pending=pending, enabled=False)
            return {"id": doc["id"], "status": "held", "event_id": pending["event"]["event_id"]}

        def guard():
            return (cancel is None or not cancel.is_set()) and self._current(doc, claim)

        if not guard():
            return {"id": doc["id"], "status": "disabled"}
        try:
            receipt = self.deliver_event(doc["bot_id"], pending["event"], guard)
            valid = (
                isinstance(receipt, dict)
                and receipt.get("ok") is not False
                and receipt.get("event_id") == pending["event"]["event_id"]
                and all(
                    isinstance(receipt.get(k), str) and len(receipt[k]) == 32 and all(c in "0123456789abcdef" for c in receipt[k])
                    for k in ("message_id", "run_id")
                )
            )
            if not valid:
                raise ConnectionError(
                    "delivery_unknown", "Bot event admission did not return a matching durable receipt.", 502, outcome_unknown=True
                )
        except Exception as exc:
            code = getattr(exc, "code", None)
            known = code in ("queue_full", "budget_exceeded") and getattr(exc, "status", None) == 429
            doc["last_error"] = {
                "code": code if known else "delivery_unknown",
                "message": "Bot inbox admission is temporarily limited."
                if known
                else "Event acknowledgment is unknown; only its original ID may be retried.",
            }
            doc["consecutive_failures"] = min(100, doc["consecutive_failures"] + 1)
            self._save_current(doc, claim, pending=pending)
            return {"id": doc["id"], "status": "pending", "event_id": pending["event"]["event_id"], "error": doc["last_error"]["code"]}
        doc["cursor"] = {"observation_sha256": pending["digest"], "sequence": pending["sequence"]}
        doc.update(
            consecutive_failures=0,
            updated_at=_iso(self.clock()),
            last_event={
                "event_id": receipt["event_id"],
                "message_id": receipt["message_id"],
                "run_id": receipt["run_id"],
                "duplicate": receipt.get("duplicate") is True,
            },
        )
        doc.pop("last_error", None)
        committed = self._save_current(doc, claim, pending=None)
        return {"id": doc["id"], "status": "delivered" if committed else "disabled", **doc["last_event"]}

    def _poll(self, doc: dict, claim: str, pending: dict | None, cancel: threading.Event | None = None) -> dict:
        try:
            self._bot(doc["bot_id"])
            if pending:
                return self._deliver(doc, claim, pending, cancel)
            self._operation(doc["bot_id"], doc["connection_id"], doc["operation_id"], doc["params"])
            if cancel is not None and cancel.is_set():
                return {"id": doc["id"], "status": "cancelled"}
            result = self.connections.invoke(doc["bot_id"], doc["connection_id"], doc["operation_id"], doc["params"])
            if not isinstance(result, dict) or result.get("ok") is not True or result.get("untrusted") is not True:
                raise ConnectionError("provider_rejected", "The connector did not return a verified operation receipt.", 502)
            payload = result["data"]
            serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(serialized.encode()).hexdigest()
            doc.update(last_poll_at=_iso(self.clock()), updated_at=_iso(self.clock()), consecutive_failures=0)
            doc.pop("last_error", None)
            matched = matches(payload, doc["predicate"])
            doc["last_precheck_matched"] = matched
            if not matched or digest == doc["cursor"]["observation_sha256"]:
                doc["cursor"]["observation_sha256"] = digest
                saved = self._save_current(doc, claim, pending=None)
                return {"id": doc["id"], "status": "unchanged" if saved and matched else "filtered" if saved else "disabled"}
            pending = self._event(doc, payload, digest, self.clock())
            if not self._save_current(doc, claim, pending=pending):
                return {"id": doc["id"], "status": "disabled"}
            return self._deliver(doc, claim, pending, cancel)
        except Exception as exc:
            # Never copy exception/provider contents into durable metadata.
            allowed = {
                "connection_not_ready",
                "connection_not_found",
                "connection_configuration_changed",
                "connection_busy",
                "provider_unavailable",
                "provider_rejected",
                "operation_not_allowed",
                "credential_store_unavailable",
            }
            code = getattr(exc, "code", "poll_failed")
            doc["last_error"] = {
                "code": code if code in allowed else "poll_failed",
                "message": "Connector polling failed; no new event was acknowledged.",
            }
            doc["consecutive_failures"] = min(100, doc["consecutive_failures"] + 1)
            self._save_current(doc, claim, pending=pending)
            return {"id": doc["id"], "status": "error", "error": doc["last_error"]["code"]}

    def poll_due(self, bot_id: str | None = None, limit: int = MAX_POLL_BATCH, cancel: threading.Event | None = None) -> list[dict]:
        if bot_id is not None:
            self._bot(bot_id)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_POLL_BATCH:
            raise ConnectionError("invalid", "A poll pass may inspect at most sixteen due subscriptions.")
        if not self._poll_lock.acquire(blocking=False):
            return []
        results, deadline = [], time.monotonic() + POLL_WALL_S
        try:
            with limit_io(POLL_WALL_S):
                while len(results) < limit and time.monotonic() < deadline:
                    if cancel is not None and cancel.is_set():
                        break
                    claimed = self._claim_due(bot_id)
                    if claimed is None:
                        break
                    doc, claim, pending = claimed
                    try:
                        results.append(self._poll(doc, claim, pending, cancel))
                    finally:
                        with self._transaction():
                            self._db.execute(
                                "UPDATE subscriptions SET claim=NULL,claim_until=NULL WHERE owner=? AND id=? AND claim=?",
                                (self.owner_id, doc["id"], claim),
                            )
            return results
        finally:
            self._poll_lock.release()

    def close(self) -> None:
        # Host stops/joins its single scheduler before closing the workspace.
        with self._lock:
            self._db.close()
