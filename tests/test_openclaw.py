"""OpenClaw channel adapter + approval notifications, against a stand-in gateway (no live sends)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

import pytest

from echo_adk.api.server import BackgroundServer
from echo_adk.api.service import AdkService, ApiError
from echo_adk.channels.openclaw import (
    ChannelError,
    CliTransport,
    GatewayProbe,
    Health,
    OpenClawConfig,
    SendResult,
    parse_target,
    resolve_cli,
    scan_wedges,
    scrub,
    validate_channels,
    window_lines,
)
from echo_adk.core.policy import CapabilityRegistry
from echo_adk.core.redact import redact
from echo_adk.core.types import Capability, now_iso
from tests.conftest import http, wait_for
from tests.fakes import FailingModel, FakeBroker, ScriptedModel, cap_action, final_action

HERE = Path(__file__).resolve().parent
CHAT = "telegram:777"
APPROVER = "42"


class StandInGateway:
    """Stand-in OpenClaw gateway: settable health, recorded sends, optional send failure."""

    def __init__(self) -> None:
        self.healthy = True
        self.reason = "gateway /healthz timed out"
        self.fail_send: Optional[str] = None
        self.sends: list[dict[str, Any]] = []
        self.health_calls = 0
        self._lock = threading.Lock()

    def health(self) -> Health:
        self.health_calls += 1
        return Health(self.healthy, "" if self.healthy else self.reason)

    def send(self, channel, dest, text, buttons=None) -> SendResult:
        with self._lock:
            self.sends.append({"channel": channel, "dest": dest, "text": text, "buttons": [dict(b) for b in buttons or []]})
            if self.fail_send:
                return SendResult(False, self.fail_send)
            return SendResult(True, message_id=str(len(self.sends)))


class Script:
    """Per-test model behaviour: each run gets a fresh ScriptedModel over ``replies`` (or a FailingModel)."""

    def __init__(self) -> None:
        self.replies = [final_action("all green")]
        self.fail = False

    def factory(self, model_id: str):
        if self.fail:
            return FailingModel("qwen-local")
        return ScriptedModel(list(self.replies), cost_per_call_usd=0.4 if model_id == "claude" else 0.0)


@pytest.fixture
def registry() -> CapabilityRegistry:
    reg = CapabilityRegistry.builtin()
    reg.register(Capability("echo.test.privileged", 2, "test.exec", {"type": "object", "properties": {"x": {"type": "string"}}}))
    return reg


@pytest.fixture
def agents(agents_copy: Path) -> Path:
    priv = json.loads((agents_copy / "forge-steward.json").read_text(encoding="utf-8"))
    priv.update({"name": "priv-steward", "capabilities": ["echo.caps.search", "echo.test.privileged"]})
    (agents_copy / "priv-steward.json").write_text(json.dumps(priv), encoding="utf-8")
    return agents_copy


def make_service(agents: Path, state: Path, registry, script: Script, gateway, **config: Any) -> AdkService:
    cfg = {"enabled": True, "approvers": (f"telegram:{APPROVER}",), "health_ttl_s": 0.0, **config}
    return AdkService(
        agents,
        state,
        model_factory=script.factory,
        broker=FakeBroker(),
        registry=registry,
        start_scheduler=False,
        openclaw=OpenClawConfig(**cfg),
        channel_transport=gateway,
    )


@pytest.fixture
def gateway() -> StandInGateway:
    return StandInGateway()


@pytest.fixture
def script() -> Script:
    return Script()


@pytest.fixture
def svc(agents, tmp_path, registry, script, gateway):
    service = make_service(agents, tmp_path / "state", registry, script, gateway)
    yield service
    service.close()


def bot_with_chat(svc, target: str = CHAT, **extra: Any) -> dict[str, Any]:
    body = {"name": "steward", "agent": "forge-steward", "channels": [{"kind": "openclaw", "target": target}], **extra}
    bot = svc.create_bot(body)
    svc.bots.resume(bot["bot_id"])
    return bot


def settle(svc, run_id: str) -> dict[str, Any]:
    run = wait_for(lambda: (lambda r: r if r["status"] in ("done", "failed", "gated") else None)(svc.get_run(run_id)))
    svc.channels.flush()
    return run


def turn(svc, bot: dict[str, Any], text: str = "how is forge?", target: str = CHAT, sender: str = "555") -> dict[str, Any]:
    channel, dest = parse_target(target)
    status, queued = svc.openclaw_inbound({"channel": channel, "target": dest, "sender": sender, "text": text})
    assert status == 202 and queued["kind"] == "message" and queued["bot_id"] == bot["bot_id"]
    assert svc.bots.tick() >= 1
    run = settle(svc, queued["run_id"])
    wait_for(lambda: svc.bots.get_bot(bot["bot_id"])["status"] != "running")
    svc.channels.flush()
    return run


def only_approval(svc, bot_id: Optional[str] = None) -> dict[str, Any]:
    rows = svc.list_approvals(None, bot_id, None)["approvals"]
    assert len(rows) == 1, rows
    return rows[0]


# ----------------------------------------------------------------------------- specs and config


def test_channel_spec_validation():
    good = validate_channels(
        [
            {"kind": "openclaw", "target": "Telegram:-1001234567890:topic:7", "approvers": ["42"]},
            {"kind": "openclaw", "target": "discord:channel:222222222222222222", "allow_from": ["111111111111111111"]},
        ]
    )
    assert [c["target"] for c in good] == ["telegram:-1001234567890:topic:7", "discord:channel:222222222222222222"]
    assert good[0]["approvers"] == ["42"] and good[1]["allow_from"] == ["111111111111111111"]
    assert validate_channels(None) == []
    bad = [
        [{"kind": "slack-bot", "target": "telegram:1"}],
        [{"kind": "openclaw", "target": "fax:123"}],
        [{"kind": "openclaw", "target": "telegram:12 34"}],
        [{"kind": "openclaw", "target": 'telegram:1"; rm'}],
        [{"kind": "openclaw", "target": "telegram"}],
        [{"kind": "openclaw", "target": "telegram:1"}, {"kind": "openclaw", "target": "TELEGRAM:1"}],
        [{"kind": "openclaw", "target": "telegram:1", "token": "x"}],
        [{"kind": "openclaw", "target": "telegram:1", "approvers": "42"}],
        [{"kind": "openclaw", "target": f"telegram:{i}"} for i in range(5)],
    ]
    for value in bad:
        with pytest.raises(ChannelError):
            validate_channels(value)


def test_config_is_loopback_only_and_parses_env():
    for url in ("http://192.168.1.244:18789", "https://127.0.0.1:18789", "http://user:pw@127.0.0.1:18789", "http://127.0.0.1:18789/x"):
        with pytest.raises(ChannelError):
            OpenClawConfig(gateway_url=url)
    cfg = OpenClawConfig.from_env(
        {
            "ECHO_ADK_OPENCLAW": "1",
            "ECHO_ADK_OPENCLAW_URL": "http://[::1]:18789",
            "ECHO_ADK_OPENCLAW_NOTIFY": "telegram:123456789",
            "ECHO_ADK_OPENCLAW_APPROVERS": "telegram:42, discord:111111111111111111",
            "ECHO_ADK_APPROVAL_TTL_S": "600",
        }
    )
    assert cfg.enabled and cfg.gateway_url == "http://[::1]:18789" and cfg.notify_target == "telegram:123456789"
    assert cfg.approvers == ("telegram:42", "discord:111111111111111111") and cfg.approval_ttl_s == 600
    assert OpenClawConfig.from_env({}).enabled is False
    with pytest.raises(ChannelError):
        OpenClawConfig(approval_ttl_s=10)


# ----------------------------------------------------------------------------- bot shape and health gate


def test_bot_exposes_channel_state_ready(svc):
    bot = bot_with_chat(svc)
    assert bot["channels"] == [{"kind": "openclaw", "target": CHAT, "state": "ready"}]
    assert svc.bots.get_bot(bot["bot_id"])["channels"][0]["state"] == "ready"


def test_unreachable_gateway_bot_keeps_running_and_state_says_why(svc, gateway):
    gateway.healthy = False
    bot = bot_with_chat(svc)
    run = turn(svc, bot)
    assert run["status"] == "done"  # the bot answered; only the chat delivery is unavailable
    assert gateway.sends == []
    view = svc.bots.get_bot(bot["bot_id"])
    assert view["status"] == "idle"
    channel = view["channels"][0]
    assert channel["state"] == "unavailable: gateway /healthz timed out"
    assert channel["last_error"] == "unavailable: gateway /healthz timed out" and channel["dropped"] == 1
    assert svc.bot_messages(bot["bot_id"], None, None)["messages"][-1]["text"] == "all green"


def test_adapter_off_reports_unavailable_and_bot_still_answers(agents, tmp_path, registry, script):
    service = AdkService(
        agents, tmp_path / "s", model_factory=script.factory, broker=FakeBroker(), registry=registry, start_scheduler=False
    )
    try:
        bot = bot_with_chat(service)
        assert bot["channels"][0]["state"].startswith("unavailable: openclaw adapter off")
        assert turn(service, bot)["status"] == "done"
    finally:
        service.close()


def test_bad_env_or_missing_cli_turns_the_adapter_off_not_the_service(agents, tmp_path, registry, script, monkeypatch):
    monkeypatch.setenv("ECHO_ADK_OPENCLAW", "1")
    monkeypatch.setenv("ECHO_ADK_OPENCLAW_URL", "http://10.0.0.5:18789")
    for env, expected in (
        ({}, "unavailable: openclaw adapter off: invalid configuration: ECHO_ADK_OPENCLAW_URL must be a loopback origin"),
        (
            {"ECHO_ADK_OPENCLAW_URL": "http://127.0.0.1:18789", "ECHO_ADK_OPENCLAW_CLI": str(tmp_path / "no-cli")},
            "unavailable: openclaw adapter off: openclaw CLI",
        ),
    ):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        service = AdkService(
            agents, tmp_path / f"s{len(env)}", model_factory=script.factory, broker=FakeBroker(), registry=registry, start_scheduler=False
        )
        try:
            bot = bot_with_chat(service)
            assert bot["channels"][0]["state"].startswith(expected), bot["channels"][0]["state"]
        finally:
            service.close()


def test_health_is_cached_between_probes(agents, tmp_path, registry, script, gateway):
    service = make_service(agents, tmp_path / "s", registry, script, gateway, health_ttl_s=60.0)
    try:
        bot = bot_with_chat(service)
        for _ in range(5):
            service.bots.get_bot(bot["bot_id"])
        assert gateway.health_calls == 1
    finally:
        service.close()


def test_a_chat_handle_routes_to_one_bot(svc):
    bot_with_chat(svc)
    with pytest.raises(ApiError) as exc:
        svc.create_bot({"name": "twin", "agent": "forge-steward", "channels": [{"kind": "openclaw", "target": CHAT}]})
    assert exc.value.status == 409
    other = svc.create_bot({"name": "other", "agent": "forge-steward"})
    with pytest.raises(ApiError):
        svc.set_bot_channels(other["bot_id"], {"channels": [{"kind": "openclaw", "target": CHAT}]})
    moved = svc.set_bot_channels(other["bot_id"], {"channels": [{"kind": "openclaw", "target": "discord:channel:99"}]})
    assert moved["channels"][0]["target"] == "discord:channel:99"


# ----------------------------------------------------------------------------- inbound / outbound


def test_inbound_message_becomes_a_turn_and_reply_goes_back_to_that_chat(svc, gateway):
    bot = svc.create_bot(
        {
            "name": "steward",
            "agent": "forge-steward",
            "channels": [{"kind": "openclaw", "target": "discord:channel:1"}, {"kind": "openclaw", "target": CHAT}],
        }
    )
    svc.bots.resume(bot["bot_id"])
    run = turn(svc, bot, "how is forge?")
    assert run["status"] == "done"
    assert gateway.sends == [{"channel": "telegram", "dest": "777", "text": "[steward] all green", "buttons": []}]
    messages = svc.bot_messages(bot["bot_id"], None, None)["messages"]
    assert [m["role"] for m in messages] == ["user", "bot"] and messages[0]["text"] == "how is forge?"
    assert svc.bots.get_bot(bot["bot_id"])["channels"][1]["last_sent_at"]


def test_scheduled_turn_reply_goes_to_every_channel(svc, gateway):
    bot = svc.create_bot(
        {
            "name": "sched",
            "agent": "forge-steward",
            "schedule": {"every_s": 60},
            "channels": [{"kind": "openclaw", "target": CHAT}, {"kind": "openclaw", "target": "discord:channel:1"}],
        }
    )
    svc.bots.resume(bot["bot_id"])
    assert svc.bots.tick(now=1_000.0) == 1
    settle(svc, svc.store.list_runs(bot_id=bot["bot_id"])[0]["run_id"])
    wait_for(lambda: len(gateway.sends) == 2)
    assert sorted(s["channel"] for s in gateway.sends) == ["discord", "telegram"]


def test_inbound_validation_unmapped_duplicate_and_allow_from(svc):
    bot = svc.create_bot(
        {"name": "guarded", "agent": "forge-steward", "channels": [{"kind": "openclaw", "target": CHAT, "allow_from": ["555"]}]}
    )
    base = {"channel": "telegram", "target": "777", "sender": "555"}
    for body in (
        {**base, "text": "x" * 12001},
        {**base, "text": "hi", "token": "t"},
        {**base, "channel": "fax", "text": "hi"},
        {**base, "text": "hi", "callback": "x"},
        {**base},
        {**base, "callback": "something-else"},
    ):
        with pytest.raises(ApiError) as exc:
            svc.openclaw_inbound(body)
        assert exc.value.status == 400
    with pytest.raises(ApiError) as exc:
        svc.openclaw_inbound({**base, "target": "888", "text": "hi"})
    assert exc.value.status == 404
    with pytest.raises(ApiError) as exc:
        svc.openclaw_inbound({**base, "sender": "666", "text": "hi"})
    assert exc.value.status == 403
    status, first = svc.openclaw_inbound({**base, "text": "hi", "message_id": "tg-1"})
    status2, again = svc.openclaw_inbound({**base, "text": "hi", "message_id": "tg-1"})
    assert status == 202 and status2 == 200 and again == {"kind": "duplicate", "message_id": "tg-1"}
    assert svc.bots.get_bot(bot["bot_id"])["inbox_depth"] == 1


def test_send_failure_is_recorded_not_retried(svc, gateway):
    gateway.fail_send = "openclaw message send failed (exit 1): chat not found"
    bot = bot_with_chat(svc)
    turn(svc, bot)
    assert len(gateway.sends) == 1  # one attempt, no retry loop
    channel = svc.bots.get_bot(bot["bot_id"])["channels"][0]
    assert channel["state"] == "ready" and channel["last_error"].endswith("chat not found") and channel["dropped"] == 1


# ----------------------------------------------------------------------------- approvals: gated runs


def gated_bot(svc, script):
    script.replies = [cap_action("echo.monitor.health.node", node="forge"), final_action("forge ok")]
    bot = bot_with_chat(svc, lease={"scopes": ["echo.caps.search"], "max_uses": 5, "ttl_s": 120})
    run = turn(svc, bot)
    assert run["status"] == "gated"
    return bot, run


def test_gated_run_pushes_one_approval_with_buttons_and_no_token(svc, script, gateway):
    bot, run = gated_bot(svc, script)
    approval = only_approval(svc, bot["bot_id"])
    assert approval["kind"] == "gated" and approval["status"] == "pending" and approval["actions"] == ["approve", "deny"]
    assert approval["needs"] == "lease scope echo.monitor.health.node" and approval["run_id"] == run["run_id"]
    assert approval["lease"] == {"scopes": ["echo.caps.search", "echo.monitor.health.node"], "max_uses": 5, "ttl_s": 120}
    assert approval["notified"]["target"] == CHAT and approval["notified"]["state"] == "sent"
    note = [s for s in gateway.sends if s["buttons"]]
    assert len(note) == 1 and note[0]["channel"] == "telegram"
    values = [b["action"]["value"] for b in note[0]["buttons"]]
    assert values == [f"echoadk:approve:{approval['approval_id']}", f"echoadk:deny:{approval['approval_id']}"]
    assert all(len(v.encode()) <= 64 for v in values)  # Telegram callback_data limit
    assert approval["approval_id"] in note[0]["text"] and "needs: lease scope echo.monitor.health.node" in note[0]["text"]
    assert redact(note[0]["text"])[1] == 0 and redact(json.dumps(note[0]["buttons"]))[1] == 0
    assert svc.bots.get_bot(bot["bot_id"])["lease"]["scopes"] == ["echo.caps.search"]


def test_approve_mints_one_lease_for_one_rerun_and_never_widens_the_template(svc, script, gateway):
    bot, _ = gated_bot(svc, script)
    approval = only_approval(svc, bot["bot_id"])
    decided = svc.decide_approval(approval["approval_id"], "approve", {})
    assert decided["status"] == "approved" and decided["decided_by"] == "api"
    rerun_id = decided["result_run_id"]
    doc = svc.store.get_run(rerun_id)
    assert doc["lease"]["scopes"] == ["echo.caps.search", "echo.monitor.health.node"] and doc["lease"]["max_uses"] == 5
    assert svc.bots.tick() == 1  # dispatched through the normal daily-cap check
    run = settle(svc, rerun_id)
    assert run["status"] == "done" and run["proof"]["verdict"] == "PASS"
    assert svc.bots.get_bot(bot["bot_id"])["lease"]["scopes"] == ["echo.caps.search"]
    wait_for(lambda: any(s["text"] == "[steward] forge ok" for s in gateway.sends))
    with pytest.raises(ApiError) as exc:
        svc.decide_approval(approval["approval_id"], "approve", {})
    assert exc.value.status == 409  # single use
    with pytest.raises(ApiError):
        svc.decide_approval(approval["approval_id"], "approve", {"scopes": ["echo.shell.run"]})


def test_deny_runs_nothing(svc, script):
    bot, _ = gated_bot(svc, script)
    approval = only_approval(svc, bot["bot_id"])
    runs_before = len(svc.store.list_runs(bot_id=bot["bot_id"]))
    assert svc.decide_approval(approval["approval_id"], "deny", None)["status"] == "denied"
    assert len(svc.store.list_runs(bot_id=bot["bot_id"])) == runs_before and svc.bots.tick() == 0


def test_no_answer_before_ttl_is_denied_by_default(svc, script):
    bot, _ = gated_bot(svc, script)
    approval = only_approval(svc, bot["bot_id"])
    doc = svc.store.get_approval(approval["approval_id"])
    doc["expires_at"] = now_iso(datetime.now(timezone.utc) - timedelta(seconds=1))
    svc.store.put_approval(doc)
    with pytest.raises(ApiError) as exc:
        svc.decide_approval(approval["approval_id"], "approve", {})
    assert exc.value.status == 409 and "expired" in exc.value.message
    expired = svc.get_approval(approval["approval_id"])
    assert expired["status"] == "expired" and expired["decided_by"] == "default-deny" and "result_run_id" not in expired


def test_repeated_gate_does_not_flood_the_chat(svc, script, gateway):
    bot, _ = gated_bot(svc, script)
    turn(svc, bot, "again")
    assert len(svc.list_approvals("pending", bot["bot_id"], None)["approvals"]) == 1
    assert len([s for s in gateway.sends if s["buttons"]]) == 1


def test_chat_decisions_need_an_approver_in_the_notified_chat(svc, script, gateway):
    bot, _ = gated_bot(svc, script)
    approval_id = only_approval(svc, bot["bot_id"])["approval_id"]
    stranger = {"channel": "telegram", "target": "777", "sender": "999", "callback": f"echoadk:approve:{approval_id}"}
    with pytest.raises(ApiError) as exc:
        svc.openclaw_inbound(stranger)
    assert exc.value.status == 403
    elsewhere = {**stranger, "target": "888", "sender": APPROVER}
    with pytest.raises(ApiError) as exc:
        svc.openclaw_inbound(elsewhere)
    assert exc.value.status == 403
    assert svc.get_approval(approval_id)["status"] == "pending"
    status, body = svc.openclaw_inbound({**stranger, "sender": APPROVER})
    assert status == 200 and body["kind"] == "decision" and body["approval"]["status"] == "approved"
    assert body["approval"]["decided_by"] == f"openclaw:telegram:{APPROVER}"
    svc.channels.flush()
    assert gateway.sends[-1]["text"].startswith("echo-adk · approved: running once as run")


def test_text_replies_and_unclaimed_callback_text_also_decide(svc, script):
    bot, _ = gated_bot(svc, script)
    approval_id = only_approval(svc, bot["bot_id"])["approval_id"]
    status, body = svc.openclaw_inbound({"channel": "telegram", "target": "777", "sender": APPROVER, "text": f"Deny {approval_id}"})
    assert status == 200 and body["approval"]["status"] == "denied"
    svc.bots.tick()
    turn(svc, bot, "once more")
    second = svc.list_approvals("pending", bot["bot_id"], None)["approvals"][0]["approval_id"]
    text = f"callback_data: echoadk:approve:{second}"
    status, body = svc.openclaw_inbound({"channel": "telegram", "target": "777", "sender": APPROVER, "text": text})
    assert status == 200 and body["approval"]["status"] == "approved"


def test_per_channel_approvers_work_without_service_approvers(agents, tmp_path, registry, script, gateway):
    service = make_service(agents, tmp_path / "s", registry, script, gateway, approvers=())
    try:
        script.replies = [cap_action("echo.monitor.health.node", node="forge"), final_action("ok")]
        bot = service.create_bot(
            {
                "name": "steward",
                "agent": "forge-steward",
                "lease": {"scopes": ["echo.caps.search"], "max_uses": 5, "ttl_s": 120},
                "channels": [{"kind": "openclaw", "target": CHAT, "approvers": ["7"]}],
            }
        )
        service.bots.resume(bot["bot_id"])
        turn(service, bot)
        approval_id = only_approval(service, bot["bot_id"])["approval_id"]
        with pytest.raises(ApiError):
            service.openclaw_inbound({"channel": "telegram", "target": "777", "sender": APPROVER, "text": f"approve {approval_id}"})
        status, body = service.openclaw_inbound({"channel": "telegram", "target": "777", "sender": "7", "text": f"approve {approval_id}"})
        assert status == 200 and body["approval"]["status"] == "approved"
    finally:
        service.close()


def test_tier2_gate_is_a_notice_chat_cannot_mint_a_grant(svc, script, gateway):
    script.replies = [cap_action("echo.test.privileged", x="y")]
    bot = svc.create_bot({"name": "priv", "agent": "priv-steward", "channels": [{"kind": "openclaw", "target": CHAT}]})
    svc.bots.resume(bot["bot_id"])
    assert turn(svc, bot)["status"] == "gated"
    approval = only_approval(svc, bot["bot_id"])
    assert approval["status"] == "notice" and approval["actions"] == [] and approval["needs"] == "tier-2 grant"
    assert "signed" in approval["reason"] and "lease" not in approval
    assert gateway.sends[-1]["buttons"] == []
    with pytest.raises(ApiError) as exc:
        svc.decide_approval(approval["approval_id"], "approve", {})
    assert exc.value.status == 409


# ----------------------------------------------------------------------------- approvals: errors, caps, groups


def test_bot_error_pushes_retry_approval_and_approve_resumes_once(svc, script, gateway):
    script.fail = True
    bot = bot_with_chat(svc)
    run = turn(svc, bot)
    assert run["status"] == "failed" and svc.bots.get_bot(bot["bot_id"])["status"] == "error"
    approval = only_approval(svc, bot["bot_id"])
    assert approval["kind"] == "error" and approval["status"] == "pending" and "unavailable" in approval["summary"]
    assert [s for s in gateway.sends if not s["buttons"]] == []  # the failure is reported once, as the approval
    script.fail = False
    decided = svc.decide_approval(approval["approval_id"], "approve", {})
    assert svc.bots.get_bot(bot["bot_id"])["status"] == "idle"
    assert svc.bots.tick() == 1
    assert settle(svc, decided["result_run_id"])["status"] == "done"
    wait_for(lambda: any(s["text"] == "[steward] all green" for s in gateway.sends))


def test_daily_cap_hit_is_a_notice_that_never_raises_the_cap(svc, gateway):
    bot = svc.create_bot(
        {"name": "paid", "agent": "paid-steward", "daily_cost_cap_usd": 0.5, "channels": [{"kind": "openclaw", "target": CHAT}]}
    )
    svc.bots.resume(bot["bot_id"])
    assert turn(svc, bot, "turn one")["status"] == "done"
    refused = turn(svc, bot, "turn two")
    assert refused["status"] == "failed" and refused["error"]["code"] == "budget_exceeded"
    approval = only_approval(svc, bot["bot_id"])
    assert approval["kind"] == "daily_cap" and approval["status"] == "notice" and approval["actions"] == []
    assert "never raised" in approval["summary"]
    with pytest.raises(ApiError):
        svc.decide_approval(approval["approval_id"], "approve", {})
    assert svc.bots.get_bot(bot["bot_id"])["daily_cost_cap_usd"] == 0.5
    assert gateway.sends[-1]["buttons"] == [] and "daily cost cap reached" in gateway.sends[-1]["text"]


def test_finished_group_chat_notifies_with_summary(svc, gateway):
    a = svc.create_bot({"name": "alpha", "agent": "forge-steward", "channels": [{"kind": "openclaw", "target": "discord:channel:5"}]})
    b = svc.create_bot({"name": "beta", "agent": "forge-steward", "channels": [{"kind": "openclaw", "target": CHAT}]})
    group = svc.create_group({"name": "standup", "bots": [a["bot_id"], b["bot_id"]], "prompt": "status?"})
    wait_for(lambda: svc.bots.get_group(group["group_id"])["status"] in ("done", "failed"))
    wait_for(lambda: svc.list_approvals(None, None, None)["approvals"])
    svc.channels.flush()
    approval = svc.list_approvals(None, None, None)["approvals"][0]
    assert approval["kind"] == "group_done" and approval["group_id"] == group["group_id"] and approval["status"] == "notice"
    assert approval["notified"]["target"] == CHAT  # Telegram first across the group's channels
    assert "standup" in gateway.sends[-1]["text"] and "alpha: all green" in gateway.sends[-1]["text"]


def test_notify_target_overrides_bot_channels(agents, tmp_path, registry, script, gateway):
    service = make_service(agents, tmp_path / "s", registry, script, gateway, notify_target="telegram:123456789")
    try:
        script.fail = True
        bot = bot_with_chat(service, target="discord:channel:1")
        turn(service, bot, target="discord:channel:1")
        assert only_approval(service, bot["bot_id"])["notified"]["target"] == "telegram:123456789"
        assert gateway.sends[-1]["channel"] == "telegram" and gateway.sends[-1]["dest"] == "123456789"
    finally:
        service.close()


def test_unavailable_gateway_keeps_the_approval_visible(svc, script, gateway):
    gateway.healthy = False
    bot, _ = gated_bot(svc, script)
    approval = only_approval(svc, bot["bot_id"])
    assert approval["status"] == "pending" and approval["notified"]["state"] == "unavailable: gateway /healthz timed out"
    assert svc.decide_approval(approval["approval_id"], "approve", {})["status"] == "approved"


def test_delete_bot_purges_its_approvals(svc, script):
    bot, _ = gated_bot(svc, script)
    approval_id = only_approval(svc, bot["bot_id"])["approval_id"]
    svc.bots.delete(bot["bot_id"])
    assert svc.store.get_approval(approval_id) is None
    with svc.store._lock:
        rows = svc.store._db.execute("SELECT doc FROM approvals").fetchall()
    assert all(bot["bot_id"] not in row[0] for row in rows)


# ----------------------------------------------------------------------------- HTTP routes


def test_http_routes_inbound_approvals_and_channels(svc, script, gateway):
    script.replies = [cap_action("echo.monitor.health.node", node="forge"), final_action("forge ok")]
    with BackgroundServer(svc) as api:
        status, _, bot = http(
            "POST",
            f"{api}/bots",
            {"name": "web", "agent": "forge-steward", "lease": {"scopes": ["echo.caps.search"], "max_uses": 5, "ttl_s": 120}},
        )
        assert status == 201 and bot["channels"] == []
        status, _, bot = http("PUT", f"{api}/bots/{bot['bot_id']}/channels", {"channels": [{"kind": "openclaw", "target": CHAT}]})
        assert status == 200 and bot["channels"][0]["state"] == "ready"
        http("POST", f"{api}/bots/{bot['bot_id']}/resume")
        status, _, queued = http(
            "POST", f"{api}/channels/openclaw/inbound", {"channel": "telegram", "target": "777", "sender": "1", "text": "hi"}
        )
        assert status == 202 and set(queued) == {"kind", "bot_id", "message_id", "run_id"}
        svc.bots.tick()
        settle(svc, queued["run_id"])
        wait_for(lambda: http("GET", f"{api}/approvals?status=pending")[2]["approvals"])
        status, _, listing = http("GET", f"{api}/approvals?status=pending&bot_id={bot['bot_id']}&limit=5")
        assert status == 200 and len(listing["approvals"]) == 1
        approval = listing["approvals"][0]
        assert http("GET", f"{api}/approvals/{approval['approval_id']}")[2]["status"] == "pending"
        assert http("GET", f"{api}/approvals?status=bogus")[0] == 400
        assert http("POST", f"{api}/approvals/{approval['approval_id']}/approve", {"scopes": ["echo.shell.run"]})[0] == 400
        status, _, decided = http("POST", f"{api}/approvals/{approval['approval_id']}/approve")
        assert status == 200 and decided["status"] == "approved" and decided["result_run_id"]
        assert http("POST", f"{api}/approvals/{approval['approval_id']}/deny")[0] == 409
        assert http("GET", f"{api}/approvals/{'b' * 32}")[0] == 404
        status, _, body = http(
            "POST", f"{api}/channels/openclaw/inbound", {"channel": "telegram", "target": "1", "sender": "1", "text": "x"}
        )
        assert status == 404 and body["error"] == "not_found"


# ----------------------------------------------------------------------------- wedge scan, probes, CLI


def log_line(moment: datetime, message: str) -> str:
    return json.dumps({"time": moment.isoformat(timespec="milliseconds"), "level": "error", "msg": message})


def test_scan_wedges_detects_the_three_modes_inside_the_window():
    tz = timezone(timedelta(hours=-6))  # the gateway logs local time with its offset
    now = datetime.now(timezone.utc)
    recent, old = now.astimezone(tz) - timedelta(minutes=3), now.astimezone(tz) - timedelta(hours=2)
    published = "prepared reply dispatch runtime owner was not published for C:\\\\Users\\\\x\\\\agents\\\\echo-prime\\\\agent"
    assert "reply dispatch runtime owner was not published" in scan_wedges([log_line(recent, published)], now.timestamp())
    assert scan_wedges([log_line(old, published)], now.timestamp()) is None
    claim = "Error: Session 0f1e2d3c-aaaa-bbbb already has an active turn claim lane=session:agent:echo-prime:main"
    one = [log_line(recent, claim)]
    two = one + [log_line(recent + timedelta(seconds=40), claim)]
    assert scan_wedges(one, now.timestamp()) is None  # one conflict can be a genuine concurrent turn
    assert "leaked turn claim on session 0f1e2d3c" in scan_wedges(two, now.timestamp())
    held = 'lane task error: lane=main durationMs=12 error="another OpenClaw process owns state-lifecycle"'
    maintenance = "Idle shared-state database cleanup failed: another OpenClaw process owns state-lifecycle"
    lines = [log_line(recent, held), log_line(recent + timedelta(seconds=60), held), log_line(recent, maintenance)]
    assert "state-lifecycle lock held (2 refused runs" in scan_wedges(lines, now.timestamp())
    assert scan_wedges([log_line(recent, maintenance)] * 5, now.timestamp()) is None
    naive = json.dumps({"time": recent.replace(tzinfo=None).isoformat(), "msg": published})
    assert scan_wedges([naive, "not json " + published], now.timestamp()) is None  # no offset: skipped, never guessed


class _GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    routes: dict[str, tuple[int, Any, float]] = {}

    def log_message(self, *args: Any) -> None:
        return None

    def do_GET(self) -> None:
        status, body, delay = self.routes.get(self.path, (404, {"ok": False}, 0.0))
        if delay:
            time.sleep(delay)
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except OSError:  # the probe gave up first (stall test)
            pass


@pytest.fixture
def fake_gateway_http():
    handler = type("H", (_GatewayHandler,), {"routes": {}})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", handler.routes
    server.shutdown()
    server.server_close()


def test_gateway_probe_http_states_and_log_scan(fake_gateway_http, tmp_path):
    url, routes = fake_gateway_http
    logs = tmp_path / "openclaw"
    logs.mkdir()
    routes.update({"/healthz": (200, {"ok": True, "status": "live"}, 0.0), "/readyz": (200, {"ready": True}, 0.0)})
    probe = GatewayProbe(url, log_dir=logs, timeout_s=0.5)
    health = probe.check()
    assert health.ok and health.state() == "ready" and health.note == "no gateway log for today yet"
    routes["/readyz"] = (503, {"ready": False, "failing": ["channel:telegram:default", "agent-database:echo-prime"]}, 0.0)
    assert probe.check().state() == "unavailable: gateway /readyz HTTP 503 (failing: channel:telegram:default, agent-database:echo-prime)"
    assert GatewayProbe(url, log_dir=logs, require_ready=False, timeout_s=0.5).check().ok
    routes["/readyz"] = (200, {"ready": True}, 0.0)
    today = logs / f"openclaw-{datetime.now():%Y-%m-%d}.log"
    stamp = datetime.now(timezone.utc).astimezone()
    today.write_text(log_line(stamp, "prepared model runtime owner was not published for echo-prime") + "\n", encoding="utf-8")
    assert probe.check().state().startswith("unavailable: gateway wedged: model runtime owner was not published")
    routes["/healthz"] = (200, {"ok": True}, 1.5)
    assert probe.check().state() == "unavailable: gateway /healthz timed out"
    assert GatewayProbe(url, log_dir=tmp_path / "missing", timeout_s=0.5).check().state().startswith("unavailable")


def test_gateway_probe_refused_port_is_unavailable(tmp_path):
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    health = GatewayProbe(f"http://127.0.0.1:{port}", log_dir=tmp_path, timeout_s=0.5).check()
    # Linux refuses at once; Windows retries the SYN for about 2 s, so the 0.5 s probe times out first.
    assert not health.ok and health.reason in ("gateway /healthz refused or unreachable", "gateway /healthz timed out")


def test_resolve_cli_refuses_unresolvable_cmd_shims(tmp_path):
    shim = tmp_path / "openclaw.cmd"
    shim.write_text("@echo off\n", encoding="utf-8")
    with pytest.raises(ChannelError, match="refusing to run the .cmd shim"):
        resolve_cli(str(shim), sys.executable)
    entry = tmp_path / "node_modules" / "openclaw" / "openclaw.mjs"
    entry.parent.mkdir(parents=True)
    entry.write_text("// entry\n", encoding="utf-8")
    assert resolve_cli(str(shim), sys.executable) == [sys.executable, str(entry)]
    with pytest.raises(ChannelError):
        resolve_cli(str(tmp_path / "nope"), sys.executable)


def test_cli_transport_argv_is_verbatim_and_failures_are_redacted(tmp_path, monkeypatch):
    out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_OPENCLAW_OUT", str(out))
    probe = GatewayProbe(log_dir=tmp_path)
    transport = CliTransport([sys.executable, str(HERE / "fake_openclaw.py")], probe, timeout_s=10)
    text = '[bot] 100% done & "quoted" | ^caret\nline two'
    buttons = [{"label": "Approve once", "action": {"type": "callback", "value": "echoadk:approve:" + "a" * 32}}]
    result = transport.send("telegram", "-1001234567890", text, buttons)
    assert result.ok and result.message_id == "4242"
    argv = json.loads(out.read_text(encoding="utf-8"))
    assert argv[:6] == ["message", "send", "--channel=telegram", "--target=-1001234567890", f"--message={text}", "--json"]
    assert json.loads(argv[6].split("=", 1)[1]) == {"blocks": [{"type": "buttons", "buttons": buttons}]}
    monkeypatch.setenv("FAKE_OPENCLAW_MODE", "fail")
    failed = transport.send("telegram", "1", "[bot] x")
    assert not failed.ok and "chat not found" in failed.error and "abc123secretvalue" not in failed.error
    monkeypatch.setenv("FAKE_OPENCLAW_MODE", "hang")
    slow = CliTransport([sys.executable, str(HERE / "fake_openclaw.py")], probe, timeout_s=1).send("telegram", "1", "[bot] x")
    assert not slow.ok and "timed out" in slow.error
    with pytest.raises(ChannelError):
        transport.argv("telegram", "1 2", "x")


BRIDGE = HERE.parent / "deploy" / "openclaw-hook" / "echo-adk-bridge" / "handler.js"
NODE_DRIVER = """
const mod = await import(process.argv[2]);
const event = JSON.parse(process.argv[3]);
await mod.default(event);
const ignored = mod.toInbound({ ...event, context: { ...event.context, conversationId: "999" } }, mod.targets());
console.log(JSON.stringify({ ignored, remote: mod.bridgeUrl({ ECHO_ADK_BRIDGE_URL: "http://192.168.1.5:8378" }) }));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_bridge_hook_forwards_a_mapped_chat_message_end_to_end(svc, gateway, tmp_path):
    bot = bot_with_chat(svc)
    entry = tmp_path / "handler.mjs"  # force ESM whatever the node version
    entry.write_text(BRIDGE.read_text(encoding="utf-8"), encoding="utf-8")
    driver = tmp_path / "driver.mjs"
    driver.write_text(NODE_DRIVER, encoding="utf-8")
    event = {
        "type": "message",
        "action": "received",
        "context": {
            "channelId": "telegram",
            "conversationId": "777",
            "from": "555",
            "content": "status of forge please",
            "messageId": "tg-9",
            "metadata": {"senderId": "555"},
        },
    }
    with BackgroundServer(svc) as api:
        env = {**os.environ, "ECHO_ADK_BRIDGE_URL": api, "ECHO_ADK_BRIDGE_TARGETS": f"{CHAT},discord:channel:1"}
        done = subprocess.run(["node", str(driver), entry.as_uri(), json.dumps(event)], capture_output=True, text=True, timeout=60, env=env)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1]) == {"ignored": None, "remote": None}
    messages = svc.bot_messages(bot["bot_id"], None, None)["messages"]
    assert [m["text"] for m in messages] == ["status of forge please"]
    assert svc.store.get_message(messages[0]["message_id"])["origin"] == CHAT


