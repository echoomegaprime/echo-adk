from __future__ import annotations

from datetime import datetime

import pytest

from echo_adk.bots.schedules import ScheduleError, next_occurrence, should_run, utc_iso, validate_schedule


def stamp(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def calendar(at="09:00", *, days=None, missed="skip", tz="America/Chicago"):
    return validate_schedule({"at": at, "tz": tz, "weekdays": days or [1, 2, 3, 4, 5, 6, 7], "missed": missed})


def test_calendar_follows_local_weekdays_and_offset():
    schedule = calendar(days=[1, 2, 3, 4, 5])
    # Friday after its morning slot goes to Monday, at 09:00 Chicago.
    assert utc_iso(next_occurrence(schedule, stamp("2026-11-06T16:00:00Z"))) == "2026-11-09T15:00:00Z"
    kathmandu = calendar(tz="Asia/Kathmandu")
    assert utc_iso(next_occurrence(kathmandu, stamp("2026-11-07T00:00:00Z"))) == "2026-11-07T03:15:00Z"


def test_spring_gap_skips_nonexistent_time_instead_of_moving_work():
    schedule = calendar("02:30")
    assert utc_iso(next_occurrence(schedule, stamp("2026-03-08T07:59:00Z"))) == "2026-03-09T07:30:00Z"
    # A weekly Sunday job skips the whole missing occurrence, not its weekday rule.
    sunday = calendar("02:30", days=[7])
    assert utc_iso(next_occurrence(sunday, stamp("2026-03-08T07:59:00Z"))) == "2026-03-15T07:30:00Z"


def test_autumn_fold_runs_once_at_the_first_local_occurrence():
    schedule = calendar("01:30")
    first = next_occurrence(schedule, stamp("2026-11-01T06:00:00Z"))
    assert utc_iso(first) == "2026-11-01T06:30:00Z"
    assert utc_iso(next_occurrence(schedule, first)) == "2026-11-02T07:30:00Z"
    # A restart during the repeated hour must not create the second 01:30.
    assert utc_iso(next_occurrence(schedule, stamp("2026-11-01T07:00:00Z"))) == "2026-11-02T07:30:00Z"


def test_legacy_interval_shape_and_coalesced_missed_work_are_preserved():
    assert validate_schedule({"every_s": 60}) == {"every_s": 60}
    assert should_run({"every_s": 60}, 1000, 100000)
    assert next_occurrence({"every_s": 60}, 100000) == 100060
    assert should_run(calendar(), 1000, 1060)
    assert not should_run(calendar(), 1000, 1061)
    assert should_run(calendar(missed="once"), 1000, 100000)
    assert not should_run(calendar(missed="once"), 1000, 999)


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"every_s": True},
        {"every_s": 59},
        {"every_s": 604801},
        {"every_s": 60, "missed": None},
        {"every_s": 60, "at": "09:00"},
        {"at": "24:00", "tz": "UTC", "weekdays": [1]},
        {"at": "9:00", "tz": "UTC", "weekdays": [1]},
        {"at": "09:00", "tz": "Mars/Somewhere", "weekdays": [1]},
        {"at": "09:00", "tz": "../UTC", "weekdays": [1]},
        {"at": "09:00", "tz": "UTC", "weekdays": []},
        {"at": "09:00", "tz": "UTC", "weekdays": [0]},
        {"at": "09:00", "tz": "UTC", "weekdays": [True]},
        {"at": "09:00", "tz": "UTC", "weekdays": [1, 1]},
        {"at": "09:00", "tz": "UTC", "weekdays": [1], "missed": "all"},
        {"at": "09:00", "tz": "UTC", "weekdays": [1], "grant_ref": "untrusted"},
    ],
)
def test_invalid_schedule_is_rejected_without_timezone_or_permission_fallback(value):
    with pytest.raises(ScheduleError):
        validate_schedule(value)
