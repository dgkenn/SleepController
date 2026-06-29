"""Learn the IDEAL sleep architecture itself — per person, per situation — from felt recovery.

`perfect_weights` learns *which metrics matter most* for this user. This goes one step further and
learns *what the target levels should be*: is YOUR best-feeling night at 22 % deep or 18 %? at
24 % REM or 20 %? It does that by revealed preference — the architecture present on the nights you
rated/performed best becomes your personal ideal — but it is **shrunk hard toward the evidence
prior and bounded to a tight band around it** (default ±4 percentage points), so it personalizes
without ever drifting away from the literature, and only after enough check-ins.

Continuity / maintenance targets are deliberately NOT learned here (sleep maintenance is the #1
priority and stays anchored to the evidence floor). With too little data it returns the prior.

Pure-python, robust to missing/degenerate data. Mirrors `perfect_weights`' shrink/bound style.
"""

from __future__ import annotations

import statistics

from sleepctl.benchmarks import NightMode, targets_for

# How far the learned ideal may move from the evidence prior (fraction of total sleep).
_MAX_SHIFT = 0.04
# Hard evidence band each target must stay within, regardless of data (safety rails).
_BAND = {"deep_pct_ideal": (0.14, 0.26), "deep_pct_min": (0.12, 0.24),
         "rem_pct_ideal": (0.16, 0.30), "rem_pct_min": (0.14, 0.26)}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def learn_ideal_architecture(repo, mode: NightMode = NightMode.NORMAL, min_nights: int = 14,
                             max_shift: float = _MAX_SHIFT) -> dict:
    """Return THIS person's learned ideal {deep_pct_ideal/min, rem_pct_ideal/min} for ``mode`` —
    or the evidence prior if the check-in data is too thin to fit."""
    base = targets_for(mode)
    prior = {"deep_pct_ideal": base.deep_pct_ideal, "deep_pct_min": base.deep_pct_min,
             "rem_pct_ideal": base.rem_pct_ideal, "rem_pct_min": base.rem_pct_min}

    rows = []  # (deep_frac, rem_frac, felt y)
    for night in repo.recent_nights(60):
        tst = getattr(night, "total_sleep_min", None)
        if not tst:
            continue
        ctx = repo.get_context(getattr(night, "date", None)) if hasattr(repo, "get_context") else None
        y = None
        if ctx is not None:
            y = getattr(ctx, "subjective_quality", None)
            if y is None:
                y = getattr(ctx, "daytime_performance", None)
        if y is None:
            continue
        deep = float(night.deep_min or 0) / float(tst)
        rem = float(night.rem_min or 0) / float(tst)
        rows.append((deep, rem, float(y)))

    if len(rows) < min_nights:
        return prior
    ys = [r[2] for r in rows]
    my, sdy = statistics.fmean(ys), statistics.pstdev(ys)
    if sdy < 1e-9:
        return prior                       # no variation in felt quality -> nothing to fit
    shrink = max(0.0, min(1.0, (len(rows) - min_nights + 1) / float(min_nights)))

    learned = dict(prior)
    for idx, ideal_key, min_key in ((0, "deep_pct_ideal", "deep_pct_min"),
                                    (1, "rem_pct_ideal", "rem_pct_min")):
        xs = [(r[idx], r[2]) for r in rows]
        # felt-recovery-weighted mean architecture: nights you felt ABOVE your average pull the
        # personal optimum toward the stage % they had (z>0 weight; below-average nights ignored).
        w = [(max(0.0, (y - my) / sdy), x) for x, y in xs]
        wsum = sum(weight for weight, _ in w)
        if wsum < 1e-9:
            continue
        personal_opt = sum(weight * x for weight, x in w) / wsum
        move = _clamp(personal_opt - prior[ideal_key], -max_shift, max_shift) * shrink
        learned[ideal_key] = round(_clamp(prior[ideal_key] + move, *_BAND[ideal_key]), 3)
        # shift the floor in lockstep so the ideal-vs-floor gap (and maintenance safety) is preserved
        learned[min_key] = round(_clamp(prior[min_key] + move, *_BAND[min_key]), 3)
    return learned


def is_personalized(learned: dict, mode: NightMode = NightMode.NORMAL) -> bool:
    base = targets_for(mode)
    return (abs(learned.get("deep_pct_ideal", base.deep_pct_ideal) - base.deep_pct_ideal) >= 0.005
            or abs(learned.get("rem_pct_ideal", base.rem_pct_ideal) - base.rem_pct_ideal) >= 0.005)