# ----------------------------------------------------------------------------- review fixes (Codex pass)


def test_scrub_removes_bot_tokens_and_url_paths(tmp_path, monkeypatch):
    fake_token = "bot" + "1" * 9 + ":" + "Q" * 35  # Telegram shape, built at run time so no token-like literal ships
    leaked = f"POST https://api.telegram.org/{fake_token}/sendMessage failed: 400 chat not found"
    cleaned = scrub(leaked)
    assert "Q" * 35 not in cleaned and "api.telegram.org/[path]" in cleaned and "chat not found" in cleaned
    discord = "M" + "a" * 25 + "." + "b" * 6 + "." + "c" * 30
    assert "a" * 25 not in scrub(f"discord rejected {discord}")
    monkeypatch.setenv("FAKE_OPENCLAW_MODE", "fail")
    monkeypatch.setenv("FAKE_OPENCLAW_ERROR", leaked)
    result = CliTransport([sys.executable, str(HERE / "fake_openclaw.py")], GatewayProbe(log_dir=tmp_path)).send("telegram", "1", "x")
    assert not result.ok and "Q" * 35 not in result.error


def test_node_wrapper_scripts_are_refused(tmp_path):
    entry = tmp_path / "openclaw.mjs"
    entry.write_text("// entry\n", encoding="utf-8")
    wrapper = tmp_path / "node.cmd"
    wrapper.write_text("@echo off\n", encoding="utf-8")
    with pytest.raises(ChannelError, match="node wrapper"):
        resolve_cli(str(entry), str(wrapper))
    assert resolve_cli(str(entry), sys.executable) == [sys.executable, str(entry)]


