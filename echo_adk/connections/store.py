"""Separate encrypted OAuth vault, never included in a bot transcript or export.

The encryption key is provided by the host. It is not generated beside the
database, copied into SQLite, or exposed by a public connection record. AES-GCM
authenticates the owner, bot, connection and purpose of each encrypted record.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from .errors import ConnectionError


class Vault:
    def __init__(self, key_provider: Callable[[], bytes]) -> None:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            raise ConnectionError(
                "connections_unavailable", "Install the optional connections dependency to enable encrypted OAuth storage.", 503
            ) from None
        self._cipher = AESGCM
        self._key_provider = key_provider
        self._key()  # Validate without retaining the key in a public configuration.

    def _key(self) -> bytes:
        try:
            key = self._key_provider()
        except Exception:
            raise ConnectionError("credential_store_unavailable", "The host credential-vault key is unavailable.", 503) from None
        if not isinstance(key, bytes) or len(key) != 32:
            raise ConnectionError("credential_store_unavailable", "The host credential-vault key must be 32 random bytes.", 503)
        return key

    def check_key(self) -> None:
        self._key()

    @staticmethod
    def _aad(owner: str, bot: str, connection: str, purpose: str) -> bytes:
        return json.dumps(["echo-adk-connections-v1", owner, bot, connection, purpose], separators=(",", ":")).encode()

    def seal(self, value: dict, *, owner: str, bot: str, connection: str, purpose: str) -> bytes:
        payload = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
        if len(payload) > 32768:
            raise ConnectionError("invalid_provider_response", "Provider credentials exceed the vault's size limit.", 502)
        nonce = secrets.token_bytes(12)
        return nonce + self._cipher(self._key()).encrypt(nonce, payload, self._aad(owner, bot, connection, purpose))

    def open(self, value: bytes | None, *, owner: str, bot: str, connection: str, purpose: str) -> dict:
        if value is None:
            return {}
        try:
            plain = self._cipher(self._key()).decrypt(value[:12], value[12:], self._aad(owner, bot, connection, purpose))
            result = json.loads(plain)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except ConnectionError:
            raise
        except Exception:
            raise ConnectionError(
                "credential_store_unavailable", "The encrypted credential record could not be authenticated.", 503
            ) from None


class ConnectionStore:
    """SQLite metadata and ciphertext with atomic, durable one-use claims.

    A claim is never automatically expired or replayed: after a process crash,
    the owner can disconnect and start a fresh consent. That prevents refresh
    rotation from being retried with an unknown provider-side outcome.
    """

    def __init__(self, path: Path, vault: Vault) -> None:
        self.vault = vault
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink() or path.is_symlink() or (path.exists() and not path.is_file()):
            raise ConnectionError("credential_store_unavailable", "The OAuth vault must use a private regular file.", 503)
        if os.name == "posix":
            path.parent.chmod(0o700)
        # Create with restrictive permissions before SQLite first opens it.
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        if os.name == "posix":
            path.chmod(0o600)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA secure_delete=ON")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS connections(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, bot TEXT NOT NULL,
                public TEXT NOT NULL, binding TEXT NOT NULL, secret BLOB,
                state_hash TEXT UNIQUE, state_expires REAL, state_used INTEGER NOT NULL DEFAULT 0,
                verifier BLOB, busy TEXT
            );
            CREATE INDEX IF NOT EXISTS connections_owner_bot ON connections(owner,bot);
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

    @staticmethod
    def _public(row: sqlite3.Row) -> dict:
        doc = json.loads(row["public"])
        doc["busy"] = row["busy"] is not None
        return doc

    def _row(self, owner: str, bot: str, connection: str) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM connections WHERE owner=? AND bot=? AND id=?", (owner, bot, connection)).fetchone()
        if row is None:
            raise ConnectionError("connection_not_found", "Connection not found for this bot.", 404)
        return row

    def list(self, owner: str, bot: str) -> list[dict]:
        with self._lock:
            return [
                self._public(r) for r in self._db.execute("SELECT * FROM connections WHERE owner=? AND bot=? ORDER BY rowid", (owner, bot))
            ]

    def get(self, owner: str, bot: str, connection: str) -> tuple[dict, str]:
        with self._lock:
            row = self._row(owner, bot, connection)
            return self._public(row), row["binding"]

    def create(self, owner: str, public: dict, binding: str, state_hash: str, state_expires: float, verifier: dict, now: float) -> None:
        bot, connection = public["bot_id"], public["id"]
        cipher = self.vault.seal(verifier, owner=owner, bot=bot, connection=connection, purpose="pkce")
        with self._transaction():
            # Expired, unconsumed authorizations carry no account token. Bound
            # their retention while never deleting an in-flight exchange.
            self._db.execute(
                "DELETE FROM connections WHERE owner=? AND bot=? AND state_expires<=? AND state_used=0 AND busy IS NULL", (owner, bot, now)
            )
            rows = self._db.execute(
                "SELECT id,public,busy,secret IS NOT NULL AS has_secret FROM connections WHERE owner=? AND bot=? ORDER BY rowid",
                (owner, bot),
            ).fetchall()
            inactive = [
                r["id"]
                for r in rows
                if json.loads(r["public"])["status"] in ("disconnected", "error") and r["busy"] is None and not r["has_secret"]
            ]
            for old in inactive[:-16]:
                self._db.execute("DELETE FROM connections WHERE id=? AND owner=?", (old, owner))
            active_count = sum(json.loads(r["public"])["status"] not in ("disconnected", "error") or bool(r["has_secret"]) for r in rows)
            total = self._db.execute("SELECT count(*) FROM connections WHERE owner=?", (owner,)).fetchone()[0]
            if active_count >= 32 or total >= 4096:
                raise ConnectionError("connection_limit", "Disconnect existing connections before starting another consent.", 409)
            self._db.execute(
                """INSERT INTO connections(id,owner,bot,public,binding,state_hash,state_expires,verifier)
                                VALUES(?,?,?,?,?,?,?,?)""",
                (connection, owner, bot, json.dumps(public), binding, state_hash, state_expires, cipher),
            )

    def lookup_state(self, owner: str, state_hash: str) -> tuple[dict, str]:
        with self._lock:
            row = self._db.execute("SELECT * FROM connections WHERE owner=? AND state_hash=?", (owner, state_hash)).fetchone()
            if row is None:
                raise ConnectionError("invalid_oauth_state", "OAuth consent state is invalid, expired, or already used.", 400)
            return self._public(row), row["binding"]

    def claim_state(self, owner: str, bot: str, connection: str, state_hash: str, now: float) -> tuple[str, dict]:
        with self._transaction():
            row = self._row(owner, bot, connection)
            if (
                row["state_hash"] != state_hash
                or row["state_used"]
                or row["state_expires"] <= now
                or row["busy"] is not None
                or json.loads(row["public"])["status"] != "pending"
            ):
                raise ConnectionError("invalid_oauth_state", "OAuth consent state is invalid, expired, or already used.", 400)
            verifier = self.vault.open(row["verifier"], owner=owner, bot=bot, connection=connection, purpose="pkce")
            claim = secrets.token_hex(16)
            self._db.execute("UPDATE connections SET state_used=1,verifier=NULL,busy=? WHERE id=?", (claim, connection))
            return claim, verifier

    def claim(self, owner: str, bot: str, connection: str) -> tuple[str, dict, dict]:
        with self._transaction():
            row = self._row(owner, bot, connection)
            public = self._public(row)
            if row["busy"] is not None:
                raise ConnectionError(
                    "connection_busy",
                    "This connection has an in-flight or interrupted operation; inspect or disconnect it before retrying.",
                    409,
                )
            if public["status"] != "connected":
                raise ConnectionError("connection_not_ready", "This connection requires fresh owner consent before invocation.", 409)
            secret = self.vault.open(row["secret"], owner=owner, bot=bot, connection=connection, purpose="tokens")
            claim = secrets.token_hex(16)
            self._db.execute("UPDATE connections SET busy=? WHERE id=?", (claim, connection))
            return claim, public, secret

    def finish(self, owner: str, bot: str, connection: str, claim: str, public: dict, secret: dict | None = None) -> bool:
        cipher = None if secret is None else self.vault.seal(secret, owner=owner, bot=bot, connection=connection, purpose="tokens")
        public = {k: v for k, v in public.items() if k != "busy"}
        with self._transaction():
            if secret is None:
                result = self._db.execute(
                    "UPDATE connections SET public=?,busy=NULL WHERE id=? AND owner=? AND bot=? AND busy=?",
                    (json.dumps(public), connection, owner, bot, claim),
                )
            else:
                result = self._db.execute(
                    "UPDATE connections SET public=?,secret=?,busy=NULL WHERE id=? AND owner=? AND bot=? AND busy=?",
                    (json.dumps(public), cipher, connection, owner, bot, claim),
                )
            return result.rowcount == 1

    def detach(self, owner: str, bot: str, connection: str, updated_at: str) -> tuple[dict, dict, bool]:
        with self._transaction():
            row = self._row(owner, bot, connection)
            public = self._public(row)
            if public["status"] == "disconnected":
                return public, {}, False
            unreadable = False
            try:
                secret = self.vault.open(row["secret"], owner=owner, bot=bot, connection=connection, purpose="tokens")
            except ConnectionError:
                secret, unreadable = {}, True
            uncertain = row["busy"] is not None or unreadable or public.get("revocation_uncertain") is True
            public.update(status="disconnected", updated_at=updated_at, busy=False)
            public.pop("expires_at", None)
            public.pop("last_error", None)
            public.pop("cleanup_required", None)
            public.pop("revocation_pending", None)
            public.pop("revocation_uncertain", None)
            self._db.execute(
                "UPDATE connections SET public=?,secret=NULL,verifier=NULL,state_used=1,busy=NULL WHERE id=?",
                (json.dumps(public), connection),
            )
            return public, secret, uncertain

    def hold_for_deletion(self, owner: str, bot: str, updated_at: str) -> list[tuple[dict, bool]]:
        """Disable every local grant before any potentially slow provider I/O."""
        held = []
        with self._transaction():
            rows = self._db.execute("SELECT * FROM connections WHERE owner=? AND bot=? ORDER BY rowid", (owner, bot)).fetchall()
            for row in rows:
                public = self._public(row)
                if public["status"] != "disconnected":
                    cipher = row["secret"]
                    if cipher is not None:
                        secret = self.vault.open(cipher, owner=owner, bot=bot, connection=row["id"], purpose="tokens")
                        cipher = self.vault.seal(
                            {**secret, "revocation_only": True}, owner=owner, bot=bot, connection=row["id"], purpose="tokens"
                        )
                    public.update(
                        status="reauth_required",
                        updated_at=updated_at,
                        busy=False,
                        cleanup_required=True,
                        revocation_pending=True,
                        revocation_uncertain=row["busy"] is not None or public.get("revocation_uncertain") is True,
                    )
                    self._db.execute(
                        "UPDATE connections SET public=?,secret=?,state_used=1,verifier=NULL,busy=NULL WHERE id=?",
                        (json.dumps(public), cipher, row["id"]),
                    )
                held.append((public, row["secret"] is not None))
        return held

    def retain_unattempted(self, owner: str, bot: str, connection: str, detached: dict, tokens: dict, uncertain: bool) -> bool:
        """Retain ciphertext only when transport proves revocation never started."""
        held = {**tokens, "revocation_only": True}
        cipher = self.vault.seal(held, owner=owner, bot=bot, connection=connection, purpose="tokens")
        public = {
            **detached,
            "status": "reauth_required",
            "cleanup_required": True,
            "revocation_pending": True,
            "revocation_uncertain": uncertain,
            "revocation": {"provider_revoked": False, "revocation_status": "pending"},
        }
        with self._transaction():
            result = self._db.execute(
                """UPDATE connections SET public=?,secret=? WHERE owner=? AND bot=? AND id=?
                                        AND public=? AND secret IS NULL AND busy IS NULL""",
                (json.dumps(public), cipher, owner, bot, connection, json.dumps(detached)),
            )
            return result.rowcount == 1

    def set_revocation(self, owner: str, bot: str, connection: str, receipt: dict) -> dict:
        with self._transaction():
            row = self._row(owner, bot, connection)
            public = self._public(row)
            if public["status"] == "disconnected":
                public["revocation"] = receipt
                if receipt["revocation_status"] not in ("confirmed", "not_needed"):
                    public["cleanup_required"] = True
                self._db.execute("UPDATE connections SET public=? WHERE id=?", (json.dumps(public), connection))
            return public

    def delete_bot(self, owner: str, bot: str) -> None:
        with self._transaction():
            self._db.execute("DELETE FROM connections WHERE owner=? AND bot=?", (owner, bot))

    def close(self) -> None:
        with self._lock:
            self._db.close()
