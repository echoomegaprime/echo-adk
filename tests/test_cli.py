from __future__ import annotations

import json

from echo_adk.cli import main
from tests.conftest import AGENTS_DIR, ROOT


def test_validate_shipped_agents(capsys, tmp_path):
    code = main(["--state-dir", str(tmp_path), "validate", str(AGENTS_DIR)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"] is True and {a["name"] for a in out["agents"]} == {"knowledge-scout", "forge-steward"}


def test_validate_reports_unknown_capabilities(capsys, tmp_path):
    agent = json.loads((AGENTS_DIR / "forge-steward.json").read_text(encoding="utf-8"))
    agent["capabilities"] = ["echo.not.registered"]
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "forge-steward.json").write_text(json.dumps(agent), encoding="utf-8")
    code = main(["--state-dir", str(tmp_path / "state"), "validate", str(tmp_path / "agents")])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["ok"] is False and "default-deny" in out["problems"][0]["error"]


def test_card_command_prints_a2a_and_echo_cards(capsys, tmp_path):
    assert main(["--state-dir", str(tmp_path), "card", str(AGENTS_DIR / "knowledge-scout.json")]) == 0
    card = json.loads(capsys.readouterr().out)
    assert card["name"] == "knowledge-scout" and card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"
    assert (
        main(["--state-dir", str(tmp_path), "card", "--format", "echo-a2a", str(ROOT / "docs" / "examples" / "knowledge-scout.yaml")]) == 0
    )
    assert json.loads(capsys.readouterr().out)["protocol"] == "echo-a2a/1.0"


def test_serve_refuses_non_loopback_host(capsys, tmp_path):
    code = main(["--state-dir", str(tmp_path), "serve", "--host", "0.0.0.0", "--port", "0", "--agents-dir", str(AGENTS_DIR)])
    err = capsys.readouterr().err
    assert code == 2 and "loopback only" in err


def test_run_with_unreachable_loopback_model_is_unavailable_not_rerouted(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", "test-key-not-used-because-model-fails-first")
    code = main(
        [
            "--state-dir",
            str(tmp_path),
            "--models-config",
            json.dumps({"endpoint": "http://127.0.0.1:1", "timeout_s": 1}),
            "run",
            str(AGENTS_DIR / "knowledge-scout.json"),
            "hello",
        ]
    )
    captured = capsys.readouterr()
    proof = json.loads(captured.out)
    assert code == 1 and proof["verdict"] == "FAIL"
    assert "test-key-not-used" not in captured.out + captured.err
    ledger_files = list((tmp_path / "ledger").glob("*.jsonl"))
    assert len(ledger_files) == 1
    first = json.loads(ledger_files[0].read_text(encoding="utf-8").splitlines()[0])
    assert first["kind"] == "model" and first["ok"] is False and first["data"]["provider"] == "qwen-local"


def test_models_config_with_credential_field_is_refused(capsys, tmp_path):
    code = main(
        [
            "--state-dir",
            str(tmp_path),
            "--models-config",
            json.dumps({"endpoint": "http://127.0.0.1:1", "api_key": "x"}),
            "run",
            str(AGENTS_DIR / "knowledge-scout.json"),
            "hello",
        ]
    )
    assert code == 2 and "credential-shaped" in capsys.readouterr().err
