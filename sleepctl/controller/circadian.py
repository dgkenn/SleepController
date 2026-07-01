"""Circadian phase estimate — the dominant variable for a rotating-shift sleeper.

The rest of the stack reasons about *tonight*. This module reasons about *the clock*: from
recent nights' sleep midpoints it estimates the user's habitual sleep window, a current phase
estimate (how far that clock has actually drifted given very recent nights), and a "phase
shift" magnitude when the recent schedule has pulled away from habit (e.g. rotating onto
nights). It also derives the wake-maintenance zone (WMZ) — the ~2-3 h window before habitual
sleep onset when the circadian drive to stay awake is strongest and sleep is behaviorally
hardest to initiate (Lavie 1986; Strogatz/Kronauer's forbidden zone for sleep) — anchored to
this individual's own habitual midpoint rather than a textbook clock time.

Pure functions over ``NightSummary`` rows (from ``repo.recent_nights``); no device or I/O, so
this is fully unit-testable from synthetic sleep history. Degrades gracefully with few/no
nights: falls back to a wide-uncertainty estimate rather than raising or guessing wildly.

Method (deliberately simple + robust, not a full two-process/DLMO model):
  1. Convert each night's bedtime/wake_time (when both present) to a sleep MIDPOINT expressed as
     minutes-past-a-reference-midnight, folding late/after-midnight clock times onto a single
     continuous axis (same trick as ``sleep_plan.median_bedtime_clock``) so a habitual 03:00
     midpoint doesn't average against a 23:00 one incorrectly.
  2. The HABITUAL phase is the median midpoint over a longer lookback window (default 14 nights)
     — robust to a night or two of outliers (call shifts, travel).
  3. The RECENT phase is the median midpoint over a short lookback (default 3 nights) — where the
     clock actually is right now.
  4. The phase shift is recent-minus-habitual, wrapped to [-12h, +12h] (shortest signed
     direction), i.e. how many hours the recent schedule has dragged the sleep window from habit.
  5. Confidence scales with the number of usable nights (few nights -> low confidence + a wider
     reported uncertainty), and with the spread (MAD) of the habitual samples (a scattered
     schedule is a less reliable "habit").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Sequence


# Nights considered "recent" (where the clock is right now) vs the longer "habitual" baseline.
RECENT_WINDOW_NIGHTS = 3
HABITUAL_WINDOW_NIGHTS = 14
MIN_NIGHTS_FOR_ESTIMATE = 2

# Wake-maintenance zone: ends shortly before habitual sleep onset, spans ~2-3 h (Lavie 1986).
WMZ_END_BEFORE_SLEEP_MIN = 60     # WMZ ends ~1 h before habitual bedtime (drive relaxes near onset)
WMZ_DURATION_MIN = 150            # ~2.5 h window


def _clock_min(dt: datetime) -> int:
    """Minutes past midnight for a datetime."""
    return int(dt.hour) * 60 + int(dt.minute)


def _fmt_clock(minutes: float) -> str:
    m = int(round(minutes)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _fold_evening(minutes: int) -> int:
    """Fold an after-midnight clock time (e.g. 00:30 -> 24:30) onto a continuous evening axis,
    so bedtimes/midpoints straddling midnight average sanely instead of splitting bimodally."""
    return minutes + 1440 if minutes < 720 else minutes


def _median(vals: Sequence[float]) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _mad(vals: Sequence[float], med: float) -> float:
    """Median absolute deviation — robust spread measure (outlier nights don't blow it up)."""
    if not vals:
        return 0.0
    devs = sorted(abs(v - med) for v in vals)
    n = len(devs)
    return devs[n // 2] if n % 2 else (devs[n // 2 - 1] + devs[n // 2]) / 2.0


def _wrap_signed(delta_min: float) -> float:
    """Wrap a minutes delta to the shortest signed direction on a 24h clock, i.e. [-720, 720]."""
    d = delta_min % 1440
    if d > 720:
        d -= 1440
    return d


def _night_midpoint(summary) -> Optional[int]:
    """A night's sleep midpoint (folded minutes-past-midnight), from bedtime + wake_time when
    both are present, else from bedtime + total_sleep_min, else from wake_time alone as a last
    resort (assume ~8h opportunity). None if nothing usable."""
    bedtime = getattr(summary, "bedtime", None)
    wake = getattr(summary, "wake_time", None)
    tst = getattr(summary, "total_sleep_min", None)

    if bedtime is not None and wake is not None:
        b = _fold_evening(_clock_min(bedtime))
        w = _clock_min(wake)
        if w < 720:
            w += 1440
        if w <= b:  # guard against bad data (wake before bed on the folded axis)
            w += 1440
        return _fold_evening(int((b + w) / 2) % 1440)

    if bedtime is not None and tst:
        b = _fold_evening(_clock_min(bedtime))
        mid = b + tst / 2.0
        return _fold_evening(int(round(mid)) % 1440)

    if wake is not None and tst:
        w = _fold_evening(_clock_min(wake))
        mid = w - tst / 2.0
        return _fold_evening(int(round(mid)) % 1440)

    return None


@dataclass
class WakeMaintenanceZone:
    """The window when sleep is biologically resisted, anchored to the user's own habitual
    sleep-onset estimate rather than a generic clock time."""
    start_clock: str
    end_clock: str
    start_min: int   # minutes-past-midnight, folded axis (may be >= 1440)
    end_min: int

    def to_dict(self) -> dict:
        return {"start_clock": self.start_clock, "end_clock": self.end_clock}

    def contains(self, dt: datetime) -> bool:
        """Is ``dt`` inside the WMZ (folded-axis compare, handles the midnight wrap)."""
        m = _fold_evening(_clock_min(dt))
        lo, hi = self.start_min, self.end_min
        if lo <= hi:
            return lo <= m <= hi
        return m >= lo or m <= hi  # wrapped window


@dataclass
class CircadianEstimate:
    """The user's estimated circadian phase, from recent sleep history.

    ``habitual_midpoint_clock`` / ``habitual_sleep_window`` describe the entrained baseline;
    ``recent_midpoint_clock`` is where the clock actually is right now; ``phase_shift_hours`` is
    signed (positive = recent sleep later than habit, e.g. after a run of night shifts).
    ``confidence`` in [0, 1] reflects both sample size and schedule regularity.
    """
    n_nights_habitual: int
    n_nights_recent: int
    habitual_midpoint_clock: Optional[str]
    habitual_sleep_start_clock: Optional[str]
    habitual_sleep_end_clock: Optional[str]
    recent_midpoint_clock: Optional[str]
    phase_shift_hours: Optional[float]
    confidence: float
    wake_maintenance_zone: Optional[WakeMaintenanceZone]
    note: str

    def to_dict(self) -> dict:
        return {
            "n_nights_habitual": self.n_nights_habitual,
            "n_nights_recent": self.n_nights_recent,
            "habitual_midpoint_clock": self.habitual_midpoint_clock,
            "habitual_sleep_start_clock": self.habitual_sleep_start_clock,
            "habitual_sleep_end_clock": self.habitual_sleep_end_clock,
            "recent_midpoint_clock": self.recent_midpoint_clock,
            "phase_shift_hours": round(self.phase_shift_hours, 2)
            if self.phase_shift_hours is not None else None,
            "confidence": round(self.confidence, 2),
            "wake_maintenance_zone": self.wake_maintenance_zone.to_dict()
            if self.wake_maintenance_zone else None,
            "note": self.note,
        }


def _fallback(note: str) -> CircadianEstimate:
    return CircadianEstimate(
        n_nights_habitual=0, n_nights_recent=0, habitual_midpoint_clock=None,
        habitual_sleep_start_clock=None, habitual_sleep_end_clock=None,
        recent_midpoint_clock=None, phase_shift_hours=None, confidence=0.0,
        wake_maintenance_zone=None, note=note,
    )


def wake_maintenance_zone_from_midpoint(
    habitual_midpoint_min: float,
    typical_sleep_span_min: float = 480.0,
) -> WakeMaintenanceZone:
    """Derive the WMZ from a habitual sleep midpoint (folded minutes-past-midnight) + typical
    time-asleep. Habitual sleep ONSET = midpoint - span/2; the WMZ is the ~2.5 h window ending
    ~1 h before that onset (Lavie's 'forbidden zone for sleep')."""
    onset = habitual_midpoint_min - typical_sleep_span_min / 2.0
    end = onset - WMZ_END_BEFORE_SLEEP_MIN
    start = end - WMZ_DURATION_MIN
    start_f = _fold_evening(int(round(start)) % 1440)
    end_f = _fold_evening(int(round(end)) % 1440)
    return WakeMaintenanceZone(
        start_clock=_fmt_clock(start_f), end_clock=_fmt_clock(end_f),
        start_min=start_f, end_min=end_f,
    )


def estimate_circadian(repo, cfg=None,
                       recent_window: int = RECENT_WINDOW_NIGHTS,
                       habitual_window: int = HABITUAL_WINDOW_NIGHTS) -> CircadianEstimate:
    """Estimate circadian phase from ``repo.recent_nights(habitual_window)``.

    ``cfg`` is accepted for future personalization (e.g. a configured typical sleep span) but
    is optional and currently only consulted for ``sleep_need_min`` if present — everything
    else is derived from the data itself. Falls back gracefully with 0-1 usable nights.
    """
    try:
        nights = repo.recent_nights(habitual_window) if repo is not None else []
    except Exception:
        nights = []
    nights = list(nights or [])  # oldest-first, per Repository.recent_nights

    midpoints: List[int] = []
    for s in nights:
        mp = _night_midpoint(s)
        if mp is not None:
            midpoints.append(mp)

    if len(midpoints) < MIN_NIGHTS_FOR_ESTIMATE:
        return _fallback(
            "Not enough sleep history yet to estimate circadian phase "
            f"(have {len(midpoints)} usable night(s), need {MIN_NIGHTS_FOR_ESTIMATE}+)."
        )

    habitual_mid = _median(midpoints)
    spread = _mad(midpoints, habitual_mid)

    recent_slice = midpoints[-recent_window:]
    recent_mid = _median(recent_slice)

    phase_shift_min = _wrap_signed(recent_mid - habitual_mid)
    phase_shift_hours = phase_shift_min / 60.0

    # Confidence: more nights -> more confident (saturating), tighter habitual spread -> more
    # confident. Both terms in [0, 1]; combined multiplicatively so either weakness caps it.
    n_conf = min(1.0, len(midpoints) / float(habitual_window))
    # 3h (180 min) MAD or worse -> ~0 regularity confidence; 0 spread -> 1.0.
    spread_conf = max(0.0, 1.0 - spread / 180.0)
    confidence = round(n_conf * (0.5 + 0.5 * spread_conf), 3)

    # Typical sleep span (for the sleep-onset-anchored WMZ): median total_sleep_min if available.
    tst_vals = [float(getattr(s, "total_sleep_min", None)) for s in nights
                if getattr(s, "total_sleep_min", None)]
    span = _median(tst_vals) or 480.0
    wmz = wake_maintenance_zone_from_midpoint(habitual_mid, span)

    habitual_start = _fold_evening(int(round(habitual_mid - span / 2.0)) % 1440)
    habitual_end = _fold_evening(int(round(habitual_mid + span / 2.0)) % 1440)

    note = (
        f"Habitual sleep midpoint ~{_fmt_clock(habitual_mid)} from {len(midpoints)} night(s); "
        f"recent {recent_window}-night midpoint ~{_fmt_clock(recent_mid)}"
    )
    if abs(phase_shift_hours) >= 1.0:
        direction = "later" if phase_shift_hours > 0 else "earlier"
        note += f" — recent schedule is running ~{abs(phase_shift_hours):.1f} h {direction} than habit."
    else:
        note += " — close to habit, no significant phase shift detected."

    return CircadianEstimate(
        n_nights_habitual=len(midpoints),
        n_nights_recent=len(recent_slice),
        habitual_midpoint_clock=_fmt_clock(habitual_mid),
        habitual_sleep_start_clock=_fmt_clock(habitual_start),
        habitual_sleep_end_clock=_fmt_clock(habitual_end),
        recent_midpoint_clock=_fmt_clock(recent_mid),
        phase_shift_hours=phase_shift_hours,
        confidence=confidence,
        wake_maintenance_zone=wmz,
        note=note,
    )
