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
        profile=None,
    ) -> None:
        self.cfg = cfg
        self._f_to_level = f_to_level or default_f_to_level
        # The learnable setpoint profile supplies the personalized effective targets +
        # blend weight; falls back to config defaults. The learning loop / ML updates it.
        self.profile = profile or cfg.default_setpoints()
        # recent commanded temps for the variability cap (short rolling window)
        self._recent_targets: deque[float] = deque(maxlen=8)
        # latency-awareness state: the response lag + the last material command's time/value
        self.response_lag_min: float = cfg.tunables.thermal_response_lag_min
        self._last_cmd_time = None
        self._last_cmd_water = None
        # Feed-forward environmental pre-compensation bias (°F), set from the overnight forecast.
        self.ambient_bias_f: float = 0.0
        # Signed maintenance "settle" nudge (°F vs neutral) used by SETTLE_COOL; <0 cools, >0
        # warms. Learnable per phenotype (Raymann warming vs Fronczek cooling — see config).
        self.settle_nudge_f: float = cfg.tunables.maintenance_settle_nudge_f
        # Learnable onset warm-nudge (°F above neutral during induction). Personalized per-mode by
        # the onset learner from measured sleep-onset latency; bounded by the comfort cap.
        self.onset_warm_f: float = cfg.tunables.onset_warm_nudge_f

    def set_onset_warm(self, warm_f: float) -> None:
        """Set the learned onset warm nudge, clamped to the comfort cap (never overheats)."""
        cap = self.cfg.tunables.onset_warm_comfort_cap_f
        self.onset_warm_f = max(0.0, min(cap, float(warm_f or 0.0)))

    def set_response_lag(self, minutes: float) -> None:
        """Update the learned actuation latency the control loop anticipates."""
        if minutes and minutes > 0:
            self.response_lag_min = float(minutes)

    def set_ambient_bias(self, bias_f: float) -> None:
        """Set the forecast-driven feed-forward bias, clamped to the configured cap."""
        cap = self.cfg.tunables.precomp_max_bias_f
        self.ambient_bias_f = max(-cap, min(cap, float(bias_f or 0.0)))

    def set_settle_nudge(self, nudge_f: float) -> None:
        """Set the learned signed maintenance settle nudge, clamped to the comfort cap."""
        cap = self.cfg.tunables.maintenance_settle_cap_f
        self.settle_nudge_f = max(-cap, min(cap, float(nudge_f or 0.0)))

    # -- composite (effective) temperature ---------------------------------------
    # Effective comfort = a blend of what the COVERED body feels (the Pod's bed-surface
    # temperature, which already integrates body heat + water + room) and what EXPOSED
    # skin (head/face) feels (the room/ambient air). A cold head therefore calls for a
    # warmer bed to keep the blended comfort on target, and vice-versa.
    def composite_temp(
        self, bed_temp_f: Optional[float], ambient_temp_f: Optional[float]
    ) -> Optional[float]:
        """Measured effective temperature = a*bed + (1-a)*ambient (None if no bed temp)."""
        if bed_temp_f is None:
            return None
        if ambient_temp_f is None:
            return bed_temp_f  # no exposed-skin info -> just the bed surface
        a = self.profile.composite_bed_weight
        return a * bed_temp_f + (1.0 - a) * ambient_temp_f

    def required_water_open_loop(
        self, effective_target_f: float, ambient_temp_f: Optional[float]
    ) -> float:
        """Feedforward: invert the blend to get the bed/water target (no measured bed temp)."""
        if ambient_temp_f is None:
            return effective_target_f
        a = self.profile.composite_bed_weight
        return clamp_fahrenheit((effective_target_f - (1.0 - a) * ambient_temp_f) / a)

    # -- intent -> EFFECTIVE comfort target --------------------------------------
    def target_for(
        self,
        intent: ThermalIntent,
        objective: NightObjective,
        hot_sleeper: bool,
        last_target_f: Optional[float] = None,
    ) -> float:
        """Per-intent EFFECTIVE comfort target (the blended temperature we want felt).

        Reads the learnable ``profile`` so the learning loop / ML can tailor each target.
        """
        t = self.cfg.tunables
        p = self.profile
        bias = (t.hot_sleeper_cool_bias_f if hot_sleeper else 0.0) + self.ambient_bias_f
        neutral = p.neutral_f + bias

        if intent is ThermalIntent.WIND_DOWN:
            target = neutral - 1.0  # gentle, not aggressive
        elif intent is ThermalIntent.INDUCTION_COOL:
            target = neutral - 2.0  # short cool dip to help onset
        elif intent is ThermalIntent.DEEP_BIAS_COOL:
            target = p.deep_bias_f + bias
        elif intent is ThermalIntent.REM_NEUTRAL:
            # Evidence (Eight Sleep Autopilot RCT, SLEEP 2024): warmth promotes REM. Apply
            # a SMALL warm offset above neutral -- kept gentle + slew-limited to protect the
            # user's sleep-maintenance priority (no abrupt change).
            target = neutral + p.rem_warm_offset_f
        elif intent is ThermalIntent.SETTLE_COOL:
            # Gentle SIGNED settle nudge to pre-empt / recover from an awakening. Default
            # cools (settle_nudge_f<0, the hot-sleeper evidence default), but learnable to
            # warm if that better prevents this user's awakenings; slew-limited downstream so
            # it never jolts the sleeper.
            target = neutral + self.settle_nudge_f
        elif intent is ThermalIntent.ONSET_WARM:
            # Small WARM nudge to induce onset (cutaneous warming speeds sleep onset). Bounded
            # by the comfort cap so a hot sleeper is never overheated; the controller cools
            # again once asleep. The hot-sleeper cool bias is intentionally NOT applied here.
            nudge = min(self.onset_warm_f, t.onset_warm_comfort_cap_f)
            target = p.neutral_f + nudge
        elif intent is ThermalIntent.WAKE_RAMP:
            target = p.wake_ramp_f  # warm toward wake (no cool bias)
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

        return clamp_fahrenheit(target)

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
        last_target_f: float,
        bed_temp_f: Optional[float] = None,
        ambient_temp_f: Optional[float] = None,
        now=None,
    ) -> tuple[float, int]:
        """Composite-feedback pipeline -> safe (water_target_f, device level).

        Drives the **effective** (blended) temperature to its per-intent target by nudging
        the commanded water temperature. Self-calibrates to body heat (via the measured bed
        temp) and exposed-skin ambient. Always bounded by slew + variability + device range.

        Latency-aware: a fresh closed-loop correction is DAMPED while the previous command is
        still taking effect (within ``response_lag_min``), because the measured error hasn't
        yet reflected the in-flight change. This prevents stacking corrections faster than the
        bed can respond -> no overshoot/oscillation.
        """
        t = self.cfg.tunables
        last = last_target_f if last_target_f is not None else t.neutral_temp_f

        if intent is ThermalIntent.STABILIZE:
            water = last                                   # hold the last command
        elif intent is ThermalIntent.WAKE_RAMP:
            water = self.target_for(intent, objective, hot_sleeper, last)  # direct warming
        else:
            eff_target = self.target_for(intent, objective, hot_sleeper, last)
            measured = self.composite_temp(bed_temp_f, ambient_temp_f)
            if measured is not None:
                # Closed loop on the composite: error in effective-comfort °F nudges water.
                error = eff_target - measured
                step = t.composite_feedback_gain * error
                step *= self._latency_damping(now, step)  # don't over-correct in-flight
                water = last + step
            else:
                # No measured bed temp -> feedforward blend inversion (ambient only).
                water = self.required_water_open_loop(eff_target, ambient_temp_f)

        slewed = self.slew_limit(last, water)
        capped = self.enforce_variability_cap(slewed)
        final = clamp_fahrenheit(capped)
        # Remember a MATERIAL command so the next ticks can damp against its in-flight effect.
        if now is not None and abs(final - last) >= 0.25:
            self._last_cmd_time = now
            self._last_cmd_water = final
        return final, self.to_level(final)

    def _latency_damping(self, now, step: float) -> float:
        """Damping factor in [0,1] for a fresh closed-loop correction.

        Right after a material command the bed hasn't responded yet, so the measured error is
        stale and a full new correction would over-shoot. We scale the correction by
        ``elapsed / response_lag`` while inside the lag window (0 -> just commanded, 1 -> the
        effect has had time to land). Slew/variability still bound everything."""
        if now is None or self._last_cmd_time is None:
            return 1.0
        try:
            elapsed = (now - self._last_cmd_time).total_seconds() / 60.0
        except Exception:
            return 1.0
        lag = max(1.0, self.response_lag_min)
        return max(0.0, min(1.0, elapsed / lag))
