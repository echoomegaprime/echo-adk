# Bot schedules

The schedule parser is `echo_adk/bots/schedules.py`; durable admission and dispatch
are in `echo_adk/bots/manager.py`. Schedules queue an ordinary governed bot turn.
They do not supply a capability lease, bypass budget admission, or resume a paused
bot. Use the version 2 API for all calendar forms and explicit missed policies.
The frozen version 1 response refuses schedules with fields beyond `every_s`
with an upgrade-required HTTP 409.

## Supported definitions

Each object may contain `missed: "skip"` or `missed: "once"`.

| Form | Example | Meaning |
| --- | --- | --- |
| Interval | `{"every_s":600}` | Every 600 seconds from dispatch; bounds are 60 through 604800 seconds. |
| One time | `{"once_at":"2026-12-01T09:00","tz":"America/Chicago"}` | One local date and minute. No offset or seconds in `once_at`. |
| Weekly | `{"at":"09:00","tz":"Europe/London","weekdays":[1,2,3,4,5]}` | Selected ISO weekdays: Monday 1 through Sunday 7. |
| Monthly | `{"at":"09:00","tz":"UTC","monthdays":[1,15]}` | Selected dates in every month. |
| Yearly | `{"at":"09:00","tz":"UTC","monthdays":[29],"months":[2]}` | Selected dates in selected months; this example runs on leap days. |
| Cron | `{"cron":"0,30 9-17/2 * * 1-5","tz":"America/Chicago"}` | Numeric minute, hour, monthday, month, weekday fields. |

Unknown fields, mixed forms, repeated list values, impossible dates, invalid
timezones and executable cron text are rejected. Monthly and yearly date lists
select all valid combinations. February never inherits a requested 31st from
another month: nonexistent dates are skipped, not clamped to a month end.

## Timezones, DST and bounded cron

Calendar forms require an installed IANA timezone database. Systems without one,
including typical Windows Python installations, use the project's optional
`calendar` extra. An unavailable timezone is an error; there is no UTC fallback.

A recurring local time in a spring gap is skipped. A repeated local time in an
autumn fold uses only its first occurrence, including after a restart during the
second occurrence. A nonexistent **one-time** local time is rejected because
there would be no future occurrence to select. A repeated one-time local time
also uses its first occurrence. `once_at` supports years 1970 through 9998.

Cron is limited to 256 characters and five numeric fields. It supports `*`, lists,
ascending ranges and positive steps; minute is 0–59, hour 0–23, monthday 1–31,
month 1–12 and weekday 0–7 (0 and 7 both mean Sunday). There are no seconds, year
field, named months/weekdays, macros, commands, `L`, `W` or `#` operators. When
both monthday and weekday are restricted, either may match. Only a literal `*`
counts as unrestricted for that rule; for example `*/1` is a restricted field in
this supported subset. Calendar lookup searches at most eight years plus a small
boundary allowance; weekly lookup searches fifteen days. This includes the
eight-year leap-day gap around a non-leap century, without unbounded searching.

## Missed work and restart behavior

Legacy intervals default to `missed: "once"`. Calendar forms default to `skip`.
An active scheduler tolerates at most sixty seconds of lateness before applying
that policy. `once` coalesces missed occurrences into one turn, without replaying
every missed interval. `skip` advances to the next future occurrence. Resuming a
paused `skip` bot advances past its earlier deadline, even within that active
scheduler jitter allowance.

A one-time schedule keeps its original UTC deadline, including if created after
the requested time. Admission or deliberate skipping persists
`schedule_complete: true`; the response then omits `next_run_at`. A failed budget
admission leaves the occurrence unconsumed. Once admitted, a failed or cancelled
turn does not implicitly schedule a replacement. The owner can explicitly edit
the paused, drained bot to assign a new schedule.

The queued run and following deadline are committed together. If the worker
stops between admission and dispatch, restart uses the same queued run ID before
checking `schedule_complete`. Completed one-time schedules cannot reinitialize
from a missing deadline. No automatic replay of an interrupted running model or
external call is added by these schedule forms.

The compatibility and restart cases are exercised by
`tests/test_bot_schedules.py`, `tests/test_schedule_extensions.py` and
`tests/test_bot_automation.py`. Before downgrading to a worker without these
forms, pause and drain affected bots and explicitly remove or convert their
schedules. An older worker must not be relied on to interpret new definitions.