def test_detached_origin_chat_never_receives_a_broadcast(svc, gateway):
    bot = svc.create_bot(
        {
            "name": "two",
            "agent": "forge-steward",
            "channels": [{"kind": "openclaw", "target": CHAT}, {"kind": "openclaw", "target": "discord:channel:9"}],
        }
    )
    svc.bots.resume(bot["bot_id"])
    status, queued = svc.openclaw_inbound({"channel": "telegram", "target": "777", "sender": "5", "text": "private question"})
    svc.set_bot_channels(bot["bot_id"], {"channels": [{"kind": "openclaw", "target": "discord:channel:9"}]})
    svc.bots.tick()
    assert settle(svc, queued["run_id"])["status"] == "done"
    wait_for(lambda: svc.bots.get_bot(bot["bot_id"])["status"] == "idle")
    svc.channels.flush()
    assert gateway.sends == []


def test_chat_decision_needs_a_delivered_notification(svc, script, gateway):
    gateway.healthy = False
    bot, _ = gated_bot(svc, script)
    approval_id = only_approval(svc, bot["bot_id"])["approval_id"]
    gateway.healthy = True
    with pytest.raises(ApiError) as exc:
        svc.openclaw_inbound({"channel": "telegram", "target": "777", "sender": APPROVER, "text": f"approve {approval_id}"})
    assert exc.value.status == 403 and svc.get_approval(approval_id)["status"] == "pending"


