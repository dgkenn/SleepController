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
             deepen: bool = False, release: bool = False) -> ThermalIntent:
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
        # LIGHT / UNKNOWN. Precedence (also enforced by the arbiter upstream, but pinned here so
        # the routine is correct for any caller): wake-PREVENTION outranks deepening — maintenance
        # is the #1 priority, so a building disturbance gets the settle nudge and we do NOT deepen
        # into it. Only when nothing is brewing do we run the in-night steerer's deepen (drive
        # toward the deep setpoint; cooler -> more deep). Slew/variability/clamp still bound it.
        if preempt_cool:
            return ThermalIntent.SETTLE_COOL
        if deepen:
            return ThermalIntent.DEEP_BIAS_COOL
        if release:
            # RELEASE the settle. STABILIZE means "hold the last target", so without this every
            # settle nudge ratchets the bed down and nothing ever brings it back: 2026-09-18
            # held 68F for 87% of maintenance while pre-emption was active on 21% of ticks, and
            # 194 of 196 pre-empt firings resolved to "hold" because the bed was already there.
            # After a quiet spell, return to neutral -- slew- and variability-limited on the way,
            # so it is a drift, not a jolt -- which both restores the headroom pre-emption needs
            # and puts the night back on the temperature this user's record calls their best.
            return ThermalIntent.NEUTRAL
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
