"""Per-user response-curve estimation via paired-night comparison.

Estimates how interventions relate to outcomes (e.g. cooling -> onset latency, cooling
-> deep, thermal stability -> fewer wake events) by comparing nights WITH vs WITHOUT a
given intervention. Effects are shrunk toward zero for small samples to avoid
overfitting; everything is explainable (sign + magnitude + n + confidence).
"""

from __future__ import annotations

import statistics
from datetime import timedelta
from typing import Optional

from sleepctl.models import CorrectionAction, Intervention, NightSummary

MIN_PAIRED = 3  # need this many nights on each side before trusting an effect


def _median(values: list[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def _effect(with_vals: list[float], without_vals: list[float]) -> dict:
    """Median difference (with - without), shrunk toward 0 for small samples."""
    n = min(len(with_vals), len(without_vals))
    mw, mwo = _median(with_vals), _median(without_vals)
    if mw is None or mwo is None or n == 0:
        return {"effect_size": 0.0, "n": n, "confidence": 0.0}
    raw = mw - mwo
    # shrinkage: confidence grows with n, ~0 below MIN_PAIRED
    confidence = max(0.0, min(1.0, (n - MIN_PAIRED + 1) / 5.0)) if n >= MIN_PAIRED else 0.0
    return {"effect_size": raw * confidence, "n": n, "confidence": round(confidence, 2)}


def _night_date(iv) -> str:
    """The night an intervention belongs to, keyed the way ``NightSummary.date`` is.

    2026-09-25 audit: this used the intervention's CALENDAR date, but nights are keyed by the
    date they STARTED with a noon cutoff (``CycleRunner.night_date``, which is also what the
    ``interventions.night_date`` column holds). Every cooling move made after midnight was
    credited to the NEXT night, so a night with no cooling could read as "cooled" and the night
    that was cooled as not. An explicit ``night_date`` on the record wins; otherwise the same noon-cutoff rule the
    column was written with (the only writer, ``CycleRunner.act``, stores
    ``night_date(now)`` alongside ``timestamp=now``) recovers it exactly.
    """
    explicit = getattr(iv, "night_date", None)
    if explicit:
        return str(explicit)
    return (iv.timestamp - timedelta(hours=12)).date().isoformat()


class ResponseEstimator:
    """Builds simple, robust response signals from history."""

    def estimate(
        self,
        history: list[NightSummary],
        interventions: list[Intervention],
    ) -> dict:
        # Map a night_date -> whether a cooling/stabilizing intervention happened.
        cooling_dates = {
            _night_date(iv)
            for iv in interventions
            if iv.action in (CorrectionAction.COOLER, CorrectionAction.ESCALATE)
        }

        def split(metric: str):
            with_vals, without_vals = [], []
            for n in history:
                v = getattr(n, metric, None)
                if v is None:
                    continue
                if n.date in cooling_dates:
                    with_vals.append(float(v))
                else:
                    without_vals.append(float(v))
            return with_vals, without_vals

        result = {}
        for name, metric in (
            ("cooling_vs_onset_latency", "sleep_onset_latency_min"),
            ("cooling_vs_deep", "deep_min"),
            ("cooling_vs_wake_events", "wake_events"),
            ("cooling_vs_hrv", "avg_hrv"),
        ):
            w, wo = split(metric)
            result[name] = _effect(w, wo)
        return result
