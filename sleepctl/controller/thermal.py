"""Thermal math: intent -> target °F -> device level, with safety limiting.

No device I/O here. The controller reasons in a personalized °F-like scale, then
this module enforces the conservative rules: small steps (<=max_step_f), a variability
cap over a short window, and a documented °F<->level calibration. Tailored for a HOT
sleeper via ``hot_sleeper_cool_bias_f``.
"""

from __future__ import annotations

from collections import deque
from typing import Callable, Optional

from sleepctl.config import AppConfig
from sleepctl.controller.calibration import (
    clamp_fahrenheit,
    fahrenheit_to_level,
    level_to_fahrenheit,
)
from sleepctl.models import NightObjective, ThermalIntent


# Calibration uses the REAL Eight Sleep non-linear lookup table (see calibration.py):
# 55-110 °F, level 0 ~= 81 °F. These thin wrappers let `calibrate` swap in a per-user
# refinement later without changing the controller.
def default_f_to_level(target_f: float) -> int:
    return fahrenheit_to_level(target_f)


def default_level_to_f(level: int) -> float:
    return level_to_fahrenheit(level)


class ThermalController:
    """Turns a ThermalIntent into a safe, calibrated device command."""

    def __init__(
        self,
        cfg: AppConfig,
        f_to_level: Optional[Callable[[float], int]] = None,
    ) -> None:
        self.cfg = cfg
        self._f_to_level = f_to_level or default_f_to_level
        # recent commanded temps for the variability cap (short rolling window)
        self._recent_targets: deque[float] = deque(maxlen=8)

    # -- intent -> target --------------------------------------------------------
    def target_for(
        self,
        intent: ThermalIntent,
        objective: NightObjective,
        hot_sleeper: bool,
        last_target_f: Optional[float] = None,
    ) -> float:
        t = self.cfg.tunables
        bias = t.hot_sleeper_cool_bias_f if hot_sleeper else 0.0
        neutral = t.neutral_temp_f + bias

        if intent is ThermalIntent.WIND_DOWN:
            target = neutral - 1.0  # gentle, not aggressive
        elif intent is ThermalIntent.INDUCTION_COOL:
            target = neutral - 2.0  # short cool dip to help onset
        elif intent is ThermalIntent.DEEP_BIAS_COOL:
            target = t.deep_bias_temp_f + bias
        elif intent is ThermalIntent.REM_NEUTRAL:
            # Evidence (Eight Sleep Autopilot RCT, SLEEP 2024): warmth promotes REM. Apply
            # a SMALL warm offset above neutral -- kept gentle + slew-limited to protect the
            # user's sleep-maintenance priority (no abrupt change).
            target = neutral + t.rem_warm_offset_f
        elif intent is ThermalIntent.WAKE_RAMP:
            target = t.wake_ramp_temp_f  # warm toward wake (no cool bias)
        elif intent is ThermalIntent.STABILIZE:
            target = last_target_f if last_target_f is not None else neutral
        else:  # NEUTRAL
            target = neutral

        # On short nights (DAMAGE_CONTROL) keep things calm: nudge toward neutral to
        # reduce thermal experimentation.
        if objective is NightObjective.DAMAGE_CONTROL and intent in (
            ThermalIntent.INDUCTION_COOL,
            ThermalIntent.DEEP_BIAS_COOL,
        ):
            target = (target + neutral) / 2.0
        return clamp_fahrenheit(target)  # never request outside the device's 55-110 °F

    # -- safety limiting ---------------------------------------------------------
    def slew_limit(self, current_f: float, target_f: float) -> float:
        """Never move more than max_step_f per call (<=1-2°F steps)."""
        step = self.cfg.tunables.max_step_f
        if target_f > current_f + step:
            return current_f + step
        if target_f < current_f - step:
            return current_f - step
        return target_f

    def enforce_variability_cap(self, proposed_f: float) -> float:
        """Clamp so total swing within the recent window stays under the cap."""
        cap = self.cfg.tunables.variability_cap_f
        if not self._recent_targets:
            self._recent_targets.append(proposed_f)
            return proposed_f
        lo = min(self._recent_targets)
        hi = max(self._recent_targets)
        clamped = proposed_f
        if proposed_f > lo + cap:
            clamped = lo + cap
        elif proposed_f < hi - cap:
            clamped = hi - cap
        self._recent_targets.append(clamped)
        return clamped

    # -- conversion --------------------------------------------------------------
    def to_level(self, target_f: float) -> int:
        t = self.cfg.tunables
        level = self._f_to_level(target_f)
        return max(t.level_min, min(t.level_max, level))

    def resolve(
        self,
        intent: ThermalIntent,
        objective: NightObjective,
        hot_sleeper: bool,
        current_f: float,
        last_target_f: Optional[float] = None,
    ) -> tuple[float, int]:
        """Full pipeline: intent -> target -> slew -> variability cap -> level."""
        raw = self.target_for(intent, objective, hot_sleeper, last_target_f)
        slewed = self.slew_limit(current_f, raw)
        capped = self.enforce_variability_cap(slewed)
        return capped, self.to_level(capped)
