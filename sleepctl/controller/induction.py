"""Wind-down + sleep-onset induction.

Onset is induced with a 2-phase **warm -> cool** cascade, grounded in the sleep-onset science
(Raymann, Swaab & Van Someren 2005/2008): cutaneous *warming* — not cooling — is what speeds
sleep onset. A small skin-temperature rise drives distal vasodilation -> a core-temperature drop
-> sleepiness. So the cascade LEADS with a gentle warm nudge, then cools once the user is
drifting off:
  1. WARM NUDGE (brief, small, comfort-capped): a short cutaneous warm nudge that triggers
     vasodilation -> core-temp drop -> sleepiness. Kept small and comfort-capped so a hot sleeper
     is never overheated. This phase is A/B-toggled per night (``warm_pulse_on``) so the onset
     learner can measure whether it helps *this* user; toggling it off makes the cascade cool-only
     from the start (the fallback if warming ever feels bad to a hot sleeper).
  2. CONSOLIDATE COOL: cool as the user drifts off (the falling core temperature is what deepens
     sleep). Once onset is confirmed the state machine hands off to maintenance.
On short nights (DAMAGE_CONTROL) the warm phase is compressed so the deepening cool comes sooner.

Note: pressing "help me fall asleep" must never open with a *cold* blast — that both feels wrong
to the user and is backwards from the physiology (cold promotes vasoconstriction, delaying onset).
The legacy ``ONSET_COLD_SETTLE`` intent is retained in the thermal map for compatibility but is no
longer part of the on-demand induction cascade.
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

        2-phase cascade by minutes-in-bed: WARM NUDGE (gentle, comfort-capped) -> CONSOLIDATE
        COOL. Warming the skin is what speeds onset, so the cascade LEADS with the warm nudge and
        only then cools as the user drifts off. On short nights (DAMAGE_CONTROL) the warm phase is
        halved so the deepening cool comes sooner. When the warm pulse is disarmed the cascade is
        cool-only from the start (the hot-sleeper fallback if warming ever feels bad).
        """
        t = self.cfg.tunables
        warm_min = t.induction_warm_pulse_min
        if objective is NightObjective.DAMAGE_CONTROL:
            # Short night: fast onset matters more, so compress the warm opener (halve it).
            warm_min = warm_min / 2.0

        # Reach-aware sizing: if the bed happens to start cool, warming to the nudge level takes
        # time, so widen the pulse to the measured reach time (never shorten it) so the warm is
        # actually felt before consolidate. None-safe: falls back to the configured length.
        warm_min = self._warm_min_effective(warm_min)

        # Phase 1: gentle warm nudge to trigger onset (cutaneous warming -> vasodilation ->
        # core-temp drop -> sleepiness). Skipped entirely when the warm pulse is disarmed.
        if self.warm_pulse_on and minutes_in_bed < warm_min:
            return ThermalIntent.ONSET_WARM
        # Phase 2: cool as the user drifts off -- the falling core temperature deepens sleep.
        return ThermalIntent.INDUCTION_COOL
