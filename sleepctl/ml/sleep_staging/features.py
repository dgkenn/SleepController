"""Trailing-window feature extraction for wearable sleep staging.

PURE standard library (``math``/``statistics`` only — NO numpy/pandas) so the *exact*
same function runs in training (dense per-second PSG-aligned data) and at inference
(sparse ~per-minute frames streamed from a Polar Verity Sense).

Given heart-rate samples ``(t_seconds, bpm)`` and optional activity samples
``(t_seconds, count)`` plus the epoch end-time, we summarize a trailing window (default
600 s) ending at that epoch. Two fixed, ordered feature lists are exposed:

    FEATURE_NAMES_HR        -- HR + clock features only (Polar Verity Sense alone)
    FEATURE_NAMES_HRMOTION  -- the above plus activity features (+ iPhone motion)

Missing values are imputed with 0.0; the model relies on standardization downstream.
"""

from __future__ import annotations

import math
import statistics
from typing import Dict, List, Optional, Sequence, Tuple

DEFAULT_WINDOW_S = 600.0
RECENT_WINDOW_S = 120.0  # "recent 2 min" sub-window

Sample = Tuple[float, float]

# --- HR + clock features (order is load-bearing; used as the model's column order) ---
# A few deliberate nonlinear / circadian terms (squares, U-shaped clock) give the linear
# softmax model extra capacity; they are pure functions of the same window so training
# and inference remain byte-identical.
FEATURE_NAMES_HR: List[str] = [
    "hr_mean",
    "hr_std",
    "hr_std_sq",
    "hr_min",
    "hr_max",
    "hr_range",
    "hr_slope_bpm_per_min",
    "hr_minus_baseline",
    "hr_minus_baseline_sq",
    "hr_frac_above_baseline5",
    "hr_rmssd",
    "hr_n_samples",
    "clock_minutes_since_start",
    "clock_norm_since_start",
    "clock_norm_sq",
    "clock_dist_from_mid",
    "clock_minutes_since_onset",
]

# Activity features appended for the HR+motion variant.
_ACTIVITY_FEATURES: List[str] = [
    "activity_present",
    "activity_window_sum",
    "activity_window_mean",
    "activity_recent2min_mean",
    "activity_any",
    "activity_log_sum",
]

FEATURE_NAMES_HRMOTION: List[str] = FEATURE_NAMES_HR + _ACTIVITY_FEATURES


def _linear_slope(ts: Sequence[float], ys: Sequence[float]) -> float:
    """Least-squares slope of ys vs ts, in units of y per unit t. 0.0 if degenerate."""
    n = len(ts)
    if n < 2:
        return 0.0
    mean_t = statistics.fmean(ts)
    mean_y = statistics.fmean(ys)
    num = 0.0
    den = 0.0
    for t, y in zip(ts, ys):
        dt = t - mean_t
        num += dt * (y - mean_y)
        den += dt * dt
    if den <= 0.0:
        return 0.0
    return num / den


def _window(samples: Optional[Sequence[Sample]], start: float, end: float) -> List[Sample]:
    if not samples:
        return []
    out = []
    for t, v in samples:
        if start <= t <= end:
            out.append((float(t), float(v)))
    out.sort(key=lambda s: s[0])
    return out


