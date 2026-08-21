"""Time parsing for tool arguments.

Models are much better at "-30m" than at RFC3339, and an incident question is
almost always relative to now. Both are accepted; everything downstream works
in aware UTC datetimes.

Ranges are clamped. A model that asks for 90 days of logs at 1-second
resolution is not malicious, it is just optimistic, and the backend it would
knock over is the one the on-call engineer is also using.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

MAX_RANGE = timedelta(days=7)
MIN_STEP = timedelta(seconds=5)
MAX_POINTS = 1_000

_REL = re.compile(r"^([+-]?)(\d+)([smhdw])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


class TimeError(ValueError):
    pass


def parse_time(value: str | None, *, default: datetime | None = None) -> datetime:
    if value is None or value == "":
        if default is None:
            raise TimeError("missing time value")
        return default
    text = value.strip()
    if text in ("now", "0"):
        return datetime.now(UTC)

    match = _REL.match(text)
    if match:
        sign, amount, unit = match.groups()
        delta = timedelta(**{_UNITS[unit]: int(amount)})
        return datetime.now(UTC) + (delta if sign == "+" else -delta)

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TimeError(
            f"cannot parse time {value!r}; use RFC3339 (2026-07-25T03:00:00Z) "
            "or a relative offset like -30m"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_range(
    start: str | None, end: str | None, *, default_window: timedelta = timedelta(hours=1)
) -> tuple[datetime, datetime]:
    end_dt = parse_time(end, default=datetime.now(UTC))
    start_dt = parse_time(start, default=end_dt - default_window)
    if start_dt >= end_dt:
        raise TimeError("start must be before end")
    if end_dt - start_dt > MAX_RANGE:
        # Clamp rather than reject: the agent gets usable data and a note,
        # instead of an error it will try to work around by looping.
        start_dt = end_dt - MAX_RANGE
    return start_dt, end_dt


def resolve_step(start: datetime, end: datetime, step: str | None) -> str:
    """Pick a step that keeps the series under MAX_POINTS."""
    span = end - start
    if step:
        match = _REL.match(step.strip())
        if not match:
            raise TimeError(f"cannot parse step {step!r}; use e.g. 30s, 5m, 1h")
        requested = timedelta(**{_UNITS[match.group(3)]: int(match.group(2))})
    else:
        requested = span / 120

    floor = max(MIN_STEP, span / MAX_POINTS)
    chosen = max(requested, floor)
    seconds = max(1, int(chosen.total_seconds()))
    return f"{seconds}s"


def _self_check() -> None:
    now = datetime.now(UTC)
    assert abs((parse_time("-30m") - (now - timedelta(minutes=30))).total_seconds()) < 2
    assert parse_time("2026-07-25T03:00:00Z").hour == 3
    assert parse_time("2026-07-25T03:00:00Z").tzinfo is not None

    start, end = parse_range("-2h", None)
    assert timedelta(hours=1, minutes=59) < end - start < timedelta(hours=2, minutes=1)

    # a 90-day ask gets clamped, not rejected
    start, end = parse_range("-90d", None)
    assert end - start == MAX_RANGE

    # step floor keeps the point count sane over a long window
    step = resolve_step(now - timedelta(days=7), now, "1s")
    assert int(step[:-1]) >= int(MAX_RANGE.total_seconds() // MAX_POINTS)
    assert resolve_step(now - timedelta(hours=1), now, "5m") == "300s"

    for bad in ("yesterday", "3 hours ago", ""):
        try:
            parse_time(bad)
            raise AssertionError(f"{bad!r} should not parse")
        except TimeError:
            pass
    print("timeparse self-check ok")


if __name__ == "__main__":
    _self_check()
