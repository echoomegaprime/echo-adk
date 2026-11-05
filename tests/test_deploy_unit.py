"""The systemd unit must start the real CLI: ExecStart is parsed through the actual argparse parser.

Regression guard for the FORGE crash loop of 2026-11-05 (restart counter 75): ``--state-dir`` was placed after
``serve`` while the CLI defined it as a global option only.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from echo_adk.api.server import is_loopback_host
from echo_adk.cli import build_parser, cmd_serve
from tests.conftest import ROOT

UNIT = ROOT / "deploy" / "echo-adk-api.service"


def unit_lines() -> list[str]:
    return [line.strip() for line in UNIT.read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#")]


def exec_start_argv() -> list[str]:
    lines = [line for line in unit_lines() if line.startswith("ExecStart=")]
    assert len(lines) == 1, "exactly one ExecStart"
    argv = shlex.split(lines[0][len("ExecStart=") :])
    assert argv[:3] == ["/usr/bin/python3", "-m", "echo_adk.cli"], argv[:3]
    return argv[3:]


def test_exec_start_parses_with_the_real_cli_parser():
    args = build_parser().parse_args(exec_start_argv())
    assert args.command == "serve" and args.func is cmd_serve
    assert args.port == 8378 and is_loopback_host(args.host)
    assert Path(args.agents_dir) == Path("/home/forge/echo-adk/agents")
    assert Path(args.state_dir) == Path("/var/lib/echo/echo-adk") and args.serve_state_dir is None


def test_state_dir_is_accepted_before_or_after_the_subcommand():
    before = build_parser().parse_args(["--state-dir", "/a", "serve", "--port", "8378", "--agents-dir", "agents"])
    after = build_parser().parse_args(["serve", "--port", "8378", "--state-dir", "/b", "--agents-dir", "agents"])
    assert before.state_dir == "/a" and before.serve_state_dir is None
    assert after.serve_state_dir == "/b"
    for sub in ("run", "mcp"):
        parsed = build_parser().parse_args([sub, "agents/knowledge-scout.json", *(["prompt"] if sub == "run" else []), "--state-dir", "/c"])
        assert parsed.serve_state_dir == "/c"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["serve", "--bogus"])


def test_unit_environment_wiring():
    lines = unit_lines()
    assert "EnvironmentFile=-/etc/echo/echo-adk.env" in lines  # holds ECHO_ADK_GATE_KEY (and may override ECHO_GATE_URL)
    assert "Environment=ECHO_GATE_URL=http://127.0.0.1:8000" in lines
    assert "Environment=ECHO_ADK_STATE_DIR=/var/lib/echo/echo-adk" in lines
    assert not any("ECHO_ADK_GATE_KEY=" in line for line in lines), "the key never appears in the unit"
    assert "User=forge" in lines and "Restart=on-failure" in lines
    assert "MemoryHigh=300M" in lines and "MemoryMax=400M" in lines
    rw = next(line for line in lines if line.startswith("ReadWritePaths="))
    assert "/var/lib/echo/echo-adk" in rw and "/home/forge/echo-adk/agents" in rw


def test_gate_settings_come_from_the_process_environment(monkeypatch):
    """What the unit's Environment/EnvironmentFile provide is exactly what GateBroker reads."""
    from echo_adk.core.broker import GateBroker

    monkeypatch.setenv("ECHO_GATE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("ECHO_ADK_GATE_KEY", "unit-test-value")
    broker = GateBroker()
    assert broker.gate_url == "http://127.0.0.1:8000" and broker.key_env == "ECHO_ADK_GATE_KEY" and broker.key_present()
    monkeypatch.delenv("ECHO_ADK_GATE_KEY")
    assert not broker.key_present()