def compute_features(
    hr_samples: Optional[Sequence[Sample]],
    activity_samples: Optional[Sequence[Sample]],
    epoch_end_s: float,
    *,
    hr_baseline: float = 0.0,
    minutes_since_start: Optional[float] = None,
    minutes_since_onset: Optional[float] = None,
    total_minutes: Optional[float] = None,
    window_s: float = DEFAULT_WINDOW_S,
    include_activity: bool = True,
) -> "Dict[str, float]":
    """Compute the ordered trailing-window feature dict ending at ``epoch_end_s``.

    Parameters
    ----------
    hr_samples : list of (t_seconds, bpm) or None
    activity_samples : list of (t_seconds, count) or None
    epoch_end_s : trailing window ends here; window is [epoch_end_s - window_s, epoch_end_s]
    hr_baseline : the recording's sleep-HR baseline (e.g. 20th percentile); pass 0 to skip.
    minutes_since_start : clock feature; if None derived from epoch_end_s.
    minutes_since_onset : minutes since first sleep epoch (0 if unknown).
    total_minutes : recording length in minutes, for normalized clock.
    include_activity : emit activity features (HR+motion variant) when True.

    Returns an ordered dict following FEATURE_NAMES_HRMOTION (or FEATURE_NAMES_HR).
    """
    start = epoch_end_s - window_s
    hr = _window(hr_samples, start, epoch_end_s)

    feats: "Dict[str, float]" = {}

    if hr:
        ts = [t for t, _ in hr]
        vs = [v for _, v in hr]
        n = len(vs)
        mean = statistics.fmean(vs)
        std = statistics.pstdev(vs) if n >= 2 else 0.0
        vmin = min(vs)
        vmax = max(vs)
        # slope in bpm per MINUTE (ts are seconds)
        slope = _linear_slope(ts, vs) * 60.0
        minus_base = mean - hr_baseline
        thr = hr_baseline + 5.0
        frac_above = sum(1 for v in vs if v > thr) / n
        # successive-difference variability (RMSSD-like)
        if n >= 2:
            diffs = [vs[i + 1] - vs[i] for i in range(n - 1)]
            rmssd = math.sqrt(statistics.fmean([d * d for d in diffs]))
        else:
            rmssd = 0.0
    else:
        mean = std = vmin = vmax = slope = minus_base = frac_above = rmssd = 0.0
        n = 0

    feats["hr_mean"] = float(mean)
    feats["hr_std"] = float(std)
    feats["hr_std_sq"] = float(std * std)
    feats["hr_min"] = float(vmin)
    feats["hr_max"] = float(vmax)
    feats["hr_range"] = float(vmax - vmin)
    feats["hr_slope_bpm_per_min"] = float(slope)
    feats["hr_minus_baseline"] = float(minus_base)
    feats["hr_minus_baseline_sq"] = float(minus_base * minus_base)
    feats["hr_frac_above_baseline5"] = float(frac_above)
    feats["hr_rmssd"] = float(rmssd)
    feats["hr_n_samples"] = float(n)

    # --- clock features ---
    if minutes_since_start is None:
        mss = max(0.0, epoch_end_s) / 60.0
    else:
        mss = float(minutes_since_start)
    feats["clock_minutes_since_start"] = mss
    if total_minutes and total_minutes > 0:
        norm = max(0.0, min(1.0, mss / total_minutes))
    else:
        # normalize by a nominal 8h night so the feature stays O(1)
        norm = max(0.0, min(1.0, mss / 480.0))
    feats["clock_norm_since_start"] = norm
    feats["clock_norm_sq"] = norm * norm           # U-shaped: wake concentrates at ends
    feats["clock_dist_from_mid"] = abs(norm - 0.5)  # distance from mid-night
    feats["clock_minutes_since_onset"] = (
        float(minutes_since_onset) if minutes_since_onset is not None else 0.0
    )

    if include_activity:
        act = _window(activity_samples, start, epoch_end_s)
        present = 1.0 if activity_samples else 0.0
        if act:
            counts = [v for _, v in act]
            win_sum = float(sum(counts))
            win_mean = float(statistics.fmean(counts))
            recent = [v for t, v in act if t >= epoch_end_s - RECENT_WINDOW_S]
            recent_mean = float(statistics.fmean(recent)) if recent else 0.0
        else:
            win_sum = win_mean = recent_mean = 0.0
        feats["activity_present"] = present
        feats["activity_window_sum"] = win_sum
        feats["activity_window_mean"] = win_mean
        feats["activity_recent2min_mean"] = recent_mean
        feats["activity_any"] = 1.0 if win_sum > 0 else 0.0
        feats["activity_log_sum"] = math.log1p(win_sum)

    return feats


def feature_vector(feats: "Dict[str, float]", feature_names: Sequence[str]) -> List[float]:
    """Project a feature dict onto an ordered name list, imputing missing with 0.0."""
    return [float(feats.get(name, 0.0)) for name in feature_names]
