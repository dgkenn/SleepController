"""Apply tiered-policy recommendations to the learnable SetpointProfile.

This turns the abstract policy output ("try deep_bias_cooling 1.0 °F") into a concrete,
bounded, versioned change to the per-stage effective setpoints — the object a future ML
model will own. Every change bumps the version so each night's outcome is attributable to
the exact setpoint that produced it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from sleepctl.config import AppConfig
from sleepctl.models import SetpointProfile

# Safety bounds for learned setpoints (effective comfort °F / offsets).
DEEP_BIAS_BOUNDS = (58.0, 78.0)
REM_OFFSET_BOUNDS = (0.0, 4.0)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _toward(current: float, target: float, step: float) -> float:
    """Move ``current`` toward ``target`` by at most ``step``."""
    if current < target:
        return min(target, current + step)
    return max(target, current - step)


def apply_recommendation(
    profile: SetpointProfile, recommendation: dict, cfg: AppConfig
) -> SetpointProfile:
    """Return the next SetpointProfile given a policy recommendation.

    "hold" leaves the profile (and version) unchanged. "try"/"escalate" nudge the relevant
    stage setpoint by the recommended magnitude; "revert" moves it back toward the default.
    """
    action = recommendation.get("action")
    target = recommendation.get("target")
    mag = float(recommendation.get("magnitude_f", 0.0) or 0.0)

    if action == "hold" or mag == 0.0 and action not in ("revert",):
        return profile  # no setpoint change -> no new version

    new = replace(
        profile, version=profile.version + 1, source="policy", updated=datetime.now()
    )

    if action == "revert":
        d = cfg.default_setpoints()
        step = mag or 1.0
        if target == "deep_bias_cooling":
            new.deep_bias_f = _toward(new.deep_bias_f, d.deep_bias_f, step)
        elif target == "rem_warming":
            new.rem_warm_offset_f = _toward(new.rem_warm_offset_f, d.rem_warm_offset_f, step)
        return new

    # try / escalate: push the stage setpoint in the helpful direction, bounded.
    if target == "deep_bias_cooling":
        new.deep_bias_f = _clamp(new.deep_bias_f - mag, *DEEP_BIAS_BOUNDS)  # cooler deep
    elif target == "rem_warming":
        new.rem_warm_offset_f = _clamp(new.rem_warm_offset_f + mag, *REM_OFFSET_BOUNDS)
    elif target == "thermal_stability":
        # Stability is enforced in-loop (variability cap / wake recovery), not a setpoint;
        # no change to the profile, so revert to the original (no version bump).
        return profile
    return new
