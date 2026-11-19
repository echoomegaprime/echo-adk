"""Verified readback of the completed November 19 synthetic FieldDesk pilot."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ..core.types import Envelope, Proof
from .service import AdkService, ApiError

# Digest of the filename -> SHA-256 map for the fixture, checkpoint, score and nine drafts.
# This identity describes one historical pilot. Different output is never served under this ID.
PILOT_DIGEST = "2b8a4fe3a71f4c0be6292a53af1d1ac779153bba8454409abf6fbf02b310ae46"
RUNS_DIGEST = "cce6dccc1e305f90379a1c6cfadb8882e005956d43a00f94c7410df5ac83e79f"
AGENT = "lyra-test-goal-loop"
MODEL = "c3po-code:echo-qwen38-abliterated-256k"
MAX_FILE_BYTES = 256 * 1024


def _digest(mapping: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _read_inputs(root: Path) -> dict[str, bytes]:
    paths = ["data/appointments.json", "score.json", "state.json"]
    # Only the public synthetic fixture, checkpoint, score and known draft filenames are eligible.
    # In particular the sealed key and arbitrary paths from the checkpoint are never opened.
    paths.extend(f"drafts/FD-{i:02d}.md" for i in range(1, 21) if (root / f"drafts/FD-{i:02d}.md").is_file())
    files = {}
    for name in paths:
        path = root / name
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("missing pilot file")
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("pilot file too large")
        files[name] = data
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    if _digest(hashes) != PILOT_DIGEST:
        raise ValueError("pilot files do not match the completed pilot")
    return files


def fielddesk_result(service: AdkService) -> dict[str, Any]:
    """Only read original receipts. An absent or incomplete proof never triggers a model or tool call."""
    setting = os.environ.get("ECHO_ADK_FIELDDESK_PROOF_DIR")
    if not setting:
        raise ApiError(503, "pilot_unavailable", "The completed synthetic FieldDesk pilot is not mounted.")
    try:
        files = _read_inputs(Path(setting))
        fixture = json.loads(files["data/appointments.json"])
        state = json.loads(files["state.json"])["appointments"]
        score = json.loads(files["score.json"])
        decisions = []
        checked_runs = {}
        for item in fixture["appointments"]:
            item_id = item["id"]
            entry = state[item_id]
            run = service.get_run(entry["run_id"])
            proof = run.get("proof") or {}
            responders = [step["data"].get("answered") or {} for step in run["steps"] if step["kind"] == "model"]
            if (
                run["run_id"] != entry["run_id"]
                or run["agent"] != AGENT
                or run["status"] != "done"
                or proof.get("run_id") != run["run_id"]
                or proof.get("verdict") != "PASS"
                or proof.get("actions") != []
                or proof.get("events_sha256") != Proof.hash_events(Envelope.from_dict(step) for step in run["steps"])
                or run["spent"]["cost_usd"] != 0
                or not responders
                or any(r.get("provider") != "qwen-local" or r.get("model") != MODEL for r in responders)
            ):
                raise ValueError("prior worker receipt drifted")
            checked_runs[run["run_id"]] = proof["events_sha256"]
            draft = files.get(f"drafts/{item_id}.md")
            decisions.append({
                "appointment_id": item_id, "decision": entry["decision"], "source_note": item["note"],
                "deciding_quote": entry["source_quote"], "draft": draft.decode("utf-8") if draft is not None else None,
                "run_id": run["run_id"], "events_sha256": proof["events_sha256"],
            })
        if _digest(checked_runs) != RUNS_DIGEST:
            raise ValueError("worker receipts do not match the completed pilot")
    except (OSError, KeyError, TypeError, ValueError, AttributeError, ApiError) as exc:
        raise ApiError(503, "pilot_unavailable", "The completed pilot files or ADK worker receipts could not be verified.") from exc
    return {
        "kind": "completed_pilot", "goal_id": PILOT_DIGEST, "synthetic": True, "goal": fixture["goal"],
        "score": {"correct": score["decisions_correct"], "total": score["decisions_total"]},
        "model": MODEL, "decisions": decisions,
    }
