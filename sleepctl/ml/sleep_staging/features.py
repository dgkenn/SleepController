"""Multi-scale trailing-window feature extraction for wearable sleep staging (v2).

PURE standard library (``math``/``statistics``/``bisect`` only — NO numpy/pandas) so the
*exact* same function runs in training (PSG-aligned PhysioNet data) and at inference
(sparse frames streamed from a Polar Verity Sense in the live controller).

What changed vs v1 (which used one 10-minute window and scored ~kappa 0.33):

1. **Multi-scale context.** Every summary is computed over 2 / 5 / 10 / 30 minute trailing
   windows instead of a single 10 min window, because sleep stage is strongly
   autocorrelated on several timescales at once.
2. **Lags and deltas.** The 5-minute mean minus the 5-minute mean as of 5 / 10 / 20 minutes
   ago, plus rolling min/max contrasts, so the model can see *direction of change*.
3. **Per-recording normalization.** Absolute bpm varies enormously between people, so the
   biggest single win is expressing the current HR relative to *this* recording's own
   distribution: z-score vs night median/IQR, percentile rank within the night-so-far,
   and offsets from the night 10th/20th percentile. Same idea for activity.
4. **Real actigraphy.** ``activity_samples`` carry per-epoch actigraphy counts (PIM), not
   Apple-Watch step counts (which are ~96% zeros overnight and carried almost no signal).

Ordered feature lists (the column order is load-bearing — models store their own
``feature_names`` and :func:`feature_vector` projects onto them):

    FEATURE_NAMES_HR        -- HR + clock features only (Verity Sense alone)
    FEATURE_NAMES_HRMOTION  -- the above plus actigraphy features

Normalization stats are passed in as an argument so that training and inference share one
code path; :func:`compute_norm_stats` derives them from a plain HR history (the controller
can call it on its own sample buffer). Missing values are imputed with 0.0 and flagged.
"""

from __future__ import annotations

import bisect
import math
import statistics
from typing import Dict, List, Optional, Sequence, Tuple, Union

# --- window geometry -------------------------------------------------------------------
WINDOWS_MIN: Tuple[float, ...] = (2.0, 5.0, 10.0, 30.0)
LAGS_MIN: Tuple[float, ...] = (5.0, 10.0, 20.0)
LAG_WINDOW_MIN = 5.0
#: how far back any feature can look — callers may pre-slice their history to this.
MAX_LOOKBACK_S = 60.0 * (max(WINDOWS_MIN + LAGS_MIN) + LAG_WINDOW_MIN)
DEFAULT_WINDOW_S = 600.0  # kept for backwards compatibility with v1 callers

EPOCH_S = 30.0
#: actigraphy count above which an epoch counts as "moving" (PIM units)
ACT_ACTIVE_THRESHOLD = 2.0
N_QUANTILES = 41  # 2.5% resolution for percentile-rank features

Sample = Tuple[float, float]
ActSample = Sequence[float]  # (t, pim) or (t, pim, zcm, mad, std, pmax)


def _wtag(w: float) -> str:
    return str(int(w)) if float(w).is_integer() else str(w).replace(".", "p")


# --------------------------------------------------------------------------- name lists
def _hr_names() -> List[str]:
    names: List[str] = []
    for w in WINDOWS_MIN:
        t = _wtag(w)
        names += [
            f"hr_mean_w{t}",
            f"hr_std_w{t}",
            f"hr_min_w{t}",
            f"hr_max_w{t}",
            f"hr_range_w{t}",
            f"hr_slope_w{t}",
            f"hr_rmssd_w{t}",
        ]
    names.append("hr_n_samples")  # samples in the 10 min window (data-quality guard)
    for lag in LAGS_MIN:
        t = _wtag(lag)
        names += [f"hr_lag{t}_mean", f"hr_d{t}"]
    names += [
        "hr_mean5_minus_min30",
        "hr_mean5_minus_max30",
        "hr_min5_minus_min30",
    ]
    names += [
        "hrn_present",
        "hrn_z5",
        "hrn_z10",
        "hrn_z30",
        "hrn_minus_p10",
        "hrn_minus_p20",
        "hrn_minus_p50",
        "hrn_ratio_p50",
        "hrn_rank5",
        "hrn_rank30",
        "hrn_rank_min5",
        "hrn_std_ratio",
        "hrn_rmssd_ratio",
        "hrn_iqr",
    ]
    names += [
        "clock_minutes_since_start",
        "clock_norm_since_start",
        "clock_norm_sq",
        "clock_dist_from_mid",
        "clock_minutes_since_onset",
    ]
    return names


