from __future__ import annotations

from datetime import datetime

import pytest

from echo_adk.api.service import AdkService
from echo_adk.bots.schedules import ScheduleError, initial_occurrence, next_occurrence, utc_iso, validate_schedule
from tests.fakes import FakeBroker, ScriptedModel, final_action


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def test_monthly_and_yearly_dates_skip_missing_days_including_century_leap_gap():
    monthly = validate_schedule({"at": "09:00", "tz": "Asia/Kathmandu", "monthdays": [31]})
    assert utc_iso(next_occurrence(monthly, stamp("2026-01-31T04:00:00Z"))) == "2026-03-31T03:15:00Z"
    yearly = validate_schedule({"at": "09:00", "tz": "UTC", "monthdays": [29], "months": [2]})
    assert utc_iso(next_occurrence(yearly, stamp("2096-02-29T09:00:00Z"))) == "2104-02-29T09:00:00Z"


def test_one_time_preserves_original_missed_deadline_and_uses_first_fold():
    once = validate_schedule({"once_at": "2026-11-01T01:30", "tz": "America/Chicago", "missed": "once"})
    first = stamp("2026-11-01T06:30:00Z")
    assert initial_occurrence(once, first + 3600) == first
    assert next_occurrence(once, first - 1) == first
    assert next_occurrence(once, first) is None
    assert next_occurrence(once, first + 3600) is None
    with pytest.raises(ScheduleError, match="nonexistent"):
        validate_schedule({"once_at": "2026-03-08T02:30", "tz": "America/Chicago"})


def test_numeric_cron_supports_lists_ranges_steps_and_documented_date_or_rule():
    schedule = validate_schedule({"cron": "0,15,30,45 9-17/2 * * 1-5", "tz": "UTC"})
    assert utc_iso(next_occurrence(schedule, stamp("2026-11-06T17:45:00Z"))) == "2026-11-09T09:00:00Z"
    # February has no 31st, but the restricted Monday field is ORed with DOM.
    date_or = validate_schedule({"cron": "0 9 31 2 1", "tz": "UTC"})
    assert utc_iso(next_occurrence(date_or, stamp("2026-02-01T00:00:00Z"))) == "2026-02-02T09:00:00Z"
    sunday = validate_schedule({"cron": "*/10 * * * 7", "tz": "UTC"})
    assert utc_iso(next_occurrence(sunday, stamp("2026-11-08T09:10:00Z"))) == "2026-11-08T09:20:00Z"


def test_cron_skips_spring_gap_and_repeated_second_occurrence():
    spring = validate_schedule({"cron": "30 2 * * *", "tz": "America/Chicago"})
    assert utc_iso(next_occurrence(spring, stamp("2026-03-08T07:59:00Z"))) == "2026-03-09T07:30:00Z"
    autumn = validate_schedule({"cron": "30 1 * * *", "tz": "America/Chicago"})
    assert utc_iso(next_occurrence(autumn, stamp("2026-11-01T06:00:00Z"))) == "2026-11-01T06:30:00Z"
    assert utc_iso(next_occurrence(autumn, stamp("2026-11-01T07:00:00Z"))) == "2026-11-02T07:30:00Z"


@pytest.mark.parametrize(
    "value",
    [
        {"cron": "@daily", "tz": "UTC"},
        {"cron": "0 0 * * * command", "tz": "UTC"},
        {"cron": "60 0 * * *", "tz": "UTC"},
        {"cron": "*/0 * * * *", "tz": "UTC"},
        {"cron": "0 0 31 2 *", "tz": "UTC"},
        {"cron": "0 0 * * MON", "tz": "UTC"},
        {"cron": "0 0 * * 5-1", "tz": "UTC"},
        {"cron": "*" * 257, "tz": "UTC"},
        {"cron": "0 0 * * *", "tz": "UTC", "weekdays": [1]},
        {"at": "09:00", "tz": "UTC", "monthdays": [31], "months": [2]},
        {"at": "09:00", "tz": "UTC", "monthdays": [True]},
        {"at": "09:00", "tz": "UTC", "monthdays": [1], "months": [0]},
        {"once_at": "2026-11-08T09:00Z", "tz": "UTC"},
        {"once_at": "2026-02-29T09:00", "tz": "UTC"},
        {"once_at": "2026-11-08T09:00", "tz": "UTC", "every_s": 60},
    ],
)
def test_extended_schedule_rejects_ambiguous_unbounded_or_executable_input(value):
    with pytest.raises(ScheduleError):
        validate_schedule(value)


