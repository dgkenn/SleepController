"""A response-AWARE sleep model for validating the closed loop.

The shipped ``SimulatorSource`` scripts a fixed stage sequence that ignores the actuator — fine
for wiring tests, useless for asking "does the controller actually help?" This model closes that
gap: the thermal trajectory the controller commands FEEDS BACK into the sleep it produces, grounded
in the same physiology the controller targets (for a hot sleeper, a cool + STABLE bed promotes deep
sleep and suppresses awakenings; too-warm or swingy temperatures fragment it; a warm ramp near wake
eases the wake). So a controller that drives the bed toward good conditions yields measurably better
nights than a no-control baseline.

Honest scope: this validates the MACHINERY (the controller moves the modeled physiology the right
way, learning converges, safety holds, no regressions) — not real-world effect sizes, which need the
Pod. The model encodes physiology direction, not the controller's own heuristics, so "the controller
wins" is informative rather than tautological. Deterministic given a seed.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Optional

from sleepctl.controller.calibration import level_to_fahrenheit
from sleepctl.models import NightSummary, SensorFrame, SleepStage

_IDEAL_F = 67.0            # hot-sleeper comfort optimum (effective bed temp)
_COLD_FLOOR_F = 60.0       # below this, cold itself starts to disturb


class ResponsiveSleepModel:
    """Coupled source + actuator: ``actuate(level)`` sets the commanded temperature; ``advance``
    evolves one minute of sleep under it; ``read_frame`` reports the current state."""

    def __init__(self, scenario: str = "normal", seed: int = 7,
                 start: Optional[datetime] = None, hot_sleeper: bool = True) -> None:
        self.rng = random.Random(seed)
        self.scenario = scenario
        self.hot_sleeper = hot_sleeper
        self.t0 = start or datetime(2026, 6, 1, 23, 0)
        self.now = self.t0
        self.minute = 0
        self.stage = SleepStage.AWAKE
        self.bed_temp_f = 75.0                 # starts a bit warm (uncontrolled)
        self.commanded_f = 75.0
        self._prev_bed_temp = 75.0
        self.asleep = False
        self._stage_min = 0                    # minutes in the current stage
        self._cycle_min = 0                    # position within the ~90 min cycle
        # outcome tallies
        self.counts = {s: 0 for s in SleepStage}
        self.wake_events = 0
        self.onset_latency = None
        self._slew_accum = 0.0
        self.woke_from = None

    # ------------------------------------------------------------------ source API
    def now_dt(self) -> datetime:
        return self.now

    def now_(self) -> datetime:                # alias some callers use
        return self.now

    def read_frame(self) -> SensorFrame:
        hr = 52 + (8 if self.stage in (SleepStage.AWAKE, SleepStage.LIGHT) else 0) \
            + (4 if self.stage is SleepStage.REM else 0)
        movement = 0.5 if self.stage is SleepStage.AWAKE else (
            0.12 if self.stage is SleepStage.LIGHT else 0.03)
        return SensorFrame(timestamp=self.now, stage=self.stage, heart_rate=float(hr),
                           hrv=55.0, respiratory_rate=14.0, movement=movement,
                           bed_temp_f=round(self.bed_temp_f, 1), room_temp_f=70.0,
                           presence=True, data_age_seconds=30.0)

    def fetch_night_summary(self, date: str) -> NightSummary:
        return self.night_summary(date)

    def capabilities(self) -> dict:
        return {"cooling": True}

    # ------------------------------------------------------------------ actuation
    def actuate(self, level: Optional[int]) -> None:
        if level is None:
            return
        self.commanded_f = level_to_fahrenheit(int(level))

    # ------------------------------------------------------------------ dynamics
    def _thermal_penalty(self) -> float:
        """0..~1 disturbance pressure from the current bed temperature (hot sleeper: warm is bad)."""
        warm = max(0.0, self.bed_temp_f - _IDEAL_F)
        cold = max(0.0, _COLD_FLOOR_F - self.bed_temp_f)
        w = 0.045 if self.hot_sleeper else 0.030
        return min(1.0, w * warm + 0.03 * cold)

    def _variability_penalty(self) -> float:
        return min(0.6, 0.35 * abs(self.bed_temp_f - self._prev_bed_temp))

    def advance(self, now: Optional[datetime] = None) -> None:
        """Evolve one minute of sleep under the current commanded temperature."""
        # bed temperature relaxes toward the commanded value (first-order lag).
        self._prev_bed_temp = self.bed_temp_f
        self.bed_temp_f += 0.35 * (self.commanded_f - self.bed_temp_f)
        self._slew_accum += abs(self.bed_temp_f - self._prev_bed_temp)

        tp = self._thermal_penalty()
        vp = self._variability_penalty()
        disturb = tp + vp

        # --- onset ---
        if not self.asleep:
            p_onset = max(0.02, 0.28 - 0.5 * tp)        # cool, calm bed -> faster onset
            if self.rng.random() < p_onset:
                self.asleep = True
                self.onset_latency = self.minute
                self._enter(SleepStage.LIGHT)
            else:
                self._tally(SleepStage.AWAKE)
                self._step_clock(now)
                return

        # --- mid-sleep awakening pressure ---
        if self.stage in (SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM):
            p_wake = 0.004 + 0.06 * disturb
            if self.stage is SleepStage.DEEP:
                p_wake *= 0.4                            # deep is robust
            if self.rng.random() < p_wake:
                self.woke_from = self.stage
                self.wake_events += 1
                self._enter(SleepStage.AWAKE)
                self._tally(SleepStage.AWAKE)
                self._step_clock(now)
                return

        # --- stage progression (a ~90 min cycle, thermally modulated) ---
        self._advance_stage(disturb)
        self._tally(self.stage)
        self._step_clock(now)

    def _advance_stage(self, disturb: float) -> None:
        self._cycle_min += 1
        if self.stage is SleepStage.AWAKE:
            # brief arousal -> back to light
            if self._stage_min >= 1 and self.rng.random() < 0.6:
                self._enter(SleepStage.LIGHT)
        elif self.stage is SleepStage.LIGHT:
            # cool+calm -> descend to deep; warm/swingy -> linger light
            p_deep = max(0.02, 0.25 - 0.5 * disturb)
            if self._stage_min >= 5 and self.rng.random() < p_deep:
                self._enter(SleepStage.DEEP)
            elif self._cycle_min > 60 and self.rng.random() < 0.12:
                self._enter(SleepStage.REM)
        elif self.stage is SleepStage.DEEP:
            # warm bed shortens deep bouts
            p_exit = min(0.5, 0.05 + 0.4 * disturb)
            if self._stage_min >= 12 and self.rng.random() < p_exit:
                self._enter(SleepStage.LIGHT)
        elif self.stage is SleepStage.REM:
            if self._stage_min >= 8 and self.rng.random() < 0.25:
                self._enter(SleepStage.LIGHT)
                self._cycle_min = 0

    # ------------------------------------------------------------------ helpers
    def _enter(self, stage: SleepStage) -> None:
        self.stage = stage
        self._stage_min = 0

    def _tally(self, stage: SleepStage) -> None:
        self.counts[stage] = self.counts.get(stage, 0) + 1

    def _step_clock(self, now: Optional[datetime]) -> None:
        self._stage_min += 1
        self.minute += 1
        self.now = (now + timedelta(minutes=1)) if now else (self.now + timedelta(minutes=1))

    # ------------------------------------------------------------------ outcome
    def night_summary(self, date: Optional[str] = None) -> NightSummary:
        deep = float(self.counts.get(SleepStage.DEEP, 0))
        rem = float(self.counts.get(SleepStage.REM, 0))
        light = float(self.counts.get(SleepStage.LIGHT, 0))
        awake = float(self.counts.get(SleepStage.AWAKE, 0))
        asleep = deep + rem + light
        in_bed = asleep + awake
        eff = (asleep / in_bed) if in_bed > 0 else 0.0
        # grogginess proxy (0-10): a fragmented, short night leaves you groggier. (The wake-FROM-
        # stage inertia is the smart-wake timing's job, not this thermal-architecture model's.)
        grog = 2.0 + 0.2 * self.wake_events + 0.012 * max(0.0, 430.0 - asleep)
        self.grogginess_proxy = round(min(10.0, max(0.0, grog)), 1)  # read by the backtest
        return NightSummary(
            date=date or self.t0.date().isoformat(),
            total_sleep_min=asleep, deep_min=deep, rem_min=rem, light_min=light,
            wake_events=self.wake_events, waso_min=max(0.0, awake - (self.onset_latency or 0)),
            sleep_efficiency=round(eff, 3),
            sleep_onset_latency_min=float(self.onset_latency or 0),
            avg_hr=54.0, avg_hrv=55.0, avg_respiratory_rate=14.0)
