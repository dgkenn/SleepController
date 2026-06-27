"""Personalize the perfect-sleep WEIGHTS from the user's own felt outcomes.

The per-mode benchmark weights start from the evidence (NSF 2017 / Ohayon 2004). But which
metrics matter most for *staying-asleep-and-feeling-good* varies by person. A scoring function
can't be A/B-tested behaviorally (changing a weight doesn't change what the bed does), so we tune
it by REVEALED PREFERENCE: fit the weights so the index best tracks the user's own subjective
quality / next-day performance check-ins — shrunk toward the evidence prior so it stays
conservative, needs real data, and never drifts far from the literature.

Method (robust, pure-python): for each metric, correlate its nightly normalized score with the
subjective outcome (both z-scored, so it's scale-agnostic); up-weight metrics that predict feeling
good for THIS person, down-weight those that don't, by a bounded, data-shrunk amount; floor the
continuity metrics (WASO/awakenings) so sleep MAINTENANCE — the #1 priority — can never be learned
away; renormalize to the prior's total. With too little check-in data, returns the prior unchanged.
"""

from __future__ import annotations

import statistics
from typing import Optional

from sleepctl.benchmarks import NightMode, perfect_sleep_index, targets_for

_CONTINUITY = {"waso", "awakenings"}


def learn_perfect_weights(repo, mode: NightMode = NightMode.NORMAL, min_nights: int = 14,
                          max_shift: float = 0.5) -> dict:
    """Return personalized (or, if data is thin, the prior) scoring weights for ``mode``."""
    prior = dict(targets_for(mode).weights)
    rows = []  # (components dict, subjective y)
    for night in repo.recent_nights(60):
        ctx = repo.get_context(getattr(night, "date", None)) if hasattr(repo, "get_context") else None
        y = None
        if ctx is not None:
            y = getattr(ctx, "subjective_quality", None)
            if y is None:
                y = getattr(ctx, "daytime_performance", None)
        if y is None:
            continue
        try:
            comps = perfect_sleep_index(night, mode)["components"]
        except Exception:
            continue
        rows.append((comps, float(y)))

    if len(rows) < min_nights:
        return prior
    ys = [r[1] for r in rows]
    sdy = statistics.pstdev(ys)
    if sdy < 1e-9:
        return prior  # no variation in felt quality -> nothing to fit
    my = statistics.fmean(ys)
    n = len(rows)
    shrink = max(0.0, min(1.0, (n - min_nights + 1) / float(min_nights)))  # grows with data

    learned = dict(prior)
    for metric in prior:
        xs = [(c[metric], y) for c, y in rows if c.get(metric) is not None]
        if len(xs) < min_nights:
            continue
        mx = statistics.fmean([x for x, _ in xs])
        sdx = statistics.pstdev([x for x, _ in xs])
        if sdx < 1e-9:
            continue  # metric never varied -> can't attribute felt quality to it
        corr = statistics.fmean(((x - mx) / sdx) * ((y - my) / sdy) for x, y in xs)
        corr = max(-1.0, min(1.0, corr))
        mult = 1.0 + max_shift * shrink * corr
        floor = 0.6 if metric in _CONTINUITY else 0.3   # maintenance can't be learned away
        learned[metric] = prior[metric] * max(floor, mult)

    s_prior, s_new = sum(prior.values()), sum(learned.values())
    if s_new > 0:                                       # preserve the prior's total scale
        learned = {k: round(v * s_prior / s_new, 4) for k, v in learned.items()}
    return learned


def personalized_targets(repo, mode: NightMode = NightMode.NORMAL,
                         total_sleep_target_min: Optional[int] = None):
    """``targets_for(mode)`` with the user's revealed-preference weights applied (evidence prior
    when data is thin). Pass to ``perfect_sleep_index(..., targets=...)`` / ``morning_readiness``."""
    from dataclasses import replace
    base = targets_for(mode) if total_sleep_target_min is None \
        else targets_for(mode, total_sleep_target_min)
    return replace(base, weights=learn_perfect_weights(repo, mode))
