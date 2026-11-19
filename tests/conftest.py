from __future__ import annotations

import json
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from echo_adk.agents import load_agent  # noqa: E402
from echo_adk.core.policy import CapabilityRegistry  # noqa: E402
from echo_adk.core.types import Capability, EchoAgent, Lease, new_id  # noqa: E402

AGENTS_DIR = ROOT / "agents"


@pytest.fixture
def scout() -> EchoAgent:
    return load_agent(AGENTS_DIR / "knowledge-scout.json")


@pytest.fixture
def steward() -> EchoAgent:
    return load_agent(AGENTS_DIR / "forge-steward.json")


@pytest.fixture
def registry() -> CapabilityRegistry:
    reg = CapabilityRegistry.builtin()
    # a tier-2 capability for grant tests (not in the live seed; test-only)
    reg.register(
        Capability(
            "echo.test.privileged",
            2,
            "test.exec",
            {"type": "object", "properties": {"x": {"type": "string"}}},
            "green",
            "run",
            "test tier-2",
        )
    )
    return reg


@pytest.fixture
def lease_for() -> Callable[..., Lease]:
    def make(agent: EchoAgent, *, run_id: Optional[str] = None, scopes=None, ttl_s: int = 600, max_uses: int = 40, grant_ref=None) -> Lease:
        return Lease.mint(
            run_id or new_id(), scopes if scopes is not None else agent.capabilities, ttl_s=ttl_s, max_uses=max_uses, grant_ref=grant_ref
        )

    return make


@pytest.fixture
def agents_copy(tmp_path: Path) -> Path:
    """A private copy of the shipped agents plus a paid-lane agent for cost tests."""
    target = tmp_path / "agents"
    shutil.copytree(AGENTS_DIR, target)
    paid = json.loads((AGENTS_DIR / "forge-steward.json").read_text(encoding="utf-8"))
    paid.update(
        {
            "name": "paid-steward",
            "lane": {"node": "forge", "model": "claude", "budget": {"max_calls": 12, "max_cost_usd": 2.0, "max_wall_s": 300}},
        }
    )
    (target / "paid-steward.json").write_text(json.dumps(paid), encoding="utf-8")
    return target


def http(method: str, url: str, body: Any = None, *, timeout: float = 10.0) -> tuple[int, dict[str, str], Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            headers = {k.lower(): v for k, v in response.headers.items()}
            return response.status, headers, json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        headers = {k.lower(): v for k, v in exc.headers.items()}
        return exc.code, headers, json.loads(raw.decode("utf-8")) if raw else None


def wait_for(predicate: Callable[[], Any], *, timeout: float = 15.0, interval: float = 0.05) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met within timeout")
