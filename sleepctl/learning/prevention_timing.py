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

The split is measured, not modelled: arrival is read off the bed's own trace, so it needs no
calibration and reflects whatever the water loop is actually doing tonight — including a degraded
one. That makes this doubly useful as a bring-up check: if arrival is never observed at all, the
actuator, not the controller, is the problem.

**Which trace, and why it matters.** There are two, and they are not interchangeable:

  * ``raw_samples.bed_temp_f`` — the sensed cover temperature, from the trends session timeseries
    (``tempBedC``). Genuinely a thermometer, but it comes down the SAME membership-gated pipeline as
    HR/HRV/stage: with no active Autopilot subscription it is ``None`` on every row, forever. It is
    also session-gated even when available (absent for the first ~15-30 min of a night).
  * ``thermal_samples.device_level`` — the Hub's own water-temp-derived achieved level
    (``currentDeviceLevel``), distinct from the commanded ``targetHeatingLevel``. Available from the
    fast device GET with no membership at all, and it is the signal
    ``controller.thermal_health.ThermalResponseMonitor`` already trusts to decide whether the
    element is working.

So this module prefers ``bed_temp_f`` when it exists and falls back to ``device_level``. The
distinction that must never collapse is **"the bed did not move" vs "we could not see the bed"**:
the first is a broken actuator, the second is a missing subscription, and reporting the second as
the first sends someone to drain a water loop that was fine. Events with no usable trace are
counted as unmeasurable and excluded from the timing/dose split rather than defaulting into it.

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

