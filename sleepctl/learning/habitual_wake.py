"""Your usual wake time, learned from your own nights -- so the wake works with no alarm set up.

The smart wake needs a wake time. It normally arrives from the phone (the sleep-schedule
automation, ``app.automation``) or the work calendar. When neither has armed one by the evening,
this supplies the time you usually get up: the median final-wake time of your recent nights from
Apple's hypnogram (``health_sleep``), weekdays and weekends kept apart because they differ.

It is armed WITHOUT vibration: a learned guess may warm the bed toward your usual time, but it
must never buzz you awake on a morning you meant to sleep in. Your own alarm stays in charge,
and any real alarm (phone automation, the dashboard, the calendar) replaces it.
"""

from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sleepctl.learning.wearable_consensus import _iso_unix, source_kind

MIN_NIGHTS = 3            # same day-type nights needed before a usual time is trusted
LOOKBACK_DAYS = 28
EARLIEST, LATEST = 4 * 60, 12 * 60       # a plausible morning wake, minutes after midnight


def _final_wakes(conn, since: datetime) -> list:
    """Local datetimes of each night's final wake (end of the last Apple sleep segment)."""
    try:
        rows = conn.execute(
            "SELECT start, end, stage, source FROM health_sleep WHERE end >= ?",
            (since.astimezone(timezone.utc).isoformat(),)).fetchall()
    except Exception:
        return []
    by_day: dict = {}
    for st, en, stage, src in rows:
        if source_kind(src) != "apple" or stage not in ("core", "deep", "rem", "asleep"):
            continue
        t = _iso_unix(en)
        if t is None:
            continue
        local = datetime.fromtimestamp(t)            # naive local wall clock
        # a wake after noon belongs to a nap or a day sleep, not the morning
        if not (EARLIEST <= local.hour * 60 + local.minute <= LATEST):
            continue
        d = local.date()
        if d not in by_day or local > by_day[d]:
            by_day[d] = local
    return sorted(by_day.values())


def usual_wake(conn, morning: date, now: Optional[datetime] = None) -> Optional[str]:
    """``"HH:MM"`` you usually wake on mornings like ``morning`` (weekday vs weekend), or None."""
    now = now or datetime.now()
    since = (now - timedelta(days=LOOKBACK_DAYS)).astimezone()
    weekend = morning.weekday() >= 5
    mins = [w.hour * 60 + w.minute for w in _final_wakes(conn, since)
            if (w.weekday() >= 5) == weekend]
    if len(mins) < MIN_NIGHTS:
        return None
    m = int(round(statistics.median(mins)))
    return f"{m // 60:02d}:{m % 60:02d}"


__all__ = ["usual_wake", "MIN_NIGHTS"]