def _activity_scalefree_names() -> List[str]:
    """Activity features invariant to a monotone rescaling of the movement signal.

    The Polar Verity Sense exposes its own triaxial accelerometer over Polar's PMD BLE
    service, so deployment can feed actigraphy counts in the *same* units as training
    (see ``scripts/reduce_motion_activity.py``) — the unit-dependent block below is
    therefore the primary path. These scale-free features are kept alongside it because
    they are what still transfers if the movement signal ever arrives on a different scale
    (a phone's 0..1 movement index, a different epoch length, a different sensor placement).
    """
    names: List[str] = ["act_present", "actn_present", "act_n_w10"]
    for w in WINDOWS_MIN:
        t = _wtag(w)
        names += [
            f"act_rank_w{t}",           # percentile rank of window mean in night-so-far
            f"act_rank_max_w{t}",       # percentile rank of window max
            f"act_robustz_w{t}",        # (mean - night p50) / night IQR
            f"act_frac_above_p50_w{t}",
            f"act_frac_above_p75_w{t}",
            f"act_logratio_p50_w{t}",   # log((mean+e)/(night p50+e))
        ]
    for lag in LAGS_MIN:
        t = _wtag(lag)
        names += [f"act_lag{t}_rank", f"act_drank{t}"]
    names += [
        "act_still_streak_min",   # consecutive minutes below the night's own low percentile
        "act_min_since_active",   # minutes since last epoch above the night's own p75
    ]
    return names


def _activity_absolute_names() -> List[str]:
    """Activity features in the raw units of the actigraphy counts.

    Valid because training and deployment share the modality: PIM/ZCM/MAD/std/peak reduced
    per 30 s epoch from a wrist triaxial accelerometer, exactly as
    ``scripts/reduce_motion_activity.py`` defines them (PIM/ZCM in counts, MAD/std/peak in
    g). Absolute magnitudes are highly discriminative — awake epochs run pim~11-47 /
    zcm~150-400 against pim~2 / zcm~0-4 asleep — so they lead, with the scale-free block
    above as the transfer-safe backup.
    """
    names: List[str] = []
    for w in WINDOWS_MIN:
        t = _wtag(w)
        names += [
            f"act_logmean_w{t}",
            f"act_logmax_w{t}",
            f"act_logstd_w{t}",
            f"act_frac_active_w{t}",
        ]
    names += [
        "actaux_present",
        "actaux_logzcm5",
        "actaux_logzcm30",
        "actaux_logmad5",
        "actaux_logstd5",
        "actaux_logpmax5",
    ]
    return names


FEATURE_NAMES_HR: List[str] = _hr_names()
ACT_FEATURES_SCALEFREE: List[str] = _activity_scalefree_names()
ACT_FEATURES_ABSOLUTE: List[str] = _activity_absolute_names()

#: primary HR+motion column order: unit-matched actigraphy counts AND their scale-free
#: normalizations. Requires ``(t, pim, zcm, mad, std, pmax)`` activity samples.
FEATURE_NAMES_HRMOTION: List[str] = (
    FEATURE_NAMES_HR + ACT_FEATURES_SCALEFREE + ACT_FEATURES_ABSOLUTE
)
#: transfer-safe subset: no feature depends on the movement signal's units, so a model
#: trained on it still works if the movement channel is a different scale entirely.
FEATURE_NAMES_HRMOTION_SCALEFREE: List[str] = FEATURE_NAMES_HR + ACT_FEATURES_SCALEFREE
#: backwards-compatible alias (the "with absolute counts" list is now the primary one)
FEATURE_NAMES_HRMOTION_ABS: List[str] = FEATURE_NAMES_HRMOTION


