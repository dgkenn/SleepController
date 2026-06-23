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

    def step(self, frame: SensorFrame, objective: NightObjective) -> ThermalIntent:
        if frame.stage is SleepStage.DEEP:
            return ThermalIntent.DEEP_BIAS_COOL
        if frame.stage is SleepStage.REM:
            return ThermalIntent.REM_NEUTRAL
        # LIGHT / UNKNOWN: prioritize stability to avoid triggering an awakening.
        return ThermalIntent.STABILIZE


class WakeRecoveryRoutine:
    """After an awakening: keep neutral/slightly cooler, change nothing abruptly."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg

    def step(self, frame: SensorFrame) -> ThermalIntent:
        # Hold the environment steady; do not chase sleep stages until physiology
        # re-stabilizes. STABILIZE keeps the last target; thermal.py applies the
        # hot-sleeper cool bias built into neutral.
        return ThermalIntent.STABILIZE