def test_completed_one_shot_restores_original_queued_id_then_never_admits_again(agents_copy, tmp_path, monkeypatch):
    model = ScriptedModel([final_action("must not run")])

    def service():
        result = AdkService(agents_copy, tmp_path / "state", model_factory=lambda _: model, broker=FakeBroker(), start_scheduler=False)
        result.bots._clock = lambda: stamp("2026-11-08T10:00:00Z")
        return result

    first = service()
    submitted = []
    monkeypatch.setattr(first, "submit_run", submitted.append)
    bot = first.create_bot(
        {"name": "once", "agent": "forge-steward", "schedule": {"once_at": "2026-11-08T09:00", "tz": "UTC", "missed": "once"}}
    )
    first.bots.resume(bot["bot_id"])
    assert first.bots.tick() == 1
    run_id = submitted[0]
    assert first.bots.get_bot(bot["bot_id"])["schedule_complete"] is True
    first.close()
    second = service()
    resumed = []
    monkeypatch.setattr(second, "submit_run", resumed.append)
    try:
        assert second.bots.tick() == 1 and resumed == [run_id]
        assert len(second.store.list_runs(bot_id=bot["bot_id"])) == 1
        second.refuse_run(run_id, "cancelled", "test stops unstarted work")
        assert second.bots.tick(now=stamp("2027-11-08T10:00:00Z")) == 0
        assert model.calls == 0
    finally:
        second.close()


def test_missed_skip_exhausts_and_explicit_schedule_update_rearms(agents_copy, tmp_path):
    service = AdkService(agents_copy, tmp_path / "state", broker=FakeBroker(), start_scheduler=False)
    service.bots._clock = lambda: stamp("2026-11-08T10:00:00Z")
    try:
        bot = service.create_bot({"name": "skip", "agent": "forge-steward", "schedule": {"once_at": "2026-11-08T09:00", "tz": "UTC"}})
        service.bots.resume(bot["bot_id"])
        assert service.bots.tick() == 0 and service.bots.get_bot(bot["bot_id"])["schedule_complete"] is True
        service.bots.pause(bot["bot_id"])
        updated = service.bots.update_bot(bot["bot_id"], {"schedule": {"once_at": "2026-11-09T09:00", "tz": "UTC"}})
        assert updated["schedule_complete"] is False and updated["next_run_at"] == "2026-11-09T09:00:00Z"
        interval = service.bots.update_bot(bot["bot_id"], {"schedule": {"every_s": 60}})
        assert "schedule_complete" not in interval and interval["next_run_at"] == "2026-11-08T10:01:00Z"
    finally:
        service.close()


def test_one_shot_budget_refusal_does_not_consume_occurrence(agents_copy, tmp_path, monkeypatch):
    service = AdkService(agents_copy, tmp_path / "state", broker=FakeBroker(), daily_cap_usd=0, start_scheduler=False)
    service.bots._clock = lambda: stamp("2026-11-08T10:00:00Z")
    submitted = []
    monkeypatch.setattr(service, "submit_run", submitted.append)
    try:
        bot = service.create_bot(
            {"name": "budget", "agent": "paid-steward", "schedule": {"once_at": "2026-11-08T09:00", "tz": "UTC", "missed": "once"}}
        )
        service.bots.resume(bot["bot_id"])
        assert service.bots.tick() == 0
        refused = service.bots.get_bot(bot["bot_id"])
        assert refused["status"] == "error" and refused["schedule_complete"] is False
        assert service.store.list_runs(bot_id=bot["bot_id"]) == []
        service.daily_cap_usd = 2
        service.bots.resume(bot["bot_id"])
        assert service.bots.tick() == 1 and len(submitted) == 1
        assert service.bots.get_bot(bot["bot_id"])["schedule_complete"] is True
    finally:
        service.close()
