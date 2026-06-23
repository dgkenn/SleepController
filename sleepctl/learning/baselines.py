"""Robust rolling baselines (7/14-day) + nightly deltas.

Uses median + MAD (median absolute deviation) rather than mean/stdev so a single bad
night barely moves the baseline. Tolerates short/missing history.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Optional

from sleepctl.models import Baselines, NightSummary


_METRICS = [
    "total_sleep_min",
    "deep_min",
    "rem_min",
    "light_min",
    "sleep_efficiency",
    "wake_events",
    "waso_min",
    "avg_hrv",
    "avg_hr",
    "sleep_onset_latency_min",
]


def _values(nights: list[NightSummary], metric: str) -> list[float]:
    out = []
    for n in nights:
        v = getattr(n, metric, None)
        if v is not None:
            out.append(float(v))
    return out


def _median(values: list[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def _mad(values: list[float], med: float) -> float:
    if len(values) < 2:
        return 0.0
    return statistics.median([abs(v - med) for v in values])


class BaselineEngine:
    """Computes rolling baselines and per-night deltas."""

    def update(self, history: list[NightSummary]) -> Baselines:
        metrics: dict[str, float] = {}
        for window in (7, 14):
            recent = history[-window:]
            for metric in _METRICS:
                vals = _values(recent, metric)
                med = _median(vals)
                if med is None:
                    continue
                metrics[f"{metric}_{window}d_median"] = med
                metrics[f"{metric}_{window}d_mad"] = _mad(vals, med)
                metrics[f"{metric}_{window}d_n"] = float(len(vals))
        return Baselines(metrics=metrics, updated=datetime.now())

    def nightly_delta(self, night: NightSummary, baselines: Baselines) -> dict[str, float]:
        deltas: dict[str, float] = {}
        for metric in _METRICS:
            v = getattr(night, metric, None)
            base = baselines.get(f"{metric}_7d_median")
            if v is not None and base is not None:
                deltas[f"{metric}_delta"] = float(v) - float(base)
        return deltas
