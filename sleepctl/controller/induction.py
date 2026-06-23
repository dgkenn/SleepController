"""Wind-down + sleep-onset induction.

While the user is in bed but awake we use a gentle wind-down (not aggressive cooling),
then a short induction cool-dip to actively help sleep onset. On short nights we use a
shorter, more aggressive window because fast onset matters more than experimentation.
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

        First ~third of the induction window: gentle wind-down. Remainder: a short
        cool dip to drive onset. On short nights the cool dip starts almost immediately.
        """
        window = self.induction_minutes(objective)
        if objective is NightObjective.DAMAGE_CONTROL:
            wind_down_until = window * 0.15
        else:
            wind_down_until = window * 0.35

        if minutes_in_bed < wind_down_until:
            return ThermalIntent.WIND_DOWN
        return ThermalIntent.INDUCTION_COOL
