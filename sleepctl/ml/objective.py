"""Priority-weighted objective over predicted outcomes.

Higher is better. Weights mirror the controller's CONTROL_PRIORITY: sleep maintenance
(wake events) dominates, then HRV / deep / REM / efficiency / total sleep, with a penalty
for onset latency drifting outside the target band. Only outcomes the model actually
trained on contribute.
"""

from __future__ import annotations

from sleepctl.config import AppConfig

# Per-outcome weights (sign already encodes "more is better" vs "less is better").
_WEIGHTS = {
    "wake_events": -12.0,        # maintenance is the top priority -> strongly penalize
    "waso_min": -0.15,
    "avg_hrv": 0.30,
    "deep_pct": 40.0,
    "rem_pct": 25.0,
    "sleep_efficiency": 20.0,
    "total_sleep_min": 0.01,
}


def objective_value(predicted: dict, cfg: AppConfig) -> float:
    score = 0.0
    for name, w in _WEIGHTS.items():
        if name in predicted:
            score += w * predicted[name]
    # onset latency: penalize distance from the midpoint of the target band
    if "sleep_onset_latency_min" in predicted:
        b = cfg.benchmarks
        target_mid = (b.onset_latency_min + b.onset_latency_max) / 2.0
        score -= 0.2 * abs(predicted["sleep_onset_latency_min"] - target_mid)
    return score
