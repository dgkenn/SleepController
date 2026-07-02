"""3AM WAKE targeted analysis — find the user's PERSONAL recurring wake patterns.

Sleep maintenance (staying asleep) is the #1 problem this controller exists to solve. This
module answers the concrete question "when do I keep waking up, and why?" from the user's own
logged history:

  1. ``cluster_awakenings`` — bucket every logged awakening by CLOCK TIME (30-min bins spanning
     the 24h clock) and by the sleep STAGE it exited, and surface bins the user wakes from
     disproportionately often (a "recurring window"). Robust to thin data: a bin is never
     reported before it has repeated evidence, and its confidence is a saturating function of
     how much evidence has accrued (more nights, more repeats -> higher confidence).
  2. ``correlate_wakes`` — for a recurring window, correlate wake probability with candidate
     drivers (bed setpoint, room temperature, HRV deviation from that night's own baseline,
     time since sleep onset, night-type) using pure-python stats: point-biserial correlation +
     a mean-difference with a crude 90% CI for continuous drivers, an odds ratio for the
     categorical night-type driver. Ranked by association strength; exploratory (n-of-1), never
     claims proof of causation.
  3. ``wake_analysis_report`` — the full structured report: every recurring window, its top
     correlated drivers, and (once confidence clears a bar) a concrete, bounded, comfort-aware
     SUGGESTION -- e.g. "you wake ~03:10 out of REM; on those nights the bed ran warm -- try a
     gentle 0.5°F cool nudge starting ~02:40."
  4. ``should_preempt_window`` — the do-no-harm GATE the controller (optionally) consumes to
     preemptively smooth the bed a little before a HIGH-CONFIDENCE recurring window. Returns
     None (log-only, no effect) unless the enabled flag, minimum-nights, and confidence
     thresholds all clear -- see ``sleepctl.config.Tunables.wake_window_preempt_*``.

Standalone: reads only ``Repository`` (``raw_samples``/``decisions``/``context``/
``recent_nights``), no dependency on the efficacy-trial module. Deterministic -- every function
either derives time from the stored data or takes ``now`` as an explicit argument; nothing here
reads the wall clock or a random source itself.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# Awakenings are only detected/logged while the controller is actively maintaining sleep (see
# ArousalDetector's call site in SleepController.decide), so these are the states in which a
# sample both (a) represents real overnight monitoring and (b) could have a wake_event=1 flag.
_SLEEP_STATES = ("maintenance", "wake_recovery")
_SLEEP_STATES_SQL = "('maintenance','wake_recovery')"

DEFAULT_BIN_MIN = 30
MIN_NIGHTS_WOKE_TO_REPORT = 2   # a single odd night is not yet a "recurring" cluster
_CONF_FLOOR_NIGHTS_WOKE = 2     # confidence is exactly 0 below this many distinct occurrences
_CONF_COUNT_SATURATION = 6.0    # the "more repeats" half of confidence saturates around here
_CONF_RATE_SATURATION = 0.5     # the "more consistent" half saturates at a 50% recurrence rate
SUGGESTION_CONFIDENCE_MIN = 0.35  # below this: report the window, but no concrete suggestion
_DRIVER_MIN_N = 3               # a driver needs at least this many nights of data to be ranked


# --------------------------------------------------------------------------------- utilities


def _parse_ts(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _minute_of_day(ts: datetime) -> int:
    return ts.hour * 60 + ts.minute


def _bin_index(minute: int, bin_min: int) -> int:
    return minute // bin_min


def _bin_bounds(bin_idx: int, bin_min: int) -> tuple:
    start = (bin_idx * bin_min) % 1440
    end = start + bin_min
    return start, end


def _clock(minute: int) -> str:
    minute = minute % 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _bin_label(bin_idx: int, bin_min: int) -> str:
    start, end = _bin_bounds(bin_idx, bin_min)
    return f"{_clock(start)}–{_clock(end % 1440)}"


def _night_dates(repo, lookback_nights: int) -> list:
    """Distinct recent night dates, oldest-first (mirrors ``recent_nights``' ordering)."""
    summaries = repo.recent_nights(lookback_nights) if hasattr(repo, "recent_nights") else []
    return [s.date for s in summaries if getattr(s, "date", None)]


# ---------------------------------------------------------------------- pure-python statistics
# Deliberately no numpy (matches the style of ``sleepctl.ml``): small, explicit, dependency-free.


def _mean(values) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def _variance(values) -> Optional[float]:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    m = sum(vals) / len(vals)
    return sum((v - m) ** 2 for v in vals) / (len(vals) - 1)


def point_biserial(values, labels) -> Optional[float]:
    """Point-biserial correlation between a continuous driver and a binary (0/1) wake label.

    Standard formula: r = (mean1 - mean0) / sd_total * sqrt(p * q). None when there isn't
    enough data or both labels aren't represented.
    """
    pairs = [(v, l) for v, l in zip(values, labels) if v is not None]
    if len(pairs) < 4:
        return None
    g1 = [v for v, l in pairs if l == 1]
    g0 = [v for v, l in pairs if l == 0]
    if not g1 or not g0:
        return None
    n = len(pairs)
    allvals = [v for v, _ in pairs]
    m = sum(allvals) / n
    var = sum((v - m) ** 2 for v in allvals) / n
    sd = math.sqrt(var)
    if sd <= 1e-9:
        return None
    m1 = sum(g1) / len(g1)
    m0 = sum(g0) / len(g0)
    p = len(g1) / n
    q = 1.0 - p
    return ((m1 - m0) / sd) * math.sqrt(p * q)


def mean_diff_ci(values, labels, z: float = 1.64) -> Optional[dict]:
    """Mean(driver | wake=1) - mean(driver | wake=0), with a crude 90% CI (normal approx,
    unequal variances -- a "Welch-style" standard error). Not a formal hypothesis test; a
    quick, honest sense of whether the interval clears zero."""
    pairs = [(v, l) for v, l in zip(values, labels) if v is not None]
    g1 = [v for v, l in pairs if l == 1]
    g0 = [v for v, l in pairs if l == 0]
    if len(g1) < 2 or len(g0) < 2:
        return None
    m1, m0 = sum(g1) / len(g1), sum(g0) / len(g0)
    v1, v0 = _variance(g1) or 0.0, _variance(g0) or 0.0
    se = math.sqrt(v1 / len(g1) + v0 / len(g0))
    diff = m1 - m0
    return {
        "mean_on_wake_nights": round(m1, 2),
        "mean_otherwise": round(m0, 2),
        "diff": round(diff, 3),
        "ci90_low": round(diff - z * se, 3),
        "ci90_high": round(diff + z * se, 3),
        "n_wake": len(g1),
        "n_no_wake": len(g0),
    }


def odds_ratio(a: float, b: float, c: float, d: float) -> float:
    """2x2 odds ratio -- a=wake&exposed, b=no-wake&exposed, c=wake&unexposed, d=no-wake&unexposed
    -- with a Haldane-Anscombe +0.5 correction so a zero cell never blows up to inf/undefined."""
    a, b, c, d = a + 0.5, b + 0.5, c + 0.5, d + 0.5
    return (a * d) / (b * c)


# ------------------------------------------------------------------------------ wake clustering


@dataclass
class WakeCluster:
    """A clock-time bin the user wakes from disproportionately often."""

    bin_index: int
    bin_min: int
    label: str                      # "03:00–03:30"
    stage_exited: str                # most common stage exited into this awakening ("rem", ...)
    stage_breakdown: dict = field(default_factory=dict)
    nights_observed: int = 0         # distinct nights with monitored data in this bin
    nights_woke: int = 0             # distinct nights with >=1 awakening in this bin
    total_events: int = 0            # total awakening samples across all those nights
    wake_rate: float = 0.0           # nights_woke / nights_observed
    confidence: float = 0.0          # 0..1, see ``_confidence``
    confidence_label: str = "low"    # "low" | "moderate" | "high"
    example_nights: list = field(default_factory=list)

    def to_dict(self) -> dict:
        start, end = _bin_bounds(self.bin_index, self.bin_min)
        return {
            "label": self.label,
            "bin_start_min": start,
            "bin_end_min": end,
            "stage_exited": self.stage_exited,
            "stage_breakdown": dict(self.stage_breakdown),
            "nights_observed": self.nights_observed,
            "nights_woke": self.nights_woke,
            "total_events": self.total_events,
            "wake_rate": round(self.wake_rate, 3) if self.wake_rate is not None else None,
            "confidence": round(self.confidence, 3),
            "confidence_label": self.confidence_label,
            "example_nights": list(self.example_nights),
        }


def _confidence(nights_woke: int, nights_observed: int) -> float:
    """Deterministic, saturating confidence in [0,1] for "this clock-time bin is a real
    recurring pattern, not noise". Zero until repeated evidence exists (a single odd night must
    never read as a pattern); then rewards BOTH the absolute count of occurrences (more repeats
    = more sure) and the RATE of recurrence across the nights we actually observed that window
    (more consistent = more sure), each saturating so a handful of nights can't overclaim
    certainty. Half-weighted blend of the two, capped at 1.0."""
    if nights_observed <= 0 or nights_woke < _CONF_FLOOR_NIGHTS_WOKE:
        return 0.0
    count_term = min(1.0, (nights_woke - _CONF_FLOOR_NIGHTS_WOKE + 1) / _CONF_COUNT_SATURATION)
    rate = nights_woke / nights_observed
    rate_term = min(1.0, rate / _CONF_RATE_SATURATION)
    return round(min(1.0, 0.5 * count_term + 0.5 * rate_term), 3)


def _confidence_label(confidence: float) -> str:
    if confidence >= 0.7:
        return "high"
    if confidence >= SUGGESTION_CONFIDENCE_MIN:
        return "moderate"
    return "low"


def cluster_awakenings(
    repo, lookback_nights: int = 60, bin_min: int = DEFAULT_BIN_MIN,
    min_nights_woke: int = MIN_NIGHTS_WOKE_TO_REPORT,
) -> list:
    """Bucket logged awakenings by CLOCK TIME + the sleep STAGE exited, and surface recurring
    clusters -- clock-time bins the user wakes from disproportionately often.

    Reads ``raw_samples`` for the most recent ``lookback_nights`` (per ``Repository.
    recent_nights``), restricted to the states where awakenings are actually detected
    (maintenance / wake_recovery). Robust to few nights: a bin is only returned once it has
    been observed waking on >= ``min_nights_woke`` distinct nights, and its confidence (see
    ``_confidence``) only rises as more evidence accrues -- it never overclaims from one night.
    """
    dates = _night_dates(repo, lookback_nights)
    if not dates:
        return []
    placeholders = ",".join("?" * len(dates))
    rows = repo.conn.execute(
        f"SELECT ts, night_date, stage, wake_event FROM raw_samples "
        f"WHERE night_date IN ({placeholders}) AND controller_state IN {_SLEEP_STATES_SQL} "
        f"ORDER BY night_date ASC, ts ASC",
        dates,
    ).fetchall()

    observed: dict = defaultdict(set)
    woke_nights: dict = defaultdict(set)
    event_count: dict = defaultdict(int)
    stage_counts: dict = defaultdict(lambda: defaultdict(int))
    example_nights: dict = defaultdict(list)
    last_asleep_stage: dict = {}   # night_date -> last DEEP/REM/LIGHT stage seen so far

    for r in rows:
        ts = _parse_ts(r["ts"])
        if ts is None:
            continue
        night = r["night_date"]
        stage = r["stage"]
        b = _bin_index(_minute_of_day(ts), bin_min)
        observed[b].add(night)
        if r["wake_event"]:
            event_count[b] += 1
            exited = last_asleep_stage.get(night, "unknown")
            stage_counts[b][exited] += 1
            if night not in woke_nights[b]:
                woke_nights[b].add(night)
                if len(example_nights[b]) < 5:
                    example_nights[b].append(night)
        if stage in ("deep", "rem", "light"):
            last_asleep_stage[night] = stage

    clusters = []
    for b, nights in woke_nights.items():
        nights_woke = len(nights)
        if nights_woke < min_nights_woke:
            continue
        nights_observed = len(observed[b])
        wake_rate = (nights_woke / nights_observed) if nights_observed else 0.0
        breakdown = dict(stage_counts[b])
        stage_exited = max(breakdown.items(), key=lambda kv: kv[1])[0] if breakdown else "unknown"
        conf = _confidence(nights_woke, nights_observed)
        clusters.append(WakeCluster(
            bin_index=b, bin_min=bin_min, label=_bin_label(b, bin_min),
            stage_exited=stage_exited, stage_breakdown=breakdown,
            nights_observed=nights_observed, nights_woke=nights_woke,
            total_events=event_count[b], wake_rate=wake_rate,
            confidence=conf, confidence_label=_confidence_label(conf),
            example_nights=sorted(example_nights[b]),
        ))

    clusters.sort(key=lambda c: (c.confidence, c.wake_rate, c.nights_woke), reverse=True)
    return clusters


# --------------------------------------------------------------------- driver correlation

_DRIVER_LABELS = {
    "bed_setpoint_f": "bed setpoint (target °F)",
    "room_temp_f": "room temperature (°F)",
    "hrv_deviation": "HRV deviation vs that night's own baseline (ms)",
    "minutes_since_onset": "time since sleep onset (min)",
}


def correlate_wakes(repo, cluster: WakeCluster, lookback_nights: int = 60) -> dict:
    """Correlate wake probability inside ``cluster``'s clock-time window with candidate
    drivers: bed setpoint (from ``decisions.target_temp_f``, nearest at/before the window),
    room temperature, HRV deviation from that night's own mean, minutes since sleep onset, and
    night-type. Pure-python: point-biserial r + a mean-diff/90% CI for the continuous drivers,
    an odds ratio for each observed night-type category. Ranked by a common |effect| scale so
    the strongest association surfaces first. Exploratory (n-of-1 observational correlation,
    not a randomized comparison) -- never claims causation, only association strength.
    """
    dates = _night_dates(repo, lookback_nights)
    bin_start, bin_end = _bin_bounds(cluster.bin_index, cluster.bin_min)

    rows_by_night: dict = defaultdict(list)
    if dates:
        placeholders = ",".join("?" * len(dates))
        rows = repo.conn.execute(
            f"SELECT ts, night_date, room_temp_f, hrv, wake_event FROM raw_samples "
            f"WHERE night_date IN ({placeholders}) AND controller_state IN {_SLEEP_STATES_SQL} "
            f"ORDER BY night_date ASC, ts ASC",
            dates,
        ).fetchall()
        for r in rows:
            rows_by_night[r["night_date"]].append(r)

    labels, bed_setpoint, room_temp, hrv_dev, mins_since_onset, night_types = (
        [], [], [], [], [], [])
    for night, samples in rows_by_night.items():
        first_ts = None
        night_start_minute = None
        hrv_all = []
        in_window = []
        woke_in_window = False
        for r in samples:
            ts = _parse_ts(r["ts"])
            if ts is None:
                continue
            if first_ts is None:
                first_ts = ts
                night_start_minute = _minute_of_day(ts)
            if r["hrv"] is not None:
                hrv_all.append(r["hrv"])
            minute = _minute_of_day(ts)
            if bin_start <= minute < bin_end:
                in_window.append(r)
                if r["wake_event"]:
                    woke_in_window = True
        if not in_window or first_ts is None:
            continue  # this night has no monitored data in this clock window -- skip it
        labels.append(1 if woke_in_window else 0)
        room_temp.append(_mean([r["room_temp_f"] for r in in_window]))
        win_hrv = _mean([r["hrv"] for r in in_window])
        night_hrv = _mean(hrv_all)
        hrv_dev.append(
            (win_hrv - night_hrv) if (win_hrv is not None and night_hrv is not None) else None)
        delta = bin_start - night_start_minute
        if delta < 0:
            delta += 1440
        mins_since_onset.append(float(delta))
        dec = repo.conn.execute(
            "SELECT target_temp_f FROM decisions WHERE night_date=? AND ts <= ? "
            "ORDER BY ts DESC LIMIT 1",
            (night, in_window[0]["ts"]),
        ).fetchone()
        bed_setpoint.append(dec["target_temp_f"] if dec else None)
        ctx = repo.get_context(night) if hasattr(repo, "get_context") else None
        night_types.append((getattr(ctx, "night_type", None) or "normal") if ctx else "normal")

    n = len(labels)
    drivers = []
    for key, values in (
        ("bed_setpoint_f", bed_setpoint), ("room_temp_f", room_temp),
        ("hrv_deviation", hrv_dev), ("minutes_since_onset", mins_since_onset),
    ):
        n_have = sum(1 for v in values if v is not None)
        if n_have == 0:
            continue  # no data at all for this driver -- nothing to report
        r = point_biserial(values, labels) if n_have >= _DRIVER_MIN_N else None
        md = mean_diff_ci(values, labels) if n_have >= _DRIVER_MIN_N else None
        drivers.append({
            "driver": key, "label": _DRIVER_LABELS[key], "type": "continuous",
            "point_biserial_r": round(r, 3) if r is not None else None,
            "mean_diff": md, "n": n_have,
            "strength": abs(r) if r is not None else 0.0,
        })

    # Categorical: night_type, dummy-coded per observed non-"normal" category vs the rest.
    for cat in sorted({nt for nt in night_types if nt} - {"normal"}):
        a = sum(1 for nt, l in zip(night_types, labels) if nt == cat and l == 1)
        b = sum(1 for nt, l in zip(night_types, labels) if nt == cat and l == 0)
        c = sum(1 for nt, l in zip(night_types, labels) if nt != cat and l == 1)
        d = sum(1 for nt, l in zip(night_types, labels) if nt != cat and l == 0)
        if (a + b) < _DRIVER_MIN_N:
            continue
        orv = odds_ratio(a, b, c, d)
        # Compress log(OR) onto roughly the same [0,1] scale as |point-biserial r| so ranking
        # across continuous + categorical drivers is at least roughly comparable -- crude by
        # design (this is exploratory ranking, not a meta-analytic pooling of effect sizes).
        strength = min(1.0, abs(math.log(orv)) / 3.0)
        drivers.append({
            "driver": f"night_type:{cat}", "label": f"night type = {cat}", "type": "categorical",
            "odds_ratio": round(orv, 2), "n": a + b + c + d,
            "wake_rate_when": round(a / (a + b), 3) if (a + b) else None,
            "wake_rate_otherwise": round(c / (c + d), 3) if (c + d) else None,
            "strength": strength,
        })

    drivers.sort(key=lambda d: d["strength"], reverse=True)
    return {
        "window": cluster.label, "n_nights": n, "drivers": drivers,
        "note": ("exploratory n-of-1 correlation, ranked by association strength -- not a "
                 "randomized comparison and not proof of causation; treat the top driver as a "
                 "hypothesis worth a small nudge, not a settled fact"),
    }


# --------------------------------------------------------------------------- suggestions


def _driver_note(top: dict) -> str:
    if top["type"] == "continuous" and top.get("mean_diff"):
        md = top["mean_diff"]
        return (f"on wake nights {top['label']} averaged {md['mean_on_wake_nights']} vs "
                f"{md['mean_otherwise']} otherwise")
    if top["type"] == "categorical":
        return (f"{top['label']} nights wake {top.get('wake_rate_when')} of the time vs "
                f"{top.get('wake_rate_otherwise')} otherwise")
    return ""


def _suggestion_for(cluster: WakeCluster, corr: dict, cfg=None) -> Optional[dict]:
    """A concrete, bounded, comfort-aware suggestion for a recurring window -- or None if
    confidence hasn't cleared the bar yet (low-n nights get reported, not acted on)."""
    if cluster.confidence < SUGGESTION_CONFIDENCE_MIN:
        return None
    candidates = [d for d in corr["drivers"] if d.get("n", 0) >= _DRIVER_MIN_N]
    top = candidates[0] if candidates else None

    t = getattr(cfg, "tunables", None)
    cap = getattr(t, "wake_window_preempt_max_f", 0.5) if t else 0.5
    lead_min = getattr(t, "wake_window_preempt_lead_min", 20.0) if t else 20.0
    bin_start, _ = _bin_bounds(cluster.bin_index, cluster.bin_min)
    lead_start = bin_start - int(lead_min)
    if lead_start < 0:
        lead_start += 1440

    direction = "cool"   # default: hot sleeper, this controller's evidence-backed default nudge
    nudge_f = min(cap, 0.5)
    if top is not None and top["type"] == "continuous" and top.get("point_biserial_r") is not None \
            and top["driver"] in ("bed_setpoint_f", "room_temp_f") and top.get("mean_diff"):
        r = top["point_biserial_r"]
        diff = abs(top["mean_diff"]["diff"])
        direction = "cool" if r > 0 else "warm"
        nudge_f = min(cap, max(0.2, round(diff / 2.0, 2)))

    text = (f"You tend to wake ~{_clock(bin_start)} out of {cluster.stage_exited.upper()} "
            f"({cluster.nights_woke}/{cluster.nights_observed} nights, "
            f"{cluster.confidence_label} confidence)")
    if top is not None:
        note = _driver_note(top)
        if note:
            text += f"; {note}"
    text += f" — try a gentle {nudge_f:.1f}°F {direction} nudge starting ~{_clock(lead_start)}."

    return {
        "text": text, "action": direction, "nudge_f": round(nudge_f, 2),
        "start_clock_time": _clock(lead_start), "window_clock_time": _clock(bin_start),
        "top_driver": top["driver"] if top is not None else None,
    }


def wake_analysis_report(
    repo, lookback_nights: int = 60, cfg=None, bin_min: int = DEFAULT_BIN_MIN,
    max_windows: int = 6,
) -> dict:
    """The full 3AM WAKE targeted analysis report: recurring wake windows (clock time + stage
    + frequency + confidence), each window's top correlated drivers, and a bounded suggestion
    once confidence clears the bar. Deterministic given the stored data (no wall-clock read)."""
    clusters = cluster_awakenings(repo, lookback_nights, bin_min)
    dates = _night_dates(repo, lookback_nights)
    windows = []
    for c in clusters[:max_windows]:
        corr = correlate_wakes(repo, c, lookback_nights)
        windows.append({
            "window": c.to_dict(),
            "drivers": corr["drivers"],
            "suggestion": _suggestion_for(c, corr, cfg),
        })
    return {
        "lookback_nights": lookback_nights,
        "n_nights_available": len(dates),
        "bin_minutes": bin_min,
        "recurring_windows": windows,
        "n_recurring_windows": len(windows),
        "note": ("Recurring wake windows are clock-time bins where you disproportionately "
                 "wake, clustered from logged awakenings + the sleep stage exited. Confidence "
                 "rises only with repeated evidence; nothing below the suggestion threshold "
                 "gets a concrete action. Drivers are exploratory (n-of-1) correlations, not "
                 "proven causes."),
    }


# ------------------------------------------------------------------- controller pre-emption gate


def should_preempt_window(recurring_windows, now: datetime, cfg=None) -> Optional[dict]:
    """Do-no-harm gate: given the ``recurring_windows`` from ``wake_analysis_report`` and the
    current clock time, decide whether a HIGH-CONFIDENCE recurring wake window justifies a
    small pre-emptive smoothing right now. Returns None (log-only, no thermal effect) unless
    ``wake_window_preempt_enabled`` is on AND the window's nights-observed/confidence both clear
    the configured gates. ``now`` must be passed in explicitly -- this never reads the wall
    clock itself, so it stays deterministic and testable.
    """
    t = getattr(cfg, "tunables", None)
    if t is not None and not getattr(t, "wake_window_preempt_enabled", True):
        return None
    min_nights = getattr(t, "wake_window_preempt_min_nights", 10) if t else 10
    conf_min = getattr(t, "wake_window_preempt_confidence_min", 0.55) if t else 0.55
    lead_min = getattr(t, "wake_window_preempt_lead_min", 20.0) if t else 20.0

    minute = now.hour * 60 + now.minute
    for w in recurring_windows or []:
        win = w.get("window", w)
        if (win.get("nights_observed") or 0) < min_nights:
            continue
        if (win.get("confidence") or 0.0) < conf_min:
            continue
        start = win.get("bin_start_min")
        end = win.get("bin_end_min")
        if start is None or end is None:
            continue
        lead_start = start - int(lead_min)
        if lead_start < 0:
            lead_start += 1440
        end_mod = end % 1440
        hit = (lead_start <= minute < end_mod) if lead_start <= end_mod else (
            minute >= lead_start or minute < end_mod)
        if hit:
            return {
                "label": win.get("label"), "confidence": win.get("confidence"),
                "stage_exited": win.get("stage_exited"),
                "reason": f"recurring_wake_window:{win.get('label')}",
            }
    return None
