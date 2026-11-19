from __future__ import annotations

import json

import pytest

from echo_adk.cli import main
from echo_adk.core.ledger import Ledger
from echo_adk.core.runner import Runner
from echo_adk.core.types import canonical_json, new_id, sha256_hex
from tests.fakes import FakeBroker, ScriptedModel, cap_action, final_action


@pytest.fixture
def completion(tmp_path, scout, registry, lease_for):
    run_id = new_id()
    result = Runner(
        ScriptedModel([cap_action("echo.knowledge.search", query="example"), final_action("finished")]),
        FakeBroker(),
        registry,
        ledger=Ledger(tmp_path),
    ).run(scout, "local worker check", lease_for(scout, run_id=run_id), run_id=run_id)
    proof = result.proof.to_dict()
    return tmp_path, run_id, proof


def verify(root, run_id, proof):
    return main(["--state-dir", str(root), "verify-completion", run_id, "--proof-sha256", sha256_hex(canonical_json(proof))])


def test_worker_proof_verifies_twice_without_writes(completion, capsys):
    root, run_id, proof = completion
    files = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
    for _ in range(2):
        assert verify(root, run_id, proof) == 0
        assert json.loads(capsys.readouterr().out)["events_sha256"] == proof["events_sha256"]
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()} == files


@pytest.mark.parametrize("change", ["event", "truncated", "proof", "other_run", "untrusted_flag", "missing_file", "invalid_json"])
def test_modified_or_incomplete_evidence_fails(completion, change):
    root, run_id, proof = completion
    path = root / "ledger" / f"{run_id}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    if change == "event":
        lines[-1] = lines[-1].replace("finished", "changed")
    elif change == "truncated":
        lines.pop()
    elif change == "untrusted_flag":
        lines[0] = lines[0].replace('"untrusted":true', '"untrusted":false')
    elif change == "proof":
        (root / "proofs" / f"{run_id}.json").write_text(json.dumps({**proof, "lease_id": new_id()}), encoding="utf-8")
    elif change == "other_run":
        run_id = new_id()
    elif change == "missing_file":
        path.unlink()
    else:
        lines[-1] = "{"
    if change != "missing_file":
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify(root, run_id, proof) == 2


@pytest.mark.parametrize("field,value", [("verdict", "UNKNOWN"), ("verdict", "FAIL"), ("actions", []), ("run_id", "0" * 32)])
def test_matching_digest_still_requires_coherent_pass(completion, field, value):
    root, run_id, proof = completion
    proof[field] = value
    (root / "proofs" / f"{run_id}.json").write_text(json.dumps(proof), encoding="utf-8")
    assert verify(root, run_id, proof) == 2


def test_absent_state_is_preserved(tmp_path):
    root = tmp_path / "absent"
    assert verify(root, new_id(), {}) == 2
    assert not root.exists()
