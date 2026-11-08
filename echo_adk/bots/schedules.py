"""Bounded interval, one-time and local calendar schedules.

Calendar gaps are skipped, and repeated local times use their first occurrence.
Monthly dates that do not exist are skipped rather than moved to another date.
The numeric five-field cron subset has minute precision and no executable text.
"""

from __future__ import annotations

import calendar
import math
import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core.types import ValidationError

MIN_SCHEDULE_S = 60
MAX_SCHEDULE_S = 7 * 24 * 3600
MISFIRE_GRACE_S = 60
MAX_CRON_CHARS = 256
MAX_CALENDAR_DAYS = 366 * 8 + 2
_WALL_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")
_ONCE_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")
_CRON_TEXT = re.compile(r"[0-9*/, \-]+\Z")


class ScheduleError(ValidationError):
    """A calendar or interval specification cannot be honored safely."""


def _zone(value: Any) -> ZoneInfo:
    if not isinstance(value, str) or not value or len(value) > 100:
        raise ScheduleError("schedule.tz must be an IANA timezone name")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError("schedule.tz is unknown or timezone data is unavailable") from None


def _days(value: Any, field: str, maximum: int) -> list[int]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= maximum
        or any(isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= maximum for day in value)
        or len(set(value)) != len(value)
    ):
        raise ScheduleError(f"schedule.{field} must contain distinct integers from 1 through {maximum}")
    return sorted(value)


def _wall_timestamp(naive: datetime, zone: ZoneInfo) -> Optional[float]:
    candidate = naive.replace(tzinfo=zone, fold=0)
    timestamp = candidate.timestamp()
    roundtrip = datetime.fromtimestamp(timestamp, zone)
    if roundtrip.replace(tzinfo=None) != naive or roundtrip.fold != 0:
        return None
    return timestamp


def _once_due(schedule: Mapping[str, Any]) -> float:
    value = schedule.get("once_at")
    if not isinstance(value, str) or _ONCE_TIME.fullmatch(value) is None:
        raise ScheduleError("schedule.once_at must be a local YYYY-MM-DDTHH:MM timestamp")
    try:
        naive = datetime.fromisoformat(value)
    except ValueError:
        raise ScheduleError("schedule.once_at is not a valid date") from None
    if not 1970 <= naive.year <= 9998:
        raise ScheduleError("schedule.once_at year must be between 1970 and 9998")
    timestamp = _wall_timestamp(naive, _zone(schedule.get("tz")))
    if timestamp is None:
        raise ScheduleError("schedule.once_at falls in a nonexistent local time")
    return timestamp


def _cron_field(field: str, minimum: int, maximum: int) -> set[int]:
    values: set[int] = set()
    items = field.split(",")
    if len(items) > maximum - minimum + 1:
        raise ScheduleError("cron field has too many list entries")
    for item in items:
        pieces = item.split("/")
        if len(pieces) > 2 or not pieces[0] or (len(pieces) == 2 and not pieces[1].isdigit()):
            raise ScheduleError("invalid cron step")
        step = int(pieces[1]) if len(pieces) == 2 else 1
        if not 1 <= step <= maximum - minimum + 1:
            raise ScheduleError("cron step is outside its field range")
        base = pieces[0]
        if base == "*":
            low, high = minimum, maximum
        elif "-" in base:
            bounds = base.split("-")
            if len(bounds) != 2 or not all(part.isdigit() for part in bounds):
                raise ScheduleError("invalid cron range")
            low, high = (int(part) for part in bounds)
        elif base.isdigit():
            low = int(base)
            high = maximum if len(pieces) == 2 else low
        else:
            raise ScheduleError("cron fields accept only numeric values, *, ranges, lists and steps")
        if not minimum <= low <= high <= maximum:
            raise ScheduleError("cron field is outside its allowed range")
        values.update(range(low, high + 1, step))
    return values


