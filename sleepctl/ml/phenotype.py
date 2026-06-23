"""Phase-1 phenotype: which factors correlate with bad nights.

Computes simple Pearson correlations between engineered/context features and the night's
outcome (reward or wake events) across history. Interpretable, robust, and a good first
read on the user's sleep phenotype before the action-value learner has enough data.
"""

from __future__ import annotations

from typing import Optional

from sleepctl.ml.features import engineer_features
from sleepctl.storage.repository import Repository


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    n = len(xs)
    if n < 4:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    if sx == 0 or sy == 0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / (sx * sy)


def correlate_with_outcome(repo: Repository, target: str = "outcome_score") -> list[tuple]:
    """Return [(feature, r, n)] sorted by |r| desc, correlating features vs the target."""
    nights = {n.date: n for n in repo.all_nights()}
    feats = engineer_features(repo)
    # collect target per date
    targets = {}
    for date, n in nights.items():
        y = n.outcome_score if target == "outcome_score" else getattr(n, target, None)
        if y is not None:
            targets[date] = float(y)

    # union of feature names
    names = set()
    for d in feats.values():
        names.update(d.keys())

    results = []
    for name in names:
        xs, ys = [], []
        for date, y in targets.items():
            v = feats.get(date, {}).get(name)
            if v is not None:
                xs.append(float(v))
                ys.append(y)
        r = _pearson(xs, ys)
        if r is not None:
            results.append((name, round(r, 3), len(xs)))
    results.sort(key=lambda t: abs(t[1]), reverse=True)
    return results
