"""Revealed-preference learning from manual temperature overrides.

When the user manually adjusts the bed temperature, that is strong information about their
comfort — and if the controller keeps "correcting" back, it fights the user and never settles
on their true optimum. So we treat repeated manual choices as a **preference prior** and gently
anchor the learnable setpoint toward the median manual target. (Manual-heavy nights are also
flagged as confounded for *automated*-action attribution — see ``confounders.py`` — so manual
tweaking informs the setpoint without corrupting the reward learning.)

Manual overrides are logged as ``ActionRecord(source="manual", params={"target_f": <°F>})``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from statistics import median
from typing import Optional

from sleepctl.config import AppConfig
from sleepctl.ml.actions import KNOB_BOUNDS
from sleepctl.models import SetpointProfile


def _manual_targets(repo, lookback: int = 60) -> list[float]:
    targets = []
    for a in repo.recent_actions(lookback):
        if a.source == "manual" and a.params and a.params.get("target_f") is not None:
            targets.append(float(a.params["target_f"]))
    return targets


def revealed_preference(
    repo, profile: SetpointProfile, cfg: AppConfig
) -> Optional[SetpointProfile]:
    """Anchor the setpoint toward the user's repeated manual choices (bounded nudge).

    Returns an updated, version-bumped profile, or None if there aren't enough manual
    overrides yet to trust a preference.
    """
    targets = _manual_targets(repo)
    if len(targets) < cfg.tunables.manual_preference_min_count:
        return None
    pref = median(targets)
    gain = cfg.tunables.manual_preference_gain
    step_cap = cfg.tunables.max_step_f

    def nudge(current: float, knob: str) -> float:
        lo, hi = KNOB_BOUNDS[knob]
        delta = gain * (pref - current)
        delta = max(-step_cap, min(step_cap, delta))  # bounded, gradual
        return max(lo, min(hi, current + delta))

    new_neutral = nudge(profile.neutral_f, "neutral_f")
    new_deep = nudge(profile.deep_bias_f, "deep_bias_f")
    if abs(new_neutral - profile.neutral_f) < 1e-6 and abs(new_deep - profile.deep_bias_f) < 1e-6:
        return None
    return replace(
        profile,
        neutral_f=new_neutral,
        deep_bias_f=new_deep,
        version=profile.version + 1,
        source="manual_pref",
        updated=datetime.now(),
    )
