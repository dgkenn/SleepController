"""Wind-down + sleep-onset induction.

Onset is induced with a literature-backed **warm-then-cool** cascade: a small WARM nudge
first (cutaneous warming speeds sleep onset and suppresses wakefulness — Raymann/Van Someren),
then a cool dip as the user drifts off (the falling core temperature that warming triggers is
what deepens sleep). Once onset is confirmed the state machine hands off to maintenance, which
cools further. On short nights the cool dip starts sooner because fast onset matters more.
"""

from __future__ import annotations

from sleepctl.config import AppConfig
from sleepctl.models import NightObjective, SensorFrame, ThermalIntent


class InductionRoutine:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg

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

        First ~third of the window: a small WARM nudge to trigger onset. Remainder: a cool
        dip to consolidate as the user drifts off. On short nights the cool dip starts sooner.
        """
        window = self.induction_minutes(objective)
        if objective is NightObjective.DAMAGE_CONTROL:
            warm_until = window * 0.15
        else:
            warm_until = window * 0.35

        if minutes_in_bed < warm_until:
            return ThermalIntent.ONSET_WARM
        return ThermalIntent.INDUCTION_COOL
