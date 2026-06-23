"""Per-outcome ridge response model.

For each sleep outcome we fit a ridge-regularized linear model mapping
[setpoint knobs + context] -> outcome, with feature standardization and y-centering (so no
explicit intercept). Pure Python; small feature set. The recommender then optimizes the
setpoint against these models.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean, pstdev
from typing import Optional

from sleepctl.ml.dataset import CONTEXT_FEATURES, OUTCOMES, SETPOINT_FEATURES, FeatureRow
from sleepctl.ml.linalg import ridge_fit

FEATURES = SETPOINT_FEATURES + CONTEXT_FEATURES


def _mean_present(values: list) -> float:
    present = [v for v in values if v is not None]
    return fmean(present) if present else 0.0


@dataclass
class _OutcomeModel:
    features: list[str]
    means: list[float]
    stds: list[float]
    coef: list[float]
    y_mean: float
    resid_std: float = 0.0   # in-sample residual spread -> prediction uncertainty
    n: int = 0               # training rows -> data support

    def predict(self, x: dict) -> float:
        total = self.y_mean
        for fi, name in enumerate(self.features):
            xv = x.get(name)
            if xv is None or self.stds[fi] == 0.0:
                continue
            total += self.coef[fi] * ((xv - self.means[fi]) / self.stds[fi])
        return total

    def confidence(self) -> float:
        """0..1 confidence growing with sample support and shrinking with residual noise."""
        support = min(1.0, self.n / 21.0)            # ~3 weeks of clean nights -> full
        # normalize residual spread against the outcome's own scale (y std proxy = resid+eps)
        noise = self.resid_std / (abs(self.y_mean) + self.resid_std + 1e-6)
        return max(0.0, support * (1.0 - min(1.0, noise)))


class SetpointModel:
    """Fits one ridge model per outcome; predicts outcomes for a candidate setpoint."""

    def __init__(self, lam: float = 1.0) -> None:
        self.lam = lam
        self.models: dict[str, _OutcomeModel] = {}
        self.n_rows = 0
        self.features = FEATURES

    @staticmethod
    def _usable(row: FeatureRow, target: str) -> bool:
        # need the target + the controllable setpoint knobs; context may be missing (imputed)
        if getattr(row, target) is None:
            return False
        return all(getattr(row, f) is not None for f in SETPOINT_FEATURES)

    def fit(self, rows: list[FeatureRow]) -> "SetpointModel":
        self.n_rows = len(rows)
        for target in OUTCOMES:
            usable = [r for r in rows if self._usable(r, target)]
            if len(usable) < 3:
                continue
            cols = {f: [getattr(r, f) for r in usable] for f in FEATURES}
            # impute missing features with the column mean (-> standardized 0, no effect)
            means = [_mean_present(cols[f]) for f in FEATURES]
            stds = [pstdev([v if v is not None else means[i] for v in cols[f]])
                    for i, f in enumerate(FEATURES)]
            X = []
            for r in usable:
                row_x = []
                for i, f in enumerate(FEATURES):
                    v = getattr(r, f)
                    v = means[i] if v is None else v
                    row_x.append(((v - means[i]) / stds[i]) if stds[i] > 0 else 0.0)
                X.append(row_x)
            y = [getattr(r, target) for r in usable]
            y_mean = fmean(y)
            yc = [yi - y_mean for yi in y]
            coef = ridge_fit(X, yc, self.lam)
            # residual spread for uncertainty
            resids = []
            for r, yi in zip(X, yc):
                pred = sum(coef[i] * r[i] for i in range(len(FEATURES)))
                resids.append(yi - pred)
            resid_std = pstdev(resids) if len(resids) >= 2 else 0.0
            self.models[target] = _OutcomeModel(
                FEATURES, means, stds, coef, y_mean, resid_std=resid_std, n=len(usable)
            )
        return self

    def predict_outcomes(self, x: dict) -> dict[str, float]:
        return {name: m.predict(x) for name, m in self.models.items()}

    def confidence(self) -> float:
        """Overall model confidence = mean confidence across trained outcomes."""
        if not self.models:
            return 0.0
        return fmean([m.confidence() for m in self.models.values()])

    def trained_outcomes(self) -> list[str]:
        return list(self.models.keys())
