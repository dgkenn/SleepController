"""Learn the user's awakening phenotype — the ML-tuned half of sleep maintenance.

The proactive wake-risk assessor uses general precursors (HR creep, restlessness, running
warm); this module learns the *personal* part from the logged history of when and at what
bed temperature THIS user actually wakes:

  - recurring awakening clock-times (clusters, e.g. a 3 a.m. wake) -> the controller
    pre-emptively cools a little before those windows on later nights
  - the bed temperature at which awakenings cluster -> a personal "too warm to stay asleep"
    threshold the assessor watches for

Built from ``raw_samples`` (per-sample ``wake_event`` + ``bed_temp_f`` + ``timestamp``).
Returns a ``WakeProfile`` the controller consumes. Conservative: needs repeated evidence
before asserting a recurring time, so a single odd night can't create a phantom pattern.
"""

from __future__ import annotations

from datetime import datetime
from typing import List

from sleepctl.controller.wake_risk import WakeProfile


def _parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def build_wake_profile(
    repo,
    lookback_samples: int = 4000,
    min_cluster_nights: int = 2,
    bin_min: int = 30,
) -> WakeProfile:
    """Construct the awakening profile: start from the evidence-backed preset and overlay
    whatever the user's own history has revealed (recurring times + personal warm
    threshold). The structural cycle/circadian vulnerabilities from the preset are always
    retained; the personal clock-times and threshold are learned on top."""
    preset = WakeProfile.evidence_default()
    try:
        rows = repo.conn.execute(
            "SELECT ts, bed_temp_f FROM raw_samples WHERE wake_event = 1 "
            "ORDER BY id DESC LIMIT ?",
            (lookback_samples,),
        ).fetchall()
    except Exception:
        return preset

    times: List[int] = []
    temps: List[float] = []
    day_keys = {}  # bin -> set of dates (so we count distinct nights, not samples)
    for r in rows:
        ts = _parse_ts(r["ts"] if hasattr(r, "keys") else r[0])
        if ts is None:
            continue
        minute = ts.hour * 60 + ts.minute
        times.append(minute)
        b = (r["bed_temp_f"] if hasattr(r, "keys") else r[1])
        if b is not None:
            temps.append(float(b))
        # attribute to a "night" (shift early-morning hours onto the prior date)
        night = ts.date().isoformat() if ts.hour >= 18 else \
            ts.date().isoformat() + "-am"
        binc = (minute // bin_min)
        day_keys.setdefault(binc, set()).add(night)

    # Recurring awakening times = bins seen on >= min_cluster_nights distinct nights.
    recurring: List[int] = []
    for binc, nights in day_keys.items():
        if len(nights) >= min_cluster_nights:
            recurring.append(int(binc * bin_min + bin_min / 2))
    recurring.sort()

    # Personal warm threshold: the lower quartile of bed temps at awakenings (i.e. even at
    # the cooler awakenings the bed was at/above this) -> watch for it.
    warm_threshold = None
    if len(temps) >= 5:
        temps_sorted = sorted(temps)
        q1 = temps_sorted[max(0, len(temps_sorted) // 4 - 1)]
        warm_threshold = round(q1, 1)

    learned_anything = bool(recurring) or warm_threshold is not None
    return WakeProfile(
        awakening_minutes=recurring,
        warm_temp_threshold_f=warm_threshold,
        # keep the preset's structural (cycle/circadian) vulnerabilities
        cycle_len_min=preset.cycle_len_min,
        cycle_boundary_window_min=preset.cycle_boundary_window_min,
        back_half_after_cycle=preset.back_half_after_cycle,
        circadian_window=preset.circadian_window,
        source="blended" if learned_anything else "preset",
    )
