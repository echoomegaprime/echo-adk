"""Append-only JSONL ledger, Proof files and scoped memory under a state directory.

Layout::

    <state_dir>/ledger/<run_id>.jsonl   one Envelope per line, append-only
    <state_dir>/proofs/<run_id>.json    the Proof of the run
    <state_dir>/memory/<scope>.jsonl    memory records for one scope (``bot:<id>``, ``agent:<name>``)
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Optional

from .redact import redact_value
from .types import SHA256_HEX, Envelope, Proof, ValidationError, canonical_json, is_id, now_iso, sha256_hex

_SCOPE = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,96}$")


def safe_scope(scope: str) -> str:
    if not isinstance(scope, str) or not _SCOPE.match(scope):
        raise ValidationError("memory scope must match [a-z0-9][a-z0-9:_-]{0,96}")
    return scope.replace(":", "__")


def verify_completion(state_dir: Path, run_id: str, proof_sha256: str) -> Proof:
    """Verify a completed run using a canonical proof digest retained by its caller."""
    if not is_id(run_id) or not SHA256_HEX.fullmatch(proof_sha256):
        raise ValidationError("expected a run ID and a lowercase SHA-256 proof digest")
    try:
        raw_proof = json.loads((state_dir / "proofs" / f"{run_id}.json").read_text(encoding="utf-8"))
        if sha256_hex(canonical_json(raw_proof)) != proof_sha256:
            raise ValidationError("proof differs from the caller's retained digest")
        proof = Proof.from_dict(raw_proof)
        if proof.run_id != run_id or proof.verdict != "PASS":
            raise ValidationError("proof does not record PASS for this run")
        with (state_dir / "ledger" / f"{run_id}.jsonl").open(encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        if sha256_hex(canonical_json(records)) != proof.events_sha256:
            raise ValidationError("event ledger differs from the retained proof")
        events = [Envelope.from_dict(record) for record in records]
        if not events or any(event.run_id != run_id for event in events):
            raise ValidationError("event ledger must contain events for the same run")
        if events[-1].kind != "final" or not events[-1].ok or events[-1].verdict != "allowed":
            raise ValidationError("event ledger lacks a successful final event")
        derived = Proof.from_envelopes(run_id, proof.lease_id, events, proof.verdict)
        if derived.actions != proof.actions:
            raise ValidationError("proof actions differ from the recorded capability actions")
    except ValidationError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValidationError("completion evidence is missing or malformed") from exc
    return proof


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
    """Per-scope append-only memory. Bots write only under ``bot:<bot_id>``."""

    def __init__(self, state_dir: str | Path) -> None:
        self.root = Path(state_dir) / "memory"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, scope: str, record: dict[str, Any]) -> dict[str, Any]:
        cleaned, redacted = redact_value(dict(record))
        entry = {"ts": now_iso(), "scope": scope, "redacted": redacted, **cleaned}
        path = self.root / f"{safe_scope(scope)}.jsonl"
        with self._lock:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        return entry

    def read(self, scope: str, limit: int = 50) -> list[dict[str, Any]]:
        path = self.root / f"{safe_scope(scope)}.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        out = [json.loads(line) for line in lines if line.strip()]
        return out[-max(1, min(limit, 1000)) :]

    def scopes(self) -> Iterable[str]:
        for path in sorted(self.root.glob("*.jsonl")):
            yield path.stem.replace("__", ":")

    def delete(self, scope: str) -> bool:
        """Remove a scope's memory entirely (bot deletion leaves nothing behind)."""
        path = self.root / f"{safe_scope(scope)}.jsonl"
        with self._lock:
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return False
