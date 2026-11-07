"""Bounded interval and local-calendar schedules, with explicit daylight-saving behavior.

Calendar weekdays use ISO numbering (Monday=1, Sunday=7). A wall-clock time that
does not exist during a spring transition is skipped. A repeated autumn time
runs at its first occurrence only. All persisted deadlines are UTC timestamps.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core.types import ValidationError

MIN_SCHEDULE_S = 60
MAX_SCHEDULE_S = 7 * 24 * 3600
MISFIRE_GRACE_S = 60
_WALL_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")


class ScheduleError(ValidationError):
    """A calendar or interval specification cannot be honored safely."""


def validate_schedule(value: Any) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ScheduleError("schedule must be an interval or calendar object")
    policy = value.get("missed")
    if "missed" in value and policy not in ("skip", "once"):
        raise ScheduleError("schedule.missed must be skip or once")
    if "every_s" in value:
        if set(value) - {"every_s", "missed"}:
            raise ScheduleError("interval schedule accepts every_s and optional missed only")
        every = value["every_s"]
        if isinstance(every, bool) or not isinstance(every, int) or not MIN_SCHEDULE_S <= every <= MAX_SCHEDULE_S:
            raise ScheduleError(f"schedule.every_s must be an integer between {MIN_SCHEDULE_S} and {MAX_SCHEDULE_S}")
        result = {"every_s": every}
        if "missed" in value:
            result["missed"] = policy
        return result
    if set(value) - {"at", "tz", "weekdays", "missed"} or not {"at", "tz", "weekdays"} <= set(value):
        raise ScheduleError("calendar schedule requires at, tz, weekdays, and optional missed")
    at, tz, weekdays = value["at"], value["tz"], value["weekdays"]
    if not isinstance(at, str) or _WALL_TIME.fullmatch(at) is None:
        raise ScheduleError("schedule.at must be a 24-hour HH:MM time")
    if not isinstance(tz, str) or not tz or len(tz) > 100:
        raise ScheduleError("schedule.tz must be an IANA timezone name")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError("schedule.tz is unknown or timezone data is unavailable") from None
    if (
        not isinstance(weekdays, list)
        or not 1 <= len(weekdays) <= 7
        or any(isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 7 for day in weekdays)
        or len(set(weekdays)) != len(weekdays)
    ):
        raise ScheduleError("schedule.weekdays must contain distinct ISO weekdays (Monday=1 through Sunday=7)")
    return {"at": at, "tz": tz, "weekdays": sorted(weekdays), "missed": policy or "skip"}


def missed_policy(schedule: Mapping[str, Any]) -> str:
    # Existing interval bots have always coalesced late work into one turn.
    return str(schedule.get("missed", "once" if "every_s" in schedule else "skip"))


def next_occurrence(schedule: Mapping[str, Any], after: float) -> float:
    """Return the first valid occurrence strictly after ``after``.

    An interval is measured from dispatch, so a late tick cannot produce a burst
    of short-spaced runs. Calendar schedules remain anchored to local wall time.
    """
    if "every_s" in schedule:
        return float(after) + int(schedule["every_s"])
    zone = ZoneInfo(str(schedule["tz"]))
    local_day = datetime.fromtimestamp(after, zone).date()
    hour, minute = (int(part) for part in str(schedule["at"]).split(":"))
    days = set(schedule["weekdays"])
    # Two weeks covers a selected weekday whose only occurrence this week is
    # absent at a DST transition; the bound prevents accidental unbounded work.
    for offset in range(15):
        day = local_day + timedelta(days=offset)
        if day.isoweekday() not in days:
            continue
        naive = datetime.combine(day, time(hour, minute))
        candidate = naive.replace(tzinfo=zone, fold=0)
        timestamp = candidate.timestamp()
        roundtrip = datetime.fromtimestamp(timestamp, zone)
        if roundtrip.replace(tzinfo=None) != naive or roundtrip.fold != 0:
            continue
        if timestamp > after:
            return timestamp
    raise ScheduleError("no valid calendar occurrence in the next two weeks")


def should_run(schedule: Mapping[str, Any], due: float, current: float) -> bool:
    """Tolerate one minute of scheduler jitter, then apply the missed-run policy."""
    return current >= due and (current - due <= MISFIRE_GRACE_S or missed_policy(schedule) == "once")


def utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
