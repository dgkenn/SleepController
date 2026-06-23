"""Personalized multi-objective reward.

Higher is better. Weighted heavily toward the user's #1 problem — **sleep maintenance**
(wake events) — then deep sleep, HRV, efficiency, total sleep, fast onset. Penalizes short
sleep, poor efficiency, intervention churn, and large temperature swings. Works on either a
predicted-outcomes dict (for action scoring) or a completed ``NightSummary`` (for the stored
``outcome_score``). Missing components simply don't contribute.
"""

from __future__ import annotations

from typing import Optional

from sleepctl.benchmarks import NightMode
from sleepctl.config import AppConfig
from sleepctl.models import NightSummary

# Interpretable per-unit weights (see DESIGN §5). wake_events dominates by design.
WEIGHTS = {
    "wake_events": -3.0,        # per event — maintenance is the top priority
    "waso_min": -0.05,          # per minute awake after onset
    "deep_pct_points": 0.30,    # per percentage-point of deep sleep
    "rem_pct_points": 0.10,     # per percentage-point of REM
    "avg_hrv": 0.05,            # per ms
    "sleep_eff_points": 0.10,   # per percentage-point of efficiency
    "total_sleep_min": 0.01,    # per minute
    "resettle_latency_min": -0.15,  # per minute to fall back asleep after an awakening
}
CHURN_PENALTY = 0.05            # per intervention/action change
TEMP_SWING_PENALTY = 0.03      # per °F of swing beyond the variability cap
SUBJECTIVE_WEIGHT = 0.20       # per point of subjective quality (0-10); grogginess subtracts

# Per-mode multipliers on the base weights so the SAME reward function optimises for the
# RIGHT objective in each situation (see benchmarks.py for the literature rationale):
#   CONSTRAINED (short work night) -> ignore raw duration; reward deep, efficiency, low WASO
#   RECOVERY    (off day/sleep debt) -> reward total sleep, REM, HRV (rebound + debt payback)
MODE_MULTIPLIERS = {
    NightMode.NORMAL: {},
    NightMode.CONSTRAINED: {
        "total_sleep_min": 0.0, "deep_pct_points": 1.5, "sleep_eff_points": 1.6,
        "waso_min": 1.6, "wake_events": 1.2, "rem_pct_points": 0.7,
    },
    NightMode.RECOVERY: {
        "total_sleep_min": 3.0, "rem_pct_points": 2.2, "avg_hrv": 1.8,
        "deep_pct_points": 1.1, "wake_events": 0.85,
    },
}


def _w(key: str, mode: Optional[NightMode]) -> float:
    base = WEIGHTS[key]
    if mode is None:
        return base
    return base * MODE_MULTIPLIERS.get(mode, {}).get(key, 1.0)


def reward_from_outcomes(
    outcomes: dict,
    cfg: AppConfig,
    churn: float = 0.0,
    temp_swing_over_cap: float = 0.0,
    subjective_quality: Optional[float] = None,
    grogginess: Optional[float] = None,
    mode: Optional[NightMode] = None,
) -> float:
    """Compute the reward from a dict of (predicted or observed) outcome values.

    ``mode`` selects the situation-specific objective; when None the original
    balanced weighting is used (so behaviour is unchanged by default).
    """
    s = 0.0
    if "wake_events" in outcomes and outcomes["wake_events"] is not None:
        s += _w("wake_events", mode) * outcomes["wake_events"]
    if outcomes.get("waso_min") is not None:
        s += _w("waso_min", mode) * outcomes["waso_min"]
    if outcomes.get("deep_pct") is not None:
        s += _w("deep_pct_points", mode) * (outcomes["deep_pct"] * 100.0)
    if outcomes.get("rem_pct") is not None:
        s += _w("rem_pct_points", mode) * (outcomes["rem_pct"] * 100.0)
    if outcomes.get("avg_hrv") is not None:
        s += _w("avg_hrv", mode) * outcomes["avg_hrv"]
    if outcomes.get("sleep_efficiency") is not None:
        s += _w("sleep_eff_points", mode) * (outcomes["sleep_efficiency"] * 100.0)
    if outcomes.get("total_sleep_min") is not None:
        s += _w("total_sleep_min", mode) * outcomes["total_sleep_min"]
    # Handling awakenings well = falling back asleep fast (maintenance is the priority).
    if outcomes.get("resettle_latency_min") is not None:
        s += WEIGHTS["resettle_latency_min"] * outcomes["resettle_latency_min"]
    if outcomes.get("sleep_onset_latency_min") is not None:
        b = cfg.benchmarks
        mid = (b.onset_latency_min + b.onset_latency_max) / 2.0
        # fast onset matters more on a constrained night (don't waste opportunity)
        sol_w = 0.18 if mode == NightMode.CONSTRAINED else 0.10
        s -= sol_w * abs(outcomes["sleep_onset_latency_min"] - mid)

    s -= CHURN_PENALTY * churn
    s -= TEMP_SWING_PENALTY * max(0.0, temp_swing_over_cap)
    if subjective_quality is not None:
        s += SUBJECTIVE_WEIGHT * subjective_quality
    if grogginess is not None:
        s -= SUBJECTIVE_WEIGHT * grogginess
    return s


def _outcomes_from_night(night: NightSummary) -> dict:
    total = night.total_sleep_min or 0.0
    return {
        "wake_events": float(night.wake_events) if night.wake_events is not None else None,
        "waso_min": night.waso_min,
        "deep_pct": (night.deep_min / total) if (night.deep_min is not None and total) else None,
        "rem_pct": (night.rem_min / total) if (night.rem_min is not None and total) else None,
        "avg_hrv": night.avg_hrv,
        "sleep_efficiency": night.sleep_efficiency,
        "total_sleep_min": night.total_sleep_min,
        "sleep_onset_latency_min": night.sleep_onset_latency_min,
    }


def night_outcome_score(
    night: NightSummary,
    cfg: AppConfig,
    churn: float = 0.0,
    temp_swing_over_cap: float = 0.0,
    subjective_quality: Optional[float] = None,
    grogginess: Optional[float] = None,
    mode: Optional[NightMode] = None,
) -> float:
    """The reward for a completed night (stored as ``NightSummary.outcome_score``)."""
    return reward_from_outcomes(
        _outcomes_from_night(night), cfg, churn, temp_swing_over_cap,
        subjective_quality, grogginess, mode=mode,
    )