# --------------------------------------------------------------------------- small utils
def _linear_slope(ts: Sequence[float], ys: Sequence[float]) -> float:
    """Least-squares slope of ys vs ts (units of y per unit t); 0.0 if degenerate."""
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


def percentile(sorted_vals: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile of an already-sorted sequence."""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_vals[0])
    rank = (pct / 100.0) * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return float(sorted_vals[lo]) * (1.0 - frac) + float(sorted_vals[hi]) * frac


def quantile_grid(sorted_vals: Sequence[float], n: int = N_QUANTILES) -> List[float]:
    """``n`` evenly spaced quantiles (0..100 inclusive) — a compact stand-in for the CDF."""
    if not sorted_vals:
        return []
    return [percentile(sorted_vals, 100.0 * i / (n - 1)) for i in range(n)]


def percentile_rank(grid: Sequence[float], value: float) -> float:
    """Rank of ``value`` in [0, 1] within the distribution summarized by ``grid``."""
    n = len(grid)
    if n < 2:
        return 0.5
    if value <= grid[0]:
        return 0.0
    if value >= grid[-1]:
        return 1.0
    i = bisect.bisect_left(grid, value)
    lo = grid[i - 1]
    hi = grid[i]
    frac = 0.0 if hi <= lo else (value - lo) / (hi - lo)
    return (i - 1 + frac) / (n - 1)


def _aval(s: ActSample) -> float:
    """Primary actigraphy channel (PIM) of an activity sample."""
    return float(s[1])


def _window(samples: Optional[Sequence[Sequence[float]]], start: float, end: float):
    """Samples with ``start <= t <= end`` (input need not be sorted)."""
    if not samples:
        return []
    return [s for s in samples if start <= s[0] <= end]


# --------------------------------------------------------------- per-recording normalization
def compute_norm_stats(
    hr_samples: Optional[Sequence[Sample]] = None,
    activity_samples: Optional[Sequence[ActSample]] = None,
    n_quantiles: int = N_QUANTILES,
) -> Dict[str, object]:
    """Per-recording normalization statistics from an HR (and optional activity) history.

    Pure python so the live controller can derive them from its own sample buffer:
    ``compute_norm_stats(hr_buffer)`` → pass the result straight into
    :func:`compute_features`. Use the *night so far* (causal) history — that is exactly how
    the training rows are built, so train/inference stay matched.
    """
    return stats_from_sorted(
        sorted(float(v) for _, v in (hr_samples or [])),
        sorted(_aval(s) for s in (activity_samples or [])),
        n_quantiles=n_quantiles,
    )


def stats_from_sorted(
    hr_sorted: Sequence[float],
    act_sorted: Sequence[float] = (),
    n_quantiles: int = N_QUANTILES,
) -> Dict[str, object]:
    """Same statistics as :func:`compute_norm_stats`, from already-sorted value lists.

    Lets the training loader maintain one incrementally-sorted "night so far" list instead
    of re-sorting per epoch, while producing byte-identical stats.
    """
    stats: Dict[str, object] = {}
    vals = hr_sorted
    if vals:
        p25 = percentile(vals, 25.0)
        p75 = percentile(vals, 75.0)
        stats.update(
            hr_n=float(len(vals)),
            hr_mean=statistics.fmean(vals),
            hr_std=statistics.pstdev(vals) if len(vals) >= 2 else 0.0,
            hr_p10=percentile(vals, 10.0),
            hr_p20=percentile(vals, 20.0),
            hr_p25=p25,
            hr_p50=percentile(vals, 50.0),
            hr_p75=p75,
            hr_p90=percentile(vals, 90.0),
            hr_iqr=max(p75 - p25, 0.0),
            hr_q=quantile_grid(vals, n_quantiles),
        )
    avals = act_sorted
    if avals:
        ap25 = percentile(avals, 25.0)
        ap75 = percentile(avals, 75.0)
        stats.update(
            act_n=float(len(avals)),
            act_mean=statistics.fmean(avals),
            act_p25=ap25,
            act_p50=percentile(avals, 50.0),
            act_p75=ap75,
            act_p90=percentile(avals, 90.0),
            act_iqr=max(ap75 - ap25, 0.0),
            act_q=quantile_grid(avals, n_quantiles),
        )
    return stats


# --------------------------------------------------------------------------- main entry
def compute_features(
    hr_samples: Optional[Sequence[Sample]],
    activity_samples: Optional[Sequence[ActSample]] = None,
    epoch_end_s: float = 0.0,
    *,
    norm_stats: Optional[Dict[str, object]] = None,
    hr_baseline: float = 0.0,
    minutes_since_start: Optional[float] = None,
    minutes_since_onset: Optional[float] = None,
    total_minutes: Optional[float] = None,
    include_activity: bool = True,
    window_s: float = DEFAULT_WINDOW_S,  # accepted for v1 call compatibility; unused
) -> Dict[str, float]:
    """Ordered multi-scale feature dict for the epoch ending at ``epoch_end_s``.

    Parameters
    ----------
    hr_samples : list of ``(t_seconds, bpm)``; only the trailing
        :data:`MAX_LOOKBACK_S` seconds matter, so callers may pre-slice for speed.
    activity_samples : list of ``(t_seconds, pim)`` — or
        ``(t, pim, zcm, mad, std, pmax)`` when the extra actigraphy channels exist.
    epoch_end_s : trailing windows all end here.
    norm_stats : output of :func:`compute_norm_stats` for this recording (night so far).
        When omitted, the normalized block is zeroed and ``hrn_present`` is 0.
    hr_baseline : legacy v1 argument; used as the night 20th-percentile fallback when
        ``norm_stats`` is not supplied.
    minutes_since_start / minutes_since_onset / total_minutes : clock context.
    include_activity : emit the actigraphy block (HR+motion variant).
    """
    feats: Dict[str, float] = {}
    end = float(epoch_end_s)

    # one pre-filter over the widest lookback, then cheap sub-window scans
    hr_all = _window(hr_samples, end - MAX_LOOKBACK_S, end)
    hr_all.sort(key=lambda s: s[0])

    def _hr_window(start: float, stop: float) -> List[Sample]:
        return [(t, v) for t, v in hr_all if start <= t <= stop]

    def _hr_stats(win: Sequence[Sample]):
        if not win:
            return None
        ts = [t for t, _ in win]
        vs = [float(v) for _, v in win]
        n = len(vs)
        mean = statistics.fmean(vs)
        std = statistics.pstdev(vs) if n >= 2 else 0.0
        vmin = min(vs)
        vmax = max(vs)
        slope = _linear_slope(ts, vs) * 60.0  # bpm per minute
        if n >= 2:
            diffs = [vs[i + 1] - vs[i] for i in range(n - 1)]
            rmssd = math.sqrt(statistics.fmean([d * d for d in diffs]))
        else:
            rmssd = 0.0
        return dict(n=n, mean=mean, std=std, min=vmin, max=vmax,
                    range=vmax - vmin, slope=slope, rmssd=rmssd)

    per_window: Dict[float, Optional[dict]] = {}
    for w in WINDOWS_MIN:
        st = _hr_stats(_hr_window(end - 60.0 * w, end))
        per_window[w] = st
        tag = _wtag(w)
        feats[f"hr_mean_w{tag}"] = float(st["mean"]) if st else 0.0
        feats[f"hr_std_w{tag}"] = float(st["std"]) if st else 0.0
        feats[f"hr_min_w{tag}"] = float(st["min"]) if st else 0.0
        feats[f"hr_max_w{tag}"] = float(st["max"]) if st else 0.0
        feats[f"hr_range_w{tag}"] = float(st["range"]) if st else 0.0
        feats[f"hr_slope_w{tag}"] = float(st["slope"]) if st else 0.0
        feats[f"hr_rmssd_w{tag}"] = float(st["rmssd"]) if st else 0.0

    w10 = per_window.get(10.0)
    feats["hr_n_samples"] = float(w10["n"]) if w10 else 0.0

    w5 = per_window.get(5.0)
    w30 = per_window.get(30.0)
    mean5 = float(w5["mean"]) if w5 else 0.0

    for lag in LAGS_MIN:
        tag = _wtag(lag)
        lag_end = end - 60.0 * lag
        st = _hr_stats(_hr_window(lag_end - 60.0 * LAG_WINDOW_MIN, lag_end))
        lag_mean = float(st["mean"]) if st else 0.0
        feats[f"hr_lag{tag}_mean"] = lag_mean
        feats[f"hr_d{tag}"] = (mean5 - lag_mean) if (st and w5) else 0.0

    if w5 and w30:
        feats["hr_mean5_minus_min30"] = mean5 - float(w30["min"])
        feats["hr_mean5_minus_max30"] = mean5 - float(w30["max"])
        feats["hr_min5_minus_min30"] = float(w5["min"]) - float(w30["min"])
    else:
        feats["hr_mean5_minus_min30"] = 0.0
        feats["hr_mean5_minus_max30"] = 0.0
        feats["hr_min5_minus_min30"] = 0.0

    # --- per-recording normalized HR block (the personalization win) --------------------
    ns = norm_stats or {}
    have_hr_stats = bool(ns) and "hr_p50" in ns
    if have_hr_stats:
        p10 = float(ns["hr_p10"])
        p20 = float(ns["hr_p20"])
        p50 = float(ns["hr_p50"])
        iqr = max(float(ns.get("hr_iqr", 0.0)), 1e-6)
        night_std = max(float(ns.get("hr_std", 0.0)), 0.5)
        grid = list(ns.get("hr_q") or [])
        mean10 = float(w10["mean"]) if w10 else mean5
        mean30 = float(w30["mean"]) if w30 else mean5
        feats["hrn_present"] = 1.0
        feats["hrn_z5"] = (mean5 - p50) / iqr
        feats["hrn_z10"] = (mean10 - p50) / iqr
        feats["hrn_z30"] = (mean30 - p50) / iqr
        feats["hrn_minus_p10"] = mean5 - p10
        feats["hrn_minus_p20"] = mean5 - p20
        feats["hrn_minus_p50"] = mean5 - p50
        feats["hrn_ratio_p50"] = mean5 / p50 if p50 > 0 else 0.0
        feats["hrn_rank5"] = percentile_rank(grid, mean5) if grid else 0.5
        feats["hrn_rank30"] = percentile_rank(grid, mean30) if grid else 0.5
        feats["hrn_rank_min5"] = (
            percentile_rank(grid, float(w5["min"])) if (grid and w5) else 0.5
        )
        feats["hrn_std_ratio"] = (float(w5["std"]) / night_std) if w5 else 0.0
        feats["hrn_rmssd_ratio"] = (float(w5["rmssd"]) / night_std) if w5 else 0.0
        feats["hrn_iqr"] = float(ns.get("hr_iqr", 0.0))
    else:
        # v1-style fallback: only an externally supplied baseline is available
        feats["hrn_present"] = 0.0
        feats["hrn_z5"] = 0.0
        feats["hrn_z10"] = 0.0
        feats["hrn_z30"] = 0.0
        feats["hrn_minus_p10"] = 0.0
        feats["hrn_minus_p20"] = (mean5 - float(hr_baseline)) if hr_baseline else 0.0
        feats["hrn_minus_p50"] = 0.0
        feats["hrn_ratio_p50"] = 0.0
        feats["hrn_rank5"] = 0.0
        feats["hrn_rank30"] = 0.0
        feats["hrn_rank_min5"] = 0.0
        feats["hrn_std_ratio"] = 0.0
        feats["hrn_rmssd_ratio"] = 0.0
        feats["hrn_iqr"] = 0.0

    # --- clock -------------------------------------------------------------------------
    mss = (max(0.0, end) / 60.0) if minutes_since_start is None else float(minutes_since_start)
    feats["clock_minutes_since_start"] = mss
    if total_minutes and total_minutes > 0:
        norm = max(0.0, min(1.0, mss / float(total_minutes)))
    else:
        norm = max(0.0, min(1.0, mss / 480.0))  # nominal 8 h night
    feats["clock_norm_since_start"] = norm
    feats["clock_norm_sq"] = norm * norm
    feats["clock_dist_from_mid"] = abs(norm - 0.5)
    feats["clock_minutes_since_onset"] = (
        float(minutes_since_onset) if minutes_since_onset is not None else 0.0
    )

    if include_activity:
        _activity_block(feats, activity_samples, end, ns)

    return feats


def _activity_block(
    feats: Dict[str, float],
    activity_samples: Optional[Sequence[ActSample]],
    end: float,
    ns: Dict[str, object],
) -> None:
    act_all = _window(activity_samples, end - MAX_LOOKBACK_S, end)
    act_all = sorted(act_all, key=lambda s: s[0])

    def _act_window(start: float, stop: float):
        return [s for s in act_all if start <= s[0] <= stop]

    def _summ(win) -> Optional[dict]:
        if not win:
            return None
        vs = [_aval(s) for s in win]
        n = len(vs)
        return dict(
            n=n,
            mean=statistics.fmean(vs),
            max=max(vs),
            std=statistics.pstdev(vs) if n >= 2 else 0.0,
            frac_active=sum(1 for v in vs if v > ACT_ACTIVE_THRESHOLD) / n,
        )

    feats["act_present"] = 1.0 if act_all else 0.0

    have_act_stats = "act_p50" in ns
    grid = list(ns.get("act_q") or []) if have_act_stats else []
    a_p50 = float(ns.get("act_p50", 0.0)) if have_act_stats else 0.0
    a_p75 = float(ns.get("act_p75", 0.0)) if have_act_stats else 0.0
    a_p25 = float(ns.get("act_p25", 0.0)) if have_act_stats else 0.0
    a_p90 = float(ns.get("act_p90", 0.0)) if have_act_stats else 0.0
    a_iqr_raw = float(ns.get("act_iqr", 0.0)) if have_act_stats else 0.0
    a_iqr = max(a_iqr_raw, 1e-9)
    feats["actn_present"] = 1.0 if have_act_stats else 0.0
    # regularizer for the log-ratio, expressed in the recording's OWN units so the feature
    # is invariant to rescaling the movement signal (counts vs a 0..1 phone index)
    a_scale = max(a_p90, a_iqr_raw)
    eps = 0.05 * a_scale

    sums: Dict[float, Optional[dict]] = {}
    for w in WINDOWS_MIN:
        win = _act_window(end - 60.0 * w, end)
        st = _summ(win)
        sums[w] = st
        tag = _wtag(w)
        # --- unit-dependent (absolute) block ------------------------------------------
        feats[f"act_logmean_w{tag}"] = math.log1p(st["mean"]) if st else 0.0
        feats[f"act_logmax_w{tag}"] = math.log1p(st["max"]) if st else 0.0
        feats[f"act_frac_active_w{tag}"] = float(st["frac_active"]) if st else 0.0
        feats[f"act_logstd_w{tag}"] = math.log1p(st["std"]) if st else 0.0
        # --- scale-free block ----------------------------------------------------------
        if st and have_act_stats:
            vals = [_aval(s) for s in win]
            feats[f"act_rank_w{tag}"] = percentile_rank(grid, st["mean"]) if grid else 0.5
            feats[f"act_rank_max_w{tag}"] = percentile_rank(grid, st["max"]) if grid else 0.5
            feats[f"act_robustz_w{tag}"] = (float(st["mean"]) - a_p50) / a_iqr
            feats[f"act_frac_above_p50_w{tag}"] = sum(1 for v in vals if v > a_p50) / len(vals)
            feats[f"act_frac_above_p75_w{tag}"] = sum(1 for v in vals if v > a_p75) / len(vals)
            feats[f"act_logratio_p50_w{tag}"] = (
                math.log((float(st["mean"]) + eps) / (a_p50 + eps)) if eps > 0 else 0.0
            )
        else:
            feats[f"act_rank_w{tag}"] = 0.0
            feats[f"act_rank_max_w{tag}"] = 0.0
            feats[f"act_robustz_w{tag}"] = 0.0
            feats[f"act_frac_above_p50_w{tag}"] = 0.0
            feats[f"act_frac_above_p75_w{tag}"] = 0.0
            feats[f"act_logratio_p50_w{tag}"] = 0.0

    a10 = sums.get(10.0)
    feats["act_n_w10"] = float(a10["n"]) if a10 else 0.0
    a5 = sums.get(5.0)
    rank5 = feats.get("act_rank_w5", 0.0)

    for lag in LAGS_MIN:
        tag = _wtag(lag)
        lag_end = end - 60.0 * lag
        st = _summ(_act_window(lag_end - 60.0 * LAG_WINDOW_MIN, lag_end))
        if st and have_act_stats and grid:
            lr = percentile_rank(grid, st["mean"])
        else:
            lr = 0.0
        feats[f"act_lag{tag}_rank"] = lr
        feats[f"act_drank{tag}"] = (rank5 - lr) if (st and a5 and have_act_stats) else 0.0

    # --- scale-free continuity cues ----------------------------------------------------
    # "active" is defined by the recording's OWN p75 (falls back to an absolute PIM
    # threshold only when no night stats were supplied), so it transfers across sensors.
    act_thresh = a_p75 if have_act_stats else ACT_ACTIVE_THRESHOLD
    still_thresh = a_p25 if have_act_stats else ACT_ACTIVE_THRESHOLD
    cap_min = MAX_LOOKBACK_S / 60.0

    last_active = None
    for s in reversed(act_all):
        if _aval(s) > act_thresh:
            last_active = s[0]
            break
    if not act_all:
        feats["act_min_since_active"] = 0.0
    elif last_active is None:
        feats["act_min_since_active"] = cap_min
    else:
        feats["act_min_since_active"] = max(0.0, (end - last_active) / 60.0)

    # consecutive trailing epochs at/below the night's own low percentile, in minutes
    streak_start = end
    for s in reversed(act_all):
        if _aval(s) <= still_thresh:
            streak_start = s[0]
        else:
            break
    feats["act_still_streak_min"] = (
        max(0.0, (end - streak_start) / 60.0) if act_all else 0.0
    )

    # optional extra actigraphy channels (zcm/mad/std/pmax) when the sample tuples carry them
    aux5 = [s for s in act_all if len(s) >= 6 and s[0] >= end - 300.0]
    aux30 = [s for s in act_all if len(s) >= 6]
    if aux5:
        feats["actaux_present"] = 1.0
        feats["actaux_logzcm5"] = math.log1p(statistics.fmean([float(s[2]) for s in aux5]))
        feats["actaux_logmad5"] = math.log1p(statistics.fmean([float(s[3]) for s in aux5]) * 100.0)
        feats["actaux_logstd5"] = math.log1p(statistics.fmean([float(s[4]) for s in aux5]) * 100.0)
        feats["actaux_logpmax5"] = math.log1p(statistics.fmean([float(s[5]) for s in aux5]) * 100.0)
        feats["actaux_logzcm30"] = math.log1p(statistics.fmean([float(s[2]) for s in aux30]))
    else:
        feats["actaux_present"] = 0.0
        feats["actaux_logzcm5"] = 0.0
        feats["actaux_logmad5"] = 0.0
        feats["actaux_logstd5"] = 0.0
        feats["actaux_logpmax5"] = 0.0
        feats["actaux_logzcm30"] = 0.0


def feature_vector(feats: Dict[str, float], feature_names: Sequence[str]) -> List[float]:
    """Project a feature dict onto an ordered name list, imputing missing with 0.0."""
    return [float(feats.get(name, 0.0)) for name in feature_names]


__all__ = [
    "FEATURE_NAMES_HR",
    "FEATURE_NAMES_HRMOTION",
    "FEATURE_NAMES_HRMOTION_SCALEFREE",
    "FEATURE_NAMES_HRMOTION_ABS",
    "ACT_FEATURES_SCALEFREE",
    "ACT_FEATURES_ABSOLUTE",
    "compute_features",
    "compute_norm_stats",
    "stats_from_sorted",
    "feature_vector",
    "percentile",
    "percentile_rank",
    "quantile_grid",
    "MAX_LOOKBACK_S",
    "EPOCH_S",
]
