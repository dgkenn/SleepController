"""Curated n-of-1 experiment templates + a-priori power guidance.

Makes the (already rigorous) experiment engine a first-class tool for a quantitative user with a
maintenance problem: one call launches a counterbalanced, washout-controlled crossover for a
hypothesis that actually matters for *staying asleep* — including the **warm-vs-cool prophylaxis**
question, so the user can settle empirically, for himself, the tension the literature leaves open
(Raymann warming vs hot-sleeper cooling).

``estimate_nights_needed`` gives an honest, variability-based estimate of how long an experiment
must run to detect a target effect — so experiments are powered, not wishful. n-of-1 trials need
enough cycles to see past night-to-night noise and autocorrelation.
"""

from __future__ import annotations

import math
from typing import List, Optional

# Each template is a ready spec for experiments.create_experiment. ``params`` describe the
# intended config delta for each arm (the controller/daemon applies the assigned arm per night).
TEMPLATES = {
    "cooler_deep": {
        "name": "Cooler deep-sleep setpoint",
        "hypothesis": "A colder bed during deep sleep reduces awakenings.",
        "variable": "deep_bias_f",
        "metric": "wake_events",
        "arm_a": {"label": "current deep bias", "params": {"deep_bias_delta_f": 0.0}},
        "arm_b": {"label": "−2°F deeper cool", "params": {"deep_bias_delta_f": -2.0}},
    },
    "warm_prophylaxis_am": {
        "name": "Early-morning warm vs hold",
        "hypothesis": ("A small skin-warming nudge in the early-morning window reduces awakenings "
                       "(Raymann) — or, for a hot sleeper, makes them worse. Settle it empirically."),
        "variable": "early_morning_nudge",
        "metric": "wake_events",
        "arm_a": {"label": "hold stable (no nudge)", "params": {"am_warm_nudge_f": 0.0}},
        "arm_b": {"label": "small warm nudge", "params": {"am_warm_nudge_f": 0.8}},
    },
    "stability_lockdown": {
        "name": "Variability lockdown",
        "hypothesis": "An ultra-stable temperature (no swings) reduces awakenings vs dynamic control.",
        "variable": "variability_cap_f",
        "metric": "wake_events",
        "arm_a": {"label": "normal dynamics", "params": {"variability_cap_f": 3.0}},
        "arm_b": {"label": "locked stable", "params": {"variability_cap_f": 1.0}},
    },
    "earlier_winddown": {
        "name": "Earlier wind-down",
        "hypothesis": "Starting induction earlier shortens sleep-onset latency.",
        "variable": "induction_minutes",
        "metric": "sleep_onset_latency_min",
        "arm_a": {"label": "normal wind-down", "params": {"induction_minutes_delta": 0}},
        "arm_b": {"label": "+15 min earlier", "params": {"induction_minutes_delta": 15}},
    },
    "anchor_bedtime": {
        "name": "Consistent anchor bedtime",
        "hypothesis": "Holding a consistent bedtime improves continuity vs a variable one.",
        "variable": "bedtime_consistency",
        "metric": "wake_events",
        "arm_a": {"label": "variable bedtime", "params": {"anchor_bedtime": False}},
        "arm_b": {"label": "fixed anchor bedtime", "params": {"anchor_bedtime": True}},
    },
}


def list_templates() -> List[dict]:
    return [{"key": k, "name": v["name"], "hypothesis": v["hypothesis"],
             "metric": v["metric"], "variable": v["variable"]} for k, v in TEMPLATES.items()]


def template(key: str) -> dict:
    if key not in TEMPLATES:
        raise KeyError(f"unknown experiment template {key!r}")
    return dict(TEMPLATES[key])


def create_from_template(repo, key: str, period: Optional[int] = None,
                         washout: Optional[int] = None):
    """Launch a counterbalanced crossover from a template. ``period`` = nights per arm per cycle."""
    from sleepctl.experiments import create_experiment

    spec = template(key)
    if period is not None:
        spec["min_nights_per_arm"] = int(period)
    if washout is not None:
        spec["washout_nights"] = int(washout)
    return create_experiment(repo, spec)


def _metric_values(repo, metric: str, n: int = 21) -> List[float]:
    vals = []
    for night in repo.recent_nights(n):
        v = getattr(night, metric, None)
        if v is not None:
            vals.append(float(v))
    return vals


def estimate_nights_needed(repo, metric: str, target_effect: float,
                           power: float = 0.8, washout: int = 1) -> dict:
    """Rough a-priori duration estimate to detect ``target_effect`` (absolute change in ``metric``)
    given the user's own night-to-night variability. Honest and approximate — a planning guide,
    not a guarantee (n-of-1 also fights autocorrelation/carryover, hence the washout + cycles).
    """
    vals = _metric_values(repo, metric)
    if len(vals) < 3:
        return {"metric": metric, "note": "not enough history to estimate variability yet",
                "sd": None, "nights_per_arm": None, "suggested_period": None,
                "suggested_cycles": None, "total_nights": None}
    mean = sum(vals) / len(vals)
    sd = (sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5
    if target_effect <= 0 or sd == 0:
        nights_per_arm = 3
    else:
        z_alpha, z_beta = 1.96, (0.84 if power >= 0.8 else 0.52)
        nights_per_arm = max(3, math.ceil((z_alpha + z_beta) ** 2 * sd ** 2 / target_effect ** 2))
    period = min(nights_per_arm, 4)                      # nights per arm within one cycle
    cycles = max(2, math.ceil(nights_per_arm / period))  # >=2 for counterbalancing
    total = (period * 2 + washout * 2) * cycles
    return {"metric": metric, "sd": round(sd, 2), "mean": round(mean, 2),
            "target_effect": target_effect, "nights_per_arm": nights_per_arm,
            "suggested_period": period, "suggested_cycles": cycles, "washout": washout,
            "total_nights": total,
            "note": (f"~{total} nights (≈{cycles} cycles of {period} per arm + washout) to detect a "
                     f"{target_effect}-{metric} change against your SD of {round(sd,2)}.")}
