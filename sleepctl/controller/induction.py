"""Wind-down + sleep-onset induction.

Onset is induced with a 3-phase **cold -> warm -> cool** cascade, tuned for a hot sleeper whose
#1 problem is staying asleep:
  1. COLD SETTLE (genuinely cold): shed the hot sleeper's heat, make them comfortable, and prime
     peripheral vasoconstriction so the later warm pulse produces a stronger vasodilation contrast.
  2. WARM PULSE (brief, small, comfort-capped): a short cutaneous warm nudge (Raymann/Van Someren);
     from the cold-primed state it triggers vasodilation -> core-temp drop -> sleepiness. This
     phase is A/B-toggled per night (``warm_pulse_on``) so the learner can measure whether it helps.
  3. CONSOLIDATE COOL: cool again as the user drifts off (the falling core temperature is what
     deepens sleep). Once onset is confirmed the state machine hands off to maintenance.
On short nights (DAMAGE_CONTROL) the cold + warm phases are compressed so onset comes faster.
"""

from __future__ import annotations

from typing import Optional

from sleepctl.config import AppConfig
from sleepctl.controller.thermal_latency import ThermalLatencyModel
from sleepctl.models import NightObjective, SensorFrame, ThermalIntent

# Hard cap on the reach-aware warm-pulse length so a slow/degenerate measurement can't run the
# pulse away (it must still hand off to consolidate within the induction window).
WARM_PULSE_MAX_MIN = 25.0


class InductionRoutine:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        # Whether tonight runs the (optional) warm pulse. A/B-toggled by the onset learner via
        # ``set_warm_pulse_arm`` so both arms keep getting sampled; defaults on (user opted in).
        self.warm_pulse_on: bool = True
        # Reach-time model + phase target levels (set by the controller). When both are present the
        # warm-pulse phase is lengthened so the bed can actually REACH the warm level before
        # consolidate; when latency is None the cascade keeps its fixed-duration behavior.
        self.latency: Optional[ThermalLatencyModel] = None
        self._cold_level: Optional[float] = None
        self._warm_level: Optional[float] = None
        self._consolidate_level: Optional[float] = None

    def set_warm_pulse_arm(self, on: bool) -> None:
        """Arm/disarm tonight's brief warm pulse (the A/B toggle the onset learner sets)."""
        self.warm_pulse_on = bool(on)

    def set_latency(self, model: Optional[ThermalLatencyModel]) -> None:
        """Attach (or clear) the reach-time model so phase durations can size to the bed's speed."""
        self.latency = model

    def set_phase_levels(self, cold_level, warm_level, consolidate_level) -> None:
        """Give the routine the device levels of each phase target (cold-settle, warm-pulse,
        consolidate) so it can compute how long warming from cold actually takes. The controller
        refreshes these from the live thermal targets each induction tick; None-safe."""
        self._cold_level = None if cold_level is None else float(cold_level)
        self._warm_level = None if warm_level is None else float(warm_level)
        self._consolidate_level = None if consolidate_level is None else float(consolidate_level)

    def _warm_min_effective(self, warm_min: float) -> float:
        """Warm-pulse length: at least the configured ``warm_min``, but widened to the reach-time
        from the cold-settle level up to the warm level (so the pulse is actually felt), capped."""
        if self.latency is None or self._cold_level is None or self._warm_level is None:
            return warm_min
        reach = self.latency.minutes_to_reach(self._cold_level, self._warm_level)
        return min(WARM_PULSE_MAX_MIN, max(warm_min, reach))

    def induction_minutes(self, objective: NightObjective) -> int:
        t = self.cfg.tunables
        if objective is NightObjective.DAMAGE_CONTROL:
            return t.induction_minutes_short
        return t.induction_minutes_normal

    def step(
        self,
        frame: SensorFrame,
        objective: NightObjective,
        minutes_in_bed: float,
    ) -> ThermalIntent:
        """Return the thermal intent for this induction tick.

        3-phase cascade by minutes-in-bed: COLD SETTLE (really cold) -> optional brief WARM PULSE
        -> CONSOLIDATE COOL. On short nights (DAMAGE_CONTROL) the cold + warm phases are halved so
        onset comes faster. When the warm pulse is disarmed the cascade is simply cold -> cool.
        """
        t = self.cfg.tunables
        cold_min = t.induction_cold_settle_min
        warm_min = t.induction_warm_pulse_min
        if objective is NightObjective.DAMAGE_CONTROL:
            # Short night: fast onset matters more, so compress the opening phases (halve them).
            cold_min = cold_min / 2.0
            warm_min = warm_min / 2.0

        # Reach-aware sizing: warming from the cold-settle floor is slow (~1.3 lvl/min from very
        # cold), so a fixed 10-min pulse can be invisible. Widen the pulse to the measured reach
        # time (never shorten it) so the bed actually arrives at the warm level before consolidate.
        warm_min = self._warm_min_effective(warm_min)

        if minutes_in_bed < cold_min:
            return ThermalIntent.ONSET_COLD_SETTLE
        if self.warm_pulse_on and minutes_in_bed < cold_min + warm_min:
            return ThermalIntent.ONSET_WARM
        return ThermalIntent.INDUCTION_COOL
