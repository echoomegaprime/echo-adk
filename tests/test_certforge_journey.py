"""The submitted offline journey must execute, and must fail on broken invariants."""

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from echo_adk.bots.manager import BotManager
from echo_adk.core.policy import Decision, Policy
from echo_adk.sdk import manifest as sdk_manifest

ROOT = Path(__file__).resolve().parents[1]
JOURNEY = ROOT / "scripts" / "certforge_journey.py"


def load_journey():
    return runpy.run_path(str(JOURNEY))


def test_app_opt_in_names_the_exact_committed_python_journey():
    apps = json.loads((ROOT / ".echo" / "apps.json").read_text())
    manifest = json.loads((ROOT / ".echo" / "certification.json").read_text())
    assert apps == {"version": 1, "apps": {"certification-forge": {"enabled": True}}}
    assert manifest == {"version": 1, "journey": ["python3", "-B", "scripts/certforge_journey.py"]}
    assert JOURNEY.is_file()


def test_journey_runs_without_installed_dependencies_or_assert_statements(tmp_path):
    # -I ignores environment/user Python paths; -S omits site-packages, so a
    # developer's pytest/FastAPI installation cannot conceal a sandbox failure.
    # -O proves explicit checks remain effective when Python assertions are off.
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-O", str(JOURNEY)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "passed" and report["mode"] == "offline"
    assert set(report["checks"]) == {"policy", "sdk_contract", "durable_events_and_budget", "memory", "interval_schedule"}
    assert report["model_calls"] == report["sdk_calls"] == 0
    assert report["production_e2e"] == "not_performed" and report["issuer_verdict"] == "not_issued"
    assert "optional SDK host admission" in report["not_verified"]


def test_journey_rejects_a_policy_that_silently_allows_missing_authority(monkeypatch):
    journey = load_journey()
    monkeypatch.setattr(Policy, "evaluate", lambda *args, **kwargs: Decision("allowed", 0, "broken policy"))
    with pytest.raises(journey["JourneyFailure"], match="missing lease"):
        journey["check_policy"]()


@pytest.mark.parametrize("change", [{"required_scope": "tier:2"}, {"required_scope": "echobots.manage"}, {"danger_tier": 1}])
def test_journey_rejects_changed_management_scope_or_lowered_risk_tier(monkeypatch, change):
    journey = load_journey()
    generate = sdk_manifest.proposed_manifest

    def changed_manifest(origin):
        value = generate(origin)
        value["capabilities"][0].update(change)
        return value

    monkeypatch.setattr(sdk_manifest, "proposed_manifest", changed_manifest)
    with pytest.raises(journey["JourneyFailure"], match="scope or risk tier"):
        journey["check_sdk_contract"]()


def test_journey_rejects_a_duplicate_event_with_a_different_run(monkeypatch, tmp_path):
    journey = load_journey()
    deliver = BotManager.deliver_event

    def broken_receipt(self, bot_id, body):
        result = deliver(self, bot_id, body)
        return {**result, "run_id": "0" * 32} if result["duplicate"] else result

    monkeypatch.setattr(BotManager, "deliver_event", broken_receipt)
    with pytest.raises(journey["JourneyFailure"], match="duplicate event"):
        journey["check_durable_events_and_budget"](tmp_path)


def test_journey_does_not_hide_execution_attempts(monkeypatch, tmp_path):
    journey = load_journey()
    # An accidental resume/tick inside the journey must surface the attempt,
    # rather than silently replacing a real run with an invented successful one.
    monkeypatch.setattr(BotManager, "tick", lambda self, **kwargs: self.service.model_factory("qwen-local"))
    with pytest.raises(journey["JourneyFailure"], match="model"):
        journey["check_durable_events_and_budget"](tmp_path)


def test_offline_guard_refuses_process_and_network_use_in_a_fresh_interpreter():
    code = (
        "import runpy,sys,socket,subprocess,json; "
        "j=runpy.run_path(sys.argv[1]); sys.addaudithook(j['offline_audit']); "
        "blocked=[]\n"
        "for action in (lambda:socket.getaddrinfo('localhost',80),"
        "lambda:subprocess.run([sys.executable,'-c','pass'],check=True)):\n"
        " try: action()\n"
        " except j['JourneyFailure']: blocked.append(True)\n"
        "print(json.dumps(blocked))\n"
    )
    result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", code, str(JOURNEY)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [True, True]