# The same arrival test on the device-level trace, for boxes with no sensed bed temperature. The
# level<->F map is non-linear but runs ~3-4 levels per F near neutral (docs/THERMAL_LATENCY.md), so
# 2 levels is the rough equivalent of ARRIVAL_DELTA_F and, at a measured ~1.5 levels/min cooling
# rate, is still comfortably above per-tick jitter.
ARRIVAL_DELTA_LEVELS = 2.0

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
    # Which trace arrival was measured from, or would have been: "bed_temp" | "device_level" | None.
    # None means NEITHER trace had a usable reading in this event's window — we were blind, which is
    # a different fact from "the bed held still" and must not be scored as one.
    arrival_source: Optional[str] = None

    @property
    def measurable(self) -> bool:
        """Did we have any trace at all to judge this event against?"""
        return self.arrival_source is not None

    @property
    def cause(self) -> Optional[str]:
        """'timing' | 'dose' | None (prevented, unmeasurable, or no awakening to compare)."""
        if self.prevented or self.wake_min is None:
            return None
        if not self.measurable:
            # We could not see the bed. Silently calling this a timing failure would recommend an
            # ever-longer lead on the strength of no evidence.
            return None
        if self.arrival_min is None:
            # We COULD see the bed and it never measurably moved inside the search window. That is
            # the most extreme timing failure there is, not an unknown.
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

    @property
    def n_measurable(self) -> int:
        return sum(1 for e in self.events if e.measurable)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "detail": self.detail,
            "remedy": self.remedy,
            "n_resolved": self.n_resolved,
            "n_failures": self.n_failures,
            "n_timing_failures": self.n_timing,
            "n_dose_failures": self.n_dose,
            "n_measurable": self.n_measurable,
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

    Note this is the SENSED cover temperature, which is membership-gated — see the module
    docstring. ``None`` here is ambiguous on its own; pair it with ``has_readings`` to tell "the bed
    held still" from "there was nothing to read".
    """
    return _measure_drop(samples, start, "bed_temp_f", float(delta_f), float(search_min))


def measure_level_arrival_min(samples: Sequence[dict], start: datetime,
                              delta_levels: float = ARRIVAL_DELTA_LEVELS,
                              search_min: float = ARRIVAL_SEARCH_MIN) -> Optional[float]:
    """``measure_arrival_min`` against the water-side ``device_level`` trace instead of a
    thermometer, for boxes with no sensed bed temperature (no Autopilot membership).

    Same contract: minutes from ``start`` until the level first falls ``delta_levels`` below its
    value at ``start``, or None if it never did inside the window. ``samples`` — dicts with ``ts``
    and ``device_level``.

    This reads ``currentDeviceLevel`` — the level the Hub has ACHIEVED — never
    ``targetHeatingLevel``. Measuring arrival off the target would just replay our own command back
    at us and report a perfect thermal response from a bed sitting in a puddle.
    """
    return _measure_drop(samples, start, "device_level", float(delta_levels), float(search_min))


def _measure_drop(samples: Sequence[dict], start: datetime, key: str,
                  delta: float, search_min: float) -> Optional[float]:
    """Minutes until ``key`` first falls ``delta`` below its value at ``start``; None if never."""
    pts = []
    for s in samples:
        t = _as_dt(s.get("ts") if isinstance(s, dict) else s["ts"])
        try:
            v = s[key] if not isinstance(s, dict) else s.get(key)
        except (KeyError, IndexError):
            continue
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

    horizon = start + timedelta(minutes=search_min)
    for t, v in pts:
        if t < start or t > horizon:
            continue
        if v <= ref - delta:
            return round((t - start).total_seconds() / 60.0, 1)
    return None


def has_readings(samples: Sequence[dict], key: str) -> bool:
    """Whether ``samples`` contains at least one non-null ``key``. The blind/still discriminator."""
    for s in samples:
        try:
            v = s.get(key) if isinstance(s, dict) else s[key]
        except (KeyError, IndexError):
            continue
        if v is not None:
            return True
    return False


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
    """Parse to a NAIVE-LOCAL datetime.

    Callers in this codebase are inconsistent about naive-vs-aware timestamps: the daemon writes
    ``raw_samples``/``precool_events`` as naive local (``datetime.now()``), while other paths use
    ``datetime.now(timezone.utc)``. Subtracting one from the other raises TypeError, which here
    would take out every duration this module computes. Normalize on the way in — same defensive
    posture as ``sleepctl.diagnostics_thermal._parse_iso`` — so one aware row can't poison the
    analysis. Aware values are converted to local time first, not merely stripped, so the arrival
    and wake offsets stay correct rather than being silently shifted by the UTC offset."""
    if isinstance(v, datetime):
        dt = v
    elif not v:
        return None
    else:
        try:
            dt = datetime.fromisoformat(str(v))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


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

    # BLIND comes first, because it invalidates every judgement below it. With no sensed bed
    # temperature and no device-level trace we cannot tell a dead water loop from a healthy one,
    # and the old code called that "no thermal response" — sending the user to drain a loop that
    # may be fine, on the strength of a missing subscription.
    measurable = [e for e in evs if e.measurable]
    if not measurable and len(evs) >= MIN_FAILURES_FOR_VERDICT:
        rep.verdict = "no_thermal_data"
        rep.detail = (f"{len(evs)} pre-cools commanded but neither thermal trace had any reading — "
                      f"we cannot tell whether the bed moved")
        rep.remedy = ("this is a SENSING gap, not proof of a thermal fault: sensed bed temperature "
                      "needs an active Autopilot membership, and the device-level trace needs the "
                      "daemon writing thermal_samples. Judge the water loop from the thermal "
                      "self-test / thermal_response check instead")
        return rep

    # THEN the actuator check: cooling was commanded repeatedly, we could SEE the bed, and it never
    # measurably moved. Now the fix genuinely isn't in the controller.
    if arrivals == [] and len(measurable) >= MIN_FAILURES_FOR_VERDICT:
        rep.verdict = "no_thermal_response"
        rep.detail = (f"{len(measurable)} pre-cools commanded and the bed never moved "
                      f"(>= {ARRIVAL_DELTA_F} F, or {ARRIVAL_DELTA_LEVELS:.0f} device levels) "
                      f"within {ARRIVAL_SEARCH_MIN:.0f} min of any of them")
        rep.remedy = ("the water loop is not delivering cooling — check priming / reservoir "
                      "before trusting any prevention or dose-response result")
        return rep

    # Only measurable failures can be split; an unmeasurable one has no cause to assign.
    failures = [e for e in evs if not e.prevented and e.measurable]
    n_blind = sum(1 for e in evs if not e.prevented and not e.measurable)
    if len(failures) < MIN_FAILURES_FOR_VERDICT:
        rep.verdict = "insufficient_data"
        rep.detail = (f"{len(failures)} measurable failure(s); need {MIN_FAILURES_FOR_VERDICT} "
                      f"before splitting timing vs dose")
        if n_blind:
            rep.detail += f" ({n_blind} more had no thermal trace to judge against)"
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

        # Prefer the thermometer; fall back to the water-side level when it is absent (no
        # membership, or the first 15-30 min of a night before the session opens).
        if has_readings(samples, "bed_temp_f"):
            source = "bed_temp"
            arrival = measure_arrival_min(samples, t0, search_min=search_min)
        else:
            levels = _level_samples(repo, t0, search_min)
            source = "device_level" if has_readings(levels, "device_level") else None
            arrival = (measure_level_arrival_min(levels, t0, search_min=search_min)
                       if source else None)

        prevented = bool(r["prevented"])
        events.append(PreventionEvent(
            ts=t0,
            window_type=r["window_type"],
            lead_used_min=r["lead_used_min"],
            prevented=prevented,
            arrival_min=arrival,
            arrival_source=source,
            wake_min=None if prevented else first_wake_min(samples, t0, search_min=search_min),
        ))
    return analyze(events)


def _level_samples(repo, t0: datetime, search_min: float) -> List[dict]:
    """The ``thermal_samples`` device-level trace around one pre-cool. Defensive: the table lives in
    the dashboard schema, so an engine-only database simply has no trace rather than an error."""
    try:
        rows = repo.conn.execute(
            "SELECT ts, device_level FROM thermal_samples WHERE ts >= ? AND ts <= ? ORDER BY ts ASC",
            ((t0 - timedelta(minutes=10)).isoformat(),
             (t0 + timedelta(minutes=float(search_min))).isoformat()),
        ).fetchall()
    except Exception:
        return []
    return [dict(s) for s in rows]
