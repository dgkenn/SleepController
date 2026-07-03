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

from sleepctl.config import AppConfig
from sleepctl.models import NightObjective, SensorFrame, ThermalIntent


class InductionRoutine:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        # Whether tonight runs the (optional) warm pulse. A/B-toggled by the onset learner via
        # ``set_warm_pulse_arm`` so both arms keep getting sampled; defaults on (user opted in).
        self.warm_pulse_on: bool = True

    def set_warm_pulse_arm(self, on: bool) -> None:
        """Arm/disarm tonight's brief warm pulse (the A/B toggle the onset learner sets)."""
        self.warm_pulse_on = bool(on)

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

        if minutes_in_bed < cold_min:
            return ThermalIntent.ONSET_COLD_SETTLE
        if self.warm_pulse_on and minutes_in_bed < cold_min + warm_min:
            return ThermalIntent.ONSET_WARM
        return ThermalIntent.INDUCTION_COOL
