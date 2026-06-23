"""Deterministic synthetic Pod for offline end-to-end testing.

Generates a realistic night as a stream of ``SensorFrame``s — an awake-in-bed onset
period, NREM/REM cycles with stage-dependent HR/HRV/RR/movement, optional injected
awakenings, and a morning wake — so the whole controller loop runs with no hardware.
Scenarios: ``normal``, ``short_sleep``, ``clustered_awakenings``.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Optional

from sleepctl.adapters.base import PodSensorSource, ThermalActuator
from sleepctl.models import NightSummary, SensorFrame, SleepStage


_STAGE_PHYSIO = {
    # stage: (hr, hrv, rr, movement)
    SleepStage.AWAKE: (64, 50, 15.0, 0.5),
    SleepStage.LIGHT: (56, 62, 13.5, 0.08),
    SleepStage.DEEP: (51, 72, 12.0, 0.03),
    SleepStage.REM: (60, 58, 14.5, 0.12),
}


class ScriptedNight:
    """Builds the per-minute stage sequence for a scenario."""

    def __init__(
        self,
        scenario: str = "normal",
        start: Optional[datetime] = None,
        seed: int = 7,
    ) -> None:
        self.scenario = scenario
        self.start = start or datetime(2026, 6, 23, 23, 0, 0)
        self.rng = random.Random(seed)
        self.stages: list[SleepStage] = []
        self.awakening_minutes: set[int] = set()
        self._build()

    def _build(self) -> None:
        if self.scenario == "short_sleep":
            total_min = 5 * 60
            onset = 10
        else:
            total_min = 8 * 60
            onset = 15

        # awake-in-bed onset
        self.stages.extend([SleepStage.AWAKE] * onset)

        # repeating cycles: light -> deep -> light -> rem (~90 min)
        cycle = (
            [SleepStage.LIGHT] * 20
            + [SleepStage.DEEP] * 25
            + [SleepStage.LIGHT] * 20
            + [SleepStage.REM] * 25
        )
        while len(self.stages) < total_min:
            self.stages.extend(cycle)
        self.stages = self.stages[:total_min]

        # injected awakenings
        if self.scenario == "clustered_awakenings":
            # cluster of awakenings around the 3.0-3.5h mark (a recurring problem window)
            for m in range(180, 210, 8):
                self._inject_awakening(m)
        elif self.scenario == "normal":
            self._inject_awakening(240)  # one brief awakening, as expected of a good night

    def _inject_awakening(self, minute: int) -> None:
        for m in range(minute, min(minute + 3, len(self.stages))):
            self.stages[m] = SleepStage.AWAKE
            self.awakening_minutes.add(m)

    def frame_at(self, minute: int, commanded_level: Optional[int]) -> SensorFrame:
        stage = self.stages[minute] if minute < len(self.stages) else SleepStage.AWAKE
        hr, hrv, rr, move = _STAGE_PHYSIO[stage]
        jitter = self.rng.uniform
        is_wake = minute in self.awakening_minutes
        return SensorFrame(
            timestamp=self.start + timedelta(minutes=minute),
            stage=stage,
            stage_confidence=0.4 if is_wake else jitter(0.75, 0.95),
            heart_rate=hr + jitter(-2, 2) + (8 if is_wake else 0),
            hrv=hrv + jitter(-4, 4) - (10 if is_wake else 0),
            respiratory_rate=rr + jitter(-0.6, 0.6) + (2.5 if is_wake else 0),
            movement=max(0.0, move + jitter(-0.02, 0.04) + (0.6 if is_wake else 0)),
            presence=True,
            bed_temp_f=70.0 + jitter(-0.5, 0.5),
            room_temp_f=68.0,
            commanded_level=commanded_level,
            data_age_seconds=30.0,
        )


class SimulatorSource(PodSensorSource):
    """Replays a ScriptedNight one frame per ``read_frame`` call."""

    def __init__(self, scenario: str = "normal", seed: int = 7, start: Optional[datetime] = None) -> None:
        self.night = ScriptedNight(scenario, start=start, seed=seed)
        self.minute = -1
        self._last_level: Optional[int] = None

    @property
    def length(self) -> int:
        return len(self.night.stages)

    @property
    def exhausted(self) -> bool:
        return self.minute >= self.length - 1

    def set_commanded_level(self, level: int) -> None:
        self._last_level = level

    def read_frame(self) -> SensorFrame:
        self.minute = min(self.minute + 1, self.length - 1)
        return self.night.frame_at(self.minute, self._last_level)

    def now(self) -> datetime:
        return self.night.start + timedelta(minutes=max(self.minute, 0))

    def fetch_night_summary(self, date: str) -> NightSummary:
        stages = self.night.stages
        per = lambda st: sum(1 for s in stages if s is st)
        deep = per(SleepStage.DEEP)
        rem = per(SleepStage.REM)
        light = per(SleepStage.LIGHT)
        awake = per(SleepStage.AWAKE)
        asleep = deep + rem + light
        onset = next((i for i, s in enumerate(stages) if s is not SleepStage.AWAKE), 0)
        wake_events = self._count_awakenings()
        in_bed = len(stages)
        return NightSummary(
            date=date,
            bedtime=self.night.start,
            wake_time=self.night.start + timedelta(minutes=in_bed),
            total_sleep_min=float(asleep),
            sleep_onset_latency_min=float(onset),
            deep_min=float(deep),
            rem_min=float(rem),
            light_min=float(light),
            wake_events=wake_events,
            waso_min=float(max(0, awake - onset)),
            sleep_efficiency=round(asleep / in_bed, 3) if in_bed else None,
            avg_hr=52.0,
            avg_hrv=66.0,
            avg_respiratory_rate=13.0,
        )

    def _count_awakenings(self) -> int:
        # count awake runs after onset
        count, prev = 0, SleepStage.AWAKE
        seen_sleep = False
        for s in self.night.stages:
            if s is not SleepStage.AWAKE:
                seen_sleep = True
            if seen_sleep and s is SleepStage.AWAKE and prev is not SleepStage.AWAKE:
                count += 1
            prev = s
        return count

    def capabilities(self) -> dict:
        return {"source": "simulator", "fields": "all", "real_time": True}


class SimulatorActuator(ThermalActuator):
    """Records commanded levels so tests can assert slew/variability limits."""

    def __init__(self, source: Optional[SimulatorSource] = None) -> None:
        self.commands: list[int] = []
        self.smart_levels: list[tuple[int, str]] = []
        self.alarms: list[tuple] = []
        self._current = 0
        self.source = source

    def set_level(self, level: int, duration_s: int = 0) -> None:
        self._current = level
        self.commands.append(level)
        if self.source is not None:
            self.source.set_commanded_level(level)

    def set_smart_level(self, level: int, stage: str) -> None:
        self.smart_levels.append((level, stage))

    def set_alarm(self, time, vibration: int, thermal_level: int) -> None:
        self.alarms.append((time, vibration, thermal_level))

    def get_current_level(self) -> int:
        return self._current
