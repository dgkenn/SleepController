"""Smart wake: wake during light sleep inside a window, thermal-only (silence).

The user needs complete silence, so this routine never uses vibration — it nudges the
user awake with a gentle thermal ramp and signals "done" to the runtime, which may fire
a silent/thermal alarm. It prefers to wake during LIGHT/AWAKE sleep within the window
and avoids waking from DEEP unless the hard wake time has arrived.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sleepctl.config import AppConfig
from sleepctl.models import SensorFrame, SleepStage, ThermalIntent


class SmartWakeRoutine:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg

    def in_window(self, now: datetime, required_wake_time: Optional[datetime]) -> bool:
        if required_wake_time is None:
            return False
        window = timedelta(minutes=self.cfg.tunables.wake_window_min)
        return (required_wake_time - window) <= now <= required_wake_time

    def step(
        self,
        frame: SensorFrame,
        now: datetime,
        required_wake_time: Optional[datetime],
    ) -> tuple[ThermalIntent, bool]:
        """Return (thermal_intent, should_wake)."""
        if required_wake_time is None:
            return ThermalIntent.WAKE_RAMP, False

        # Hard deadline: wake regardless of stage.
        if now >= required_wake_time:
            return ThermalIntent.WAKE_RAMP, True

        # Inside the window: wake on a favorable (light/awake) stage.
        if self.in_window(now, required_wake_time):
            if frame.stage in (SleepStage.LIGHT, SleepStage.AWAKE):
                return ThermalIntent.WAKE_RAMP, True
            # Deep/REM: keep ramping gently but wait for a lighter stage.
            return ThermalIntent.WAKE_RAMP, False

        return ThermalIntent.WAKE_RAMP, False
