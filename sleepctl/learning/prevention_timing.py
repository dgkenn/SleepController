"""Is pre-emptive cooling PHYSICALLY CAPABLE of arriving before the awakening it targets?

Sleep maintenance is the #1 goal, and anticipatory cooling is the main tool for it: when a
vulnerable window is predicted, ``lead_time.py`` starts cooling ``lead_used_min`` ahead of it and
``precool_events`` records whether an awakening happened anyway.

That ledger answers "did it work". It does not answer **why it failed**, and the two causes need
opposite fixes:

  * **TIMING failure** — you woke *before the bed had actually moved*. The maneuver never had a
    chance; its magnitude is irrelevant. The fix is a LONGER LEAD.
  * **DOSE failure** — the bed had demonstrably arrived and you woke anyway. The lead was fine;
    the fix is a bigger/different nudge (or accepting that thermal can't prevent this window).

Without the split, the settle learner sees both as "prevention rate is low" and tunes magnitude —
which cannot fix a timing failure and burns nights discovering that. A system whose pre-emption is
timing-limited will look like it has a weak thermal response when it actually has a late one.

The split is measured, not modelled: arrival is read off the bed's OWN temperature trace
(``raw_samples.bed_temp_f``), so it needs no calibration and reflects whatever the water loop is
actually doing tonight — including a degraded one. That makes this doubly useful as a bring-up
check: if arrival is never observed at all, the actuator, not the controller, is the problem.

Pure functions over rows + a ``from_repo`` reader. No I/O in the analysis path.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

# A pre-cool is considered to have ARRIVED once the bed has fallen this far below its temperature
# at the moment cooling started. Deliberately small: we are timing the onset of a real thermal
# move, not waiting for the full setpoint, because the physiological effect of cutaneous cooling
# begins as the surface starts dropping (Raymann/Van Someren) rather than at the plateau.
ARRIVAL_DELTA_F = 0.5

# How long after a pre-cool starts we keep looking for arrival before calling it "never arrived".
# Past this the window has moved on and a late arrival is irrelevant to that event.
ARRIVAL_SEARCH_MIN = 45.0

# Minimum resolved FAILURES before the timing/dose split is reported as a verdict rather than as
# "not enough data". Below this a single unlucky night dominates the ratio.
MIN_FAILURES_FOR_VERDICT = 4

# Fraction of failures that must be timing-caused before we call the loop timing-limited.
TIMING_LIMITED_FRACTION = 0.6

# Safety margin added to the measured arrival when recommending a new lead.
LEAD_MARGIN_MIN = 3.0


@dataclass
class PreventionEvent:
    """One resolved pre-cool attempt, with its measured timings."""

    ts: datetime
    window_type: Optional[str]
    lead_used_min: Optional[float]
    prevented: bool
    arrival_min: Optional[float] = None   # measured minutes until the bed actually moved
    wake_min: Optional[float] = None      # minutes until the awakening (failures only)

    @property
    def cause(self) -> Optional[str]:
        """'timing' | 'dose' | None (prevented, or not enough measurement to say)."""
        if self.prevented or self.wake_min is None:
            return None
        if self.arrival_min is None:
            # Cooling was commanded and the bed never measurably moved inside the search window.
            # That is the most extreme timing failure there is, not an unknown.
            return "timing"
        return "timing" if self.wake_min < self.arrival_min else "dose"


@dataclass
class PreventionTimingReport:
    events: List[PreventionEvent] = field(default_factory=list)
    verdict: str = "insufficient_data"   # timing_limited | dose_limited | mixed | healthy | ...
    detail: str = ""
    remedy: Optional[str] = None
    median_arrival_min: Optional[float] = None
    median_wake_min: Optional[float] = None
    recommended_lead_min: Optional[float] = None
    by_window: dict = field(default_factory=dict)

    @property
    def n_resolved(self) -> int:
        return len(self.events)

    @property
    def n_failures(self) -> int:
        return sum(1 for e in self.events if not e.prevented)

    @property
    def n_timing(self) -> int:
        return sum(1 for e in self.events if e.cause == "timing")

    @property
    def n_dose(self) -> int:
        return sum(1 for e in self.events if e.cause == "dose")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "detail": self.detail,
            "remedy": self.remedy,
            "n_resolved": self.n_resolved,
            "n_failures": self.n_failures,
            "n_timing_failures": self.n_timing,
            "n_dose_failures": self.n_dose,
            "median_arrival_min": self.median_arrival_min,
            "median_wake_min": self.median_wake_min,
            "recommended_lead_min": self.recommended_lead_min,
            "by_window": self.by_window,
        }


def _median(vals: Sequence[float]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 1) if vals else None


def measure_arrival_min(samples: Sequence[dict], start: datetime,
                        delta_f: float = ARRIVAL_DELTA_F,
                        search_min: float = ARRIVAL_SEARCH_MIN) -> Optional[float]:
    """Minutes from ``start`` until ``bed_temp_f`` first falls ``delta_f`` below its value at
    ``start``. None if the bed never got there inside the search window (or there's no trace).

    ``samples`` — dicts/rows with ``ts`` (datetime or ISO str) and ``bed_temp_f``, any order.
    The reference is the LAST reading at or before ``start``: that is the temperature the bed was
    actually holding when cooling was commanded, which is what the drop must be measured against.
    """
    pts = []
    for s in samples:
        t = _as_dt(s.get("ts") if isinstance(s, dict) else s["ts"])
        v = (s.get("bed_temp_f") if isinstance(s, dict) else s["bed_temp_f"])
        if t is None or v is None:
            continue
        try:
            pts.append((t, float(v)))
        except (TypeError, ValueError):
            continue
    if not pts:
        return None
    pts.sort(key=lambda p: p[0])

    ref = None
    for t, v in pts:
        if t <= start:
            ref = v
        else:
            break
    if ref is None:
        # No reading at/before the start (e.g. the pre-cool is the first thing in the trace).
        # Fall back to the earliest reading inside the window rather than giving up.
        after = [(t, v) for t, v in pts if t >= start]
        if not after:
            return None
        ref = after[0][1]

    horizon = start + timedelta(minutes=float(search_min))
    for t, v in pts:
        if t < start or t > horizon:
            continue
        if v <= ref - float(delta_f):
            return round((t - start).total_seconds() / 60.0, 1)
    return None


def first_wake_min(samples: Sequence[dict], start: datetime,
                   search_min: float = ARRIVAL_SEARCH_MIN) -> Optional[float]:
    """Minutes from ``start`` to the first ``wake_event`` sample inside the search window."""
    horizon = start + timedelta(minutes=float(search_min))
    best = None
    for s in samples:
        t = _as_dt(s.get("ts") if isinstance(s, dict) else s["ts"])
        w = (s.get("wake_event") if isinstance(s, dict) else s["wake_event"])
        if t is None or not w:
            continue
        if start <= t <= horizon:
            m = (t - start).total_seconds() / 60.0
            if best is None or m < best:
                best = m
    return round(best, 1) if best is not None else None


def _as_dt(v) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def analyze(events: Sequence[PreventionEvent]) -> PreventionTimingReport:
    """Split resolved failures into timing- vs dose-caused and recommend a lead."""
    evs = list(events)
    rep = PreventionTimingReport(events=evs)
    if not evs:
        rep.detail = "no resolved pre-cool events yet"
        return rep

    arrivals = [e.arrival_min for e in evs if e.arrival_min is not None]
    rep.median_arrival_min = _median(arrivals)
    rep.median_wake_min = _median([e.wake_min for e in evs if e.wake_min is not None])

    # Per-window breakdown, useful because the four window types have very different characters.
    for e in evs:
        w = e.window_type or "unknown"
        b = rep.by_window.setdefault(w, {"n": 0, "failures": 0, "timing": 0, "dose": 0})
        b["n"] += 1
        if not e.prevented:
            b["failures"] += 1
        if e.cause == "timing":
            b["timing"] += 1
        elif e.cause == "dose":
            b["dose"] += 1

    # The actuator check comes FIRST: if cooling was commanded repeatedly and the bed never
    # measurably moved, nothing downstream is meaningful and the fix isn't in the controller.
    if arrivals == [] and len(evs) >= MIN_FAILURES_FOR_VERDICT:
        rep.verdict = "no_thermal_response"
        rep.detail = (f"{len(evs)} pre-cools commanded and the bed never fell {ARRIVAL_DELTA_F} F "
                      f"within {ARRIVAL_SEARCH_MIN:.0f} min of any of them")
        rep.remedy = ("the water loop is not delivering cooling — check priming / reservoir "
                      "before trusting any prevention or dose-response result")
        return rep

    failures = [e for e in evs if not e.prevented]
    if len(failures) < MIN_FAILURES_FOR_VERDICT:
        rep.verdict = "insufficient_data"
        rep.detail = (f"{len(failures)} resolved failure(s); need {MIN_FAILURES_FOR_VERDICT} "
                      f"before splitting timing vs dose")
        if rep.median_arrival_min is not None:
            rep.detail += f" (measured thermal arrival so far: {rep.median_arrival_min} min)"
        return rep

    n_timing, n_dose = rep.n_timing, rep.n_dose
    frac_timing = n_timing / len(failures) if failures else 0.0

    # A lead only needs raising if timing is actually the binding constraint; recommend enough to
    # cover the measured arrival with a margin, never less than the leads already in use.
    if rep.median_arrival_min is not None:
        used = [e.lead_used_min for e in failures if e.lead_used_min is not None]
        floor = max(used) if used else 0.0
        rep.recommended_lead_min = round(max(rep.median_arrival_min + LEAD_MARGIN_MIN, floor), 1)

    if frac_timing >= TIMING_LIMITED_FRACTION:
        rep.verdict = "timing_limited"
        rep.detail = (f"{n_timing}/{len(failures)} prevention failures happened BEFORE the bed had "
                      f"moved (median arrival {rep.median_arrival_min} min, median wake "
                      f"{rep.median_wake_min} min)")
        rep.remedy = (f"increase the pre-cool lead to ~{rep.recommended_lead_min} min — a larger "
                      f"nudge cannot fix an arrival that comes after the awakening")
    elif n_dose > n_timing:
        rep.verdict = "dose_limited"
        rep.detail = (f"{n_dose}/{len(failures)} failures happened AFTER the bed had arrived "
                      f"(median arrival {rep.median_arrival_min} min) — the lead is sufficient")
        rep.remedy = ("lead time is not the constraint; let the settle learner adjust magnitude, "
                      "or accept that these windows aren't thermally preventable")
    else:
        rep.verdict = "mixed"
        rep.detail = (f"{n_timing} timing / {n_dose} dose failures of {len(failures)} — no single "
                      f"binding constraint yet")
        rep.remedy = "keep collecting nights; re-check once the split separates"
    return rep


def from_repo(repo, nights: int = 30, search_min: float = ARRIVAL_SEARCH_MIN
              ) -> PreventionTimingReport:
    """Build the report from the live ledger. Fully defensive: any read failure degrades to an
    empty report rather than raising into a caller (diagnostics, CLI, the nightly job)."""
    try:
        cutoff = (datetime.now() - timedelta(days=int(nights))).isoformat()
        rows = repo.conn.execute(
            "SELECT ts, window_type, lead_used_min, prevented FROM precool_events "
            "WHERE resolved = 1 AND ts >= ? ORDER BY ts ASC", (cutoff,)
        ).fetchall()
    except Exception:
        return PreventionTimingReport(detail="pre-cool ledger unavailable")

    events: List[PreventionEvent] = []
    for r in rows:
        t0 = _as_dt(r["ts"])
        if t0 is None:
            continue
        try:
            samples = repo.conn.execute(
                "SELECT ts, bed_temp_f, wake_event FROM raw_samples "
                "WHERE ts >= ? AND ts <= ? ORDER BY ts ASC",
                # Reach back a little BEFORE the pre-cool so there is a reference reading for the
                # temperature the bed was holding when cooling was commanded.
                ((t0 - timedelta(minutes=10)).isoformat(),
                 (t0 + timedelta(minutes=float(search_min))).isoformat()),
            ).fetchall()
        except Exception:
            samples = []
        samples = [dict(s) for s in samples]
        prevented = bool(r["prevented"])
        events.append(PreventionEvent(
            ts=t0,
            window_type=r["window_type"],
            lead_used_min=r["lead_used_min"],
            prevented=prevented,
            arrival_min=measure_arrival_min(samples, t0, search_min=search_min),
            wake_min=None if prevented else first_wake_min(samples, t0, search_min=search_min),
        ))
    return analyze(events)
