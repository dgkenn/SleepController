"""Learn the wake-window temperature ramp from WAKE-QUALITY feedback.

``wake_ramp_f`` shapes the end of the night (warming toward the wake time). It is deliberately
NOT in the action-value learner's set: that learner scores against sleep-architecture outcomes,
which the wake ramp doesn't move — the wake ramp affects how you *wake* (grogginess), a separate
signal. So it gets its own small learner here, driven by the subjective grogginess check-in.

Scale-agnostic and conservative (do-no-harm): it only nudges when nights at warmer vs cooler wake
ramps show a grogginess difference of at least half a standard deviation, and only by a bounded
step within the wake-ramp safety bounds. With no real variation tried yet, it holds (the wake-ramp
n-of-1 experiment can introduce variation to learn from).
"""

from __future__ import annotations

import statistics

WAKE_RAMP_BOUNDS = (70.0, 86.0)  # mirrors ml.actions.KNOB_BOUNDS["wake_ramp_f"]


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def learn_wake_ramp(repo, cfg, current_f: float | None = None,
                    min_nights: int = 6, step: float = 1.0) -> float:
    """Return an adjusted wake-ramp °F that lowers morning grogginess, or the current value when
    the evidence is thin/weak."""
    lo, hi = WAKE_RAMP_BOUNDS
    base = current_f if current_f is not None else cfg.tunables.wake_ramp_temp_f
    try:
        sp_by_v = repo.setpoints_by_version()
    except Exception:
        return base
    pairs = []  # (wake_ramp_f used, grogginess)
    for night in repo.recent_nights(21):
        ctx = repo.get_context(night.date)
        g = getattr(ctx, "grogginess", None) if ctx is not None else None
        v = getattr(night, "setpoint_version", None)
        sp = sp_by_v.get(v) if v is not None else None
        wr = getattr(sp, "wake_ramp_f", None) if sp is not None else None
        if g is not None and wr is not None:
            pairs.append((float(wr), float(g)))
    if len(pairs) < min_nights:
        return base

    wrs = [p[0] for p in pairs]
    grog = [p[1] for p in pairs]
    gsd = statistics.pstdev(grog)
    if gsd < 1e-9:
        return base  # no grogginess variation -> nothing to learn
    median_wr = statistics.median(wrs)
    warm = [g for wr, g in pairs if wr >= median_wr]
    cool = [g for wr, g in pairs if wr < median_wr]
    if len(warm) < 2 or len(cool) < 2:
        return base  # not enough variation in the wake ramp actually tried
    # grogginess(warm ramps) - grogginess(cool ramps); require >= 0.5 SD to act
    d = statistics.fmean(warm) - statistics.fmean(cool)
    if abs(d) < 0.5 * gsd:
        return base
    direction = -1.0 if d > 0 else 1.0   # warmer ramp -> groggier => go cooler, and vice-versa
    return round(_clamp(base + direction * step, lo, hi), 2)