def test_malformed_url_is_a_config_error_not_a_crash(agents, tmp_path, registry, script, monkeypatch):
    with pytest.raises(ChannelError):
        OpenClawConfig(gateway_url="http://[::1")
    with pytest.raises(ChannelError):
        OpenClawConfig(gateway_url="http://127.0.0.1:99999")
    monkeypatch.setenv("ECHO_ADK_OPENCLAW", "1")
    monkeypatch.setenv("ECHO_ADK_OPENCLAW_URL", "http://[::1")
    service = AdkService(
        agents, tmp_path / "s", model_factory=script.factory, broker=FakeBroker(), registry=registry, start_scheduler=False
    )
    try:
        assert service.channels.health().state().startswith("unavailable: openclaw adapter off: invalid configuration")
    finally:
        service.close()


def test_rejected_message_does_not_poison_duplicate_detection(svc):
    body = {"channel": "telegram", "target": "777", "sender": "5", "text": "hello", "message_id": "tg-77"}
    with pytest.raises(ApiError):
        svc.openclaw_inbound(body)  # not mapped yet
    bot_with_chat(svc)
    status, queued = svc.openclaw_inbound(body)
    assert status == 202 and queued["kind"] == "message"


def test_failed_mint_consumes_the_approval_as_denied(svc, script, agents):
    bot, _ = gated_bot(svc, script)
    approval_id = only_approval(svc, bot["bot_id"])["approval_id"]
    steward = svc.agents["forge-steward"]
    svc.agents["forge-steward"] = type(steward)(**{**steward.__dict__, "capabilities": ["echo.caps.search"]})
    with pytest.raises(ApiError) as exc:
        svc.decide_approval(approval_id, "approve", {})
    assert exc.value.status == 409
    final = svc.get_approval(approval_id)
    assert final["status"] == "denied" and "allowlist" in final["reason"] and "result_run_id" not in final


def test_missing_log_dir_fails_closed_and_long_logs_cover_the_window(fake_gateway_http, tmp_path):
    url, routes = fake_gateway_http
    routes.update({"/healthz": (200, {"ok": True}, 0.0), "/readyz": (200, {"ready": True}, 0.0)})
    missing = GatewayProbe(url, log_dir=tmp_path / "missing", timeout_s=0.5).check()
    assert missing.state().startswith("unavailable: wedge check impossible: gateway log dir")
    log = tmp_path / "big.log"
    now = datetime.now(timezone.utc)
    old = log_line(now - timedelta(hours=1), "old line " + "x" * 200)
    fresh = log_line(now - timedelta(minutes=1), "fresh line " + "y" * 200)
    log.write_text("\n".join([old] * 3000 + [fresh] * 3000) + "\n", encoding="utf-8")  # ~1.5 MB of fresh lines
    lines, covered = window_lines(log, (now - timedelta(minutes=15)).timestamp(), first_bytes=200_000, max_bytes=4_000_000)
    assert covered and any("old line" in line for line in lines)
    _, short = window_lines(log, (now - timedelta(minutes=15)).timestamp(), first_bytes=100_000, max_bytes=200_000)
    assert short is False
