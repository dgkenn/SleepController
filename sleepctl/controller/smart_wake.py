"""Smart wake: wake during light sleep inside a window, with heat + gentle vibration.

Wakes the user at the OPTIMAL point in the sleep cycle — a LIGHT/AWAKE stage inside the
wake window before the required time — using a warming thermal ramp PLUS a gentle vibration
alarm (audio stays OFF, so "silence" is preserved: vibration is tactile, not noise). If no
light-sleep moment occurs, it falls back to firing at the hard deadline. The Pod's native
smart alarm (``set_alarm_direct`` with ``smart_light_sleep``) is programmed for the window so
the device fires precisely during light sleep; this routine drives the complementary ramp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sleepctl.config import AppConfig
from sleepctl.models import SensorFrame, SleepStage, ThermalIntent


@dataclass
class WakeAlarmSpec:
    """A vibration+thermal wake alarm for the runtime/daemon to program on the device."""

    time: datetime          # required wake time (deadline)
    window_min: int         # smart window before `time` to fire during light sleep
    vibration_power: int    # 0 = off; gentle default from config
    thermal_level: int      # warm wake ramp target level
    audio: bool = False     # always off (silence preserved)


class SmartWakeRoutine:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg

    def in_window(self, now: datetime, required_wake_time: Optional[datetime]) -> bool:
        if required_wake_time is None:
            return False
        window = timedelta(minutes=self.cfg.tunables.wake_window_min)
        return (required_wake_time - window) <= now <= required_wake_time

    def alarm_spec(
        self, now: datetime, required_wake_time: Optional[datetime]
    ) -> Optional[WakeAlarmSpec]:
        """Program a vibration+thermal smart alarm once the wake window is reached."""
        t = self.cfg.tunables
        if required_wake_time is None or not self.in_window(now, required_wake_time):
            return None
        power = t.wake_vibration_power if t.wake_vibration_enabled else 0
        return WakeAlarmSpec(
            time=required_wake_time,
            window_min=t.wake_window_min,
            vibration_power=power,
            thermal_level=t.level_max,  # warm; thermal.to_level clamps the actual ramp
        )

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
