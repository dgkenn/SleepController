"""Sleep-stage-aware maintenance + wake-recovery.

Maintenance prioritizes thermal STABILITY to protect sleep (the user's weak point):
cooler+stable in deep sleep, neutral in REM (avoid overcooling / abrupt change), and
hold-stable otherwise. Wake recovery pauses aggressive control after an awakening and
waits for stable physiology before resuming optimization.
"""

from __future__ import annotations

from sleepctl.config import AppConfig
from sleepctl.models import NightObjective, SensorFrame, SleepStage, ThermalIntent


class MaintenanceRoutine:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg

    def step(self, frame: SensorFrame, objective: NightObjective,
             preempt_cool: bool = False, keep_light: bool = False,
             deepen: bool = False) -> ThermalIntent:
        if keep_light:
            # Power-nap mode: hold neutral so the bed never drives slow-wave sleep — keep the
            # nap light so waking is grogginess-free. A rising wake-risk still gets a gentle cool.
            return ThermalIntent.SETTLE_COOL if preempt_cool else ThermalIntent.STABILIZE
        if frame.stage is SleepStage.DEEP:
            # Never disturb deep sleep with proactive moves; the deep-bias cool already runs.
            return ThermalIntent.DEEP_BIAS_COOL
        if frame.stage is SleepStage.REM:
            # REM is when hot sleepers are most vulnerable to heat: if wake-risk is rising,
            # lean cooler instead of the usual small REM warm bias.
            return ThermalIntent.SETTLE_COOL if preempt_cool else ThermalIntent.REM_NEUTRAL
        # LIGHT / UNKNOWN: in-night architecture steering — if we're behind the ideal deep curve
        # and wake-risk is low, drive toward the deep setpoint to bias deeper (cooler -> more
        # deep). This OUTRANKS a plain hold but the risk veto already happened upstream (deepen is
        # only ever True when risk is low). Slew/variability/clamp still bound the move.
        if deepen:
            return ThermalIntent.DEEP_BIAS_COOL
        # Otherwise pre-empt a building disturbance with a gentle cool, else hold steady
        # (stability protects maintenance).
        if preempt_cool:
            return ThermalIntent.SETTLE_COOL
        return ThermalIntent.STABILIZE


class WakeRecoveryRoutine:
    """After an awakening: actively help re-settle (cooling promotes sleep), then hold."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg

    def step(self, frame: SensorFrame) -> ThermalIntent:
        # If the user is stirring (light/awake, moving), a gentle cooling assist re-induces
        # sleep — the same principle as induction. Once physiology settles (deep/REM again),
        # hold steady and stop intervening.
        if frame.stage in (SleepStage.AWAKE, SleepStage.LIGHT, SleepStage.UNKNOWN):
            return ThermalIntent.SETTLE_COOL
        return ThermalIntent.STABILIZE
