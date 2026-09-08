"""Recover a mid-night controller state from the samples a previous process recorded.

The daemon restarts itself on every deploy, and the controller's night lives in process
memory: bed entry, sleep onset, the state machine, the accrued architecture. Each of those has
grown its own recovery as its loss was measured (bed entry 2026-08-27, the abandoned-session
clock 2026-08-25). This is the one for the STATE: without it a restart re-runs the induction
cascade on a user who is already asleep. Pure SQL over ``raw_samples`` so it is testable
without a daemon."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sleepctl.models import SleepStage

#: A last sample older than this is a previous night's, not a live session to resume.
MAX_AGE_MIN = 15.0
#: Same cap the live accumulator uses so a stale tick cannot inflate a bucket.
_MAX_STEP_MIN = 10.0
_PAST_ONSET = ("maintenance", "wake_recovery", "wake_window")


def _parse(ts) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts))
    except Exception:
        return None


def recover_session_state(conn, night_date: str, now: datetime) -> Optional[dict]:
    """``{"state", "onset_ts", "architecture"}`` when the most recent sample of ``night_date``
    is fresh and past sleep onset, else None.

    ``onset_ts`` is the first MAINTENANCE sample of the night (the state machine enters it on
    the tick onset is confirmed). ``architecture`` re-derives the deep/REM/light minutes
    accrued since then from the recorded stages, with the same per-step cap as the live
    accumulator."""
    row = conn.execute(
        "SELECT ts, controller_state FROM raw_samples WHERE night_date = ? "
        "AND controller_state IS NOT NULL ORDER BY id DESC LIMIT 1", (night_date,)).fetchone()
    if not row:
        return None
    last_ts, state = _parse(row[0]), row[1]
    if last_ts is None or state not in _PAST_ONSET:
        return None
    if (now - last_ts).total_seconds() / 60.0 > MAX_AGE_MIN:
        return None
    onset_row = conn.execute(
        "SELECT MIN(ts) FROM raw_samples WHERE night_date = ? AND controller_state = 'maintenance'",
        (night_date,)).fetchone()
    onset_ts = _parse(onset_row[0]) if onset_row and onset_row[0] else None
    if onset_ts is None:
        return None
    arch = {"deep_min": 0.0, "rem_min": 0.0, "light_min": 0.0}
    prev_ts = None
    for ts, stage in conn.execute(
            "SELECT ts, stage FROM raw_samples WHERE night_date = ? AND ts >= ? ORDER BY id ASC",
            (night_date, onset_ts.isoformat())).fetchall():
        t = _parse(ts)
        if t is None:
            continue
        if prev_ts is not None:
            dt = (t - prev_ts).total_seconds() / 60.0
            if 0.0 < dt <= _MAX_STEP_MIN:
                if stage == SleepStage.DEEP.value:
                    arch["deep_min"] += dt
                elif stage == SleepStage.REM.value:
                    arch["rem_min"] += dt
                elif stage == SleepStage.LIGHT.value:
                    arch["light_min"] += dt
        prev_ts = t
    return {"state": state, "onset_ts": onset_ts, "architecture": arch}