def _cron(value: Any) -> tuple[str, list[set[int]], bool, bool]:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_CRON_CHARS or _CRON_TEXT.fullmatch(value) is None:
        raise ScheduleError("schedule.cron must be at most 256 characters of numeric five-field cron")
    fields = value.split()
    if len(fields) != 5:
        raise ScheduleError("schedule.cron requires minute hour monthday month weekday")
    parsed = [
        _cron_field(field, low, high) for field, (low, high) in zip(fields, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7)), strict=True)
    ]
    parsed[4] = {day % 7 for day in parsed[4]}
    # Only a literal '*' is unrestricted in this explicit subset. If both date
    # fields are restricted they are ORed, as in traditional cron.
    if fields[4] == "*" and not any(day <= calendar.monthrange(2000, month)[1] for month in parsed[3] for day in parsed[2]):
        raise ScheduleError("cron has no possible calendar date")
    return " ".join(fields), parsed, fields[2] == "*", fields[4] == "*"


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
    if "once_at" in value:
        if set(value) - {"once_at", "tz", "missed"} or not {"once_at", "tz"} <= set(value):
            raise ScheduleError("one-time schedule requires once_at, tz and optional missed")
        _once_due(value)
        return {"once_at": value["once_at"], "tz": value["tz"], "missed": policy or "skip"}
    if "cron" in value:
        if set(value) - {"cron", "tz", "missed"} or "tz" not in value:
            raise ScheduleError("cron schedule requires cron, tz and optional missed")
        _zone(value["tz"])
        expression, _, _, _ = _cron(value["cron"])
        return {"cron": expression, "tz": value["tz"], "missed": policy or "skip"}
    if not {"at", "tz"} <= set(value):
        raise ScheduleError("calendar schedule requires at, tz and weekdays or monthdays")
    at = value["at"]
    if not isinstance(at, str) or _WALL_TIME.fullmatch(at) is None:
        raise ScheduleError("schedule.at must be a 24-hour HH:MM time")
    _zone(value["tz"])
    result = {"at": at, "tz": value["tz"], "missed": policy or "skip"}
    if "weekdays" in value:
        if set(value) - {"at", "tz", "weekdays", "missed"}:
            raise ScheduleError("weekly schedule accepts at, tz, weekdays and optional missed only")
        result["weekdays"] = _days(value["weekdays"], "weekdays", 7)
    elif "monthdays" in value:
        if set(value) - {"at", "tz", "monthdays", "months", "missed"}:
            raise ScheduleError("monthly schedule accepts at, tz, monthdays, optional months and missed only")
        result["monthdays"] = _days(value["monthdays"], "monthdays", 31)
        if "months" in value:
            result["months"] = _days(value["months"], "months", 12)
        if not any(
            day <= calendar.monthrange(2000, month)[1] for month in result.get("months", range(1, 13)) for day in result["monthdays"]
        ):
            raise ScheduleError("schedule has no possible calendar date")
    else:
        raise ScheduleError("calendar schedule requires weekdays or monthdays")
    return result


def missed_policy(schedule: Mapping[str, Any]) -> str:
    return str(schedule.get("missed", "once" if "every_s" in schedule else "skip"))


def _timestamp(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ScheduleError("schedule timestamp must be finite")
    return float(value)


def initial_occurrence(schedule: Mapping[str, Any], current: float) -> Optional[float]:
    """Preserve an original one-shot deadline, including when already missed."""
    schedule = validate_schedule(schedule)
    if schedule is None:
        return None
    current = _timestamp(current)
    if "once_at" in schedule:
        return _once_due(schedule)
    if "every_s" in schedule:
        return current
    return next_occurrence(schedule, current)


def next_occurrence(schedule: Mapping[str, Any], after: float) -> Optional[float]:
    """Return the first valid occurrence strictly after after, or an exhausted one-shot."""
    schedule = validate_schedule(schedule)
    if schedule is None:
        return None
    after = _timestamp(after)
    if "every_s" in schedule:
        return after + schedule["every_s"]
    if "once_at" in schedule:
        due = _once_due(schedule)
        return due if due > after else None
    zone = _zone(schedule["tz"])
    try:
        local = datetime.fromtimestamp(after, zone)
    except (OverflowError, OSError, ValueError):
        raise ScheduleError("schedule timestamp is outside the supported calendar") from None
    if "cron" in schedule:
        _, fields, any_monthday, any_weekday = _cron(schedule["cron"])
        minutes, hours, monthdays, months, weekdays = fields

        def selected(day: date) -> bool:
            if day.month not in months:
                return False
            dom, dow = day.day in monthdays, day.isoweekday() % 7 in weekdays
            return (dom and dow) if any_monthday or any_weekday else (dom or dow)
    else:
        hour, minute = (int(part) for part in schedule["at"].split(":"))
        hours, minutes = {hour}, {minute}

        def selected(day: date) -> bool:
            if "weekdays" in schedule:
                return day.isoweekday() in schedule["weekdays"]
            return day.day in schedule["monthdays"] and day.month in schedule.get("months", range(1, 13))

    limit = 15 if "weekdays" in schedule else MAX_CALENDAR_DAYS
    for offset in range(limit):
        try:
            day = local.date() + timedelta(days=offset)
        except OverflowError:
            break
        if not selected(day):
            continue
        for hour in sorted(hours):
            for minute in sorted(minutes):
                naive = datetime.combine(day, time(hour, minute))
                if offset == 0 and naive <= local.replace(tzinfo=None):
                    continue
                timestamp = _wall_timestamp(naive, zone)
                if timestamp is not None and timestamp > after:
                    return timestamp
    raise ScheduleError("no valid calendar occurrence within the bounded search window")


def should_run(schedule: Mapping[str, Any], due: float, current: float) -> bool:
    due, current = _timestamp(due), _timestamp(current)
    return current >= due and (current - due <= MISFIRE_GRACE_S or missed_policy(schedule) == "once")


def utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(_timestamp(timestamp), timezone.utc).isoformat().replace("+00:00", "Z")
