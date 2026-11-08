"""The versioned bot automation contract, driven over real loopback HTTP."""

import urllib.error
import urllib.request

import pytest

from echo_adk.api.server import BackgroundServer
from echo_adk.api.service import AdkService
from tests.conftest import http, wait_for
from tests.fakes import FakeBroker, ScriptedModel, final_action


@pytest.fixture
def api_v2(agents_copy, tmp_path):
    service = AdkService(
        agents_copy,
        tmp_path / "state",
        start_scheduler=False,
        model_factory=lambda _: ScriptedModel([final_action("event processed")]),
        broker=FakeBroker(),
    )
    with BackgroundServer(service) as url:
        yield service, url
    service.close()


def test_v2_reports_local_capabilities_and_held_budget_without_changing_v1_health(api_v2):
    _, url = api_v2
    status, _, health = http("GET", url + "/v2/health")
    assert status == 200 and health["contract_version"] == 2
    assert health["features"]["calendar_schedules"] and health["features"]["memory_recall"]
    assert health["connections"]["memory"] == {"provider": "local", "recall": True, "durable": True, "shared": False}
    assert health["connections"]["browser"]["available"] is False
    assert health["budgets"]["timezone"] == "UTC" and health["budgets"]["reserved_usd"] == 0
    assert "features" not in http("GET", url + "/health")[2]


def test_calendar_creation_is_versioned_and_does_not_silently_change_v1(api_v2):
    _, url = api_v2
    payload = {
        "name": "Morning",
        "agent": "knowledge-scout",
        "schedule": {"at": "09:00", "tz": "America/Chicago", "weekdays": [1, 2, 3, 4, 5], "missed": "once"},
    }
    assert http("POST", url + "/bots", payload)[0] == 400
    status, _, bot = http("POST", url + "/v2/bots", payload)
    assert status == 201 and bot["status"] == "paused"
    assert bot["next_run_at"] and bot["budget"]["reserved_usd"] == 0
    status, _, listing = http("GET", url + "/v2/bots")
    assert status == 200 and listing["bots"][0]["schedule"] == payload["schedule"]
    assert http("GET", url + "/bots")[0] == 409  # explicit upgrade, never misrepresent a calendar schedule


def test_event_is_deduplicated_while_paused_then_produces_a_proven_run(api_v2):
    service, url = api_v2
    bot = http("POST", url + "/v2/bots", {"name": "Events", "agent": "knowledge-scout"})[2]
    path = url + "/v2/bots/" + bot["bot_id"]
    event = {"event_id": "push-123", "source": "github", "text": "A review is ready"}
    status, _, receipt = http("POST", path + "/events", event)
    assert status == 202 and receipt["duplicate"] is False
    duplicate = http("POST", path + "/events", event)[2]
    assert duplicate["duplicate"] is True and duplicate["run_id"] == receipt["run_id"]
    assert service.bots.tick() == 0
    assert http("POST", path + "/events", {**event, "text": "changed"})[0] == 409
    assert http("POST", path.replace("/v2", "") + "/events", event)[0] == 404
    assert http("POST", path + "/resume")[0] == 200
    service.bots.tick()
    result = wait_for(lambda: (lambda r: r if r["status"] == "done" else None)(http("GET", url + "/v2/runs/" + receipt["run_id"])[2]))
    assert result["proof"]["verdict"] == "PASS"
    assert result["steps"][-1]["data"]["memory_receipt"]["provider"] == "local"
    assert result["budget_accounting"]["state"] == "settled"


def test_event_does_not_accept_authority_in_payload(api_v2):
    _, url = api_v2
    bot = http("POST", url + "/v2/bots", {"name": "Events", "agent": "knowledge-scout"})[2]
    status, _, body = http(
        "POST",
        url + "/v2/bots/" + bot["bot_id"] + "/events",
        {"event_id": "a", "source": "github", "text": "x", "lease": {"scopes": ["*"]}},
    )
    assert status == 400 and body["error"] == "invalid"


def test_v1_cannot_change_bot_with_a_v2_interval_missed_run_policy(api_v2):
    _, url = api_v2
    payload = {"name": "Skip missed", "agent": "knowledge-scout", "schedule": {"every_s": 60, "missed": "skip"}}
    assert http("POST", url + "/bots", payload)[0] == 400
    status, _, bot = http("POST", url + "/v2/bots", payload)
    assert status == 201
    path = "/bots/" + bot["bot_id"]
    assert http("GET", url + path)[0] == 409
    assert http("POST", url + path + "/resume")[0] == 409
    assert http("GET", url + "/v2" + path)[2]["status"] == "paused"


def test_event_provenance_is_versioned_without_changing_v1_messages(api_v2):
    _, url = api_v2
    bot = http("POST", url + "/v2/bots", {"name": "Event provenance", "agent": "knowledge-scout"})[2]
    path = "/bots/" + bot["bot_id"]
    event = {"event_id": "push-1", "source": "github", "text": "Review this change"}
    assert http("POST", url + "/v2" + path + "/events", event)[0] == 202
    v2 = http("GET", url + "/v2" + path + "/messages")[2]["messages"]
    v1 = http("GET", url + path + "/messages")[2]["messages"]
    assert v2[0]["event"] == {"event_id": "push-1", "source": "github", "untrusted": True}
    assert v1 == [{key: value for key, value in v2[0].items() if key != "event"}]


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "attacker.example"},
        {"Host": "127.0.0.1.attacker.example"},
        {"Origin": "https://attacker.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_loopback_api_rejects_browser_origins_and_rebinding_before_mutation(api_v2, headers):
    service, url = api_v2
    request = urllib.request.Request(
        url + "/v2/bots",
        method="POST",
        data=b'{"name":"injected","agent":"knowledge-scout"}',
        headers={"Content-Type": "application/json", **headers},
    )
    with pytest.raises(urllib.error.HTTPError) as denied:
        urllib.request.urlopen(request)
    assert denied.value.code == 403
    assert service.bots.list_bots()["bots"] == []


def test_api_rejects_simple_form_content_type(api_v2):
    service, url = api_v2
    request = urllib.request.Request(
        url + "/v2/bots", method="POST", data=b'{"name":"injected","agent":"knowledge-scout"}', headers={"Content-Type": "text/plain"}
    )
    with pytest.raises(urllib.error.HTTPError) as denied:
        urllib.request.urlopen(request)
    assert denied.value.code == 415 and service.bots.list_bots()["bots"] == []
