"""Sophisticated, graded awakening/arousal detection (all sensors).

Sleep maintenance is the user's #1 problem, so detecting disturbances must be both sensitive
and precise. Mirroring the sleep-onset detector, this combines every Pod-2 BCG signal and
uses signal *shape* + *persistence* to grade the disturbance rather than emit a single
yes/no — so the controller can respond proportionally (a brief micro-arousal gets a tiny
cooling nudge; a full awakening triggers recovery; a bed-exit ends the night).

Literature-grounded signals:
  - heart-rate SURGE above the sleep baseline -- cardiac activation reliably accompanies
    cortical arousals (the autonomic arousal marker; Sforza/ASDA)
  - HRV drop (sympathetic shift at arousal)
  - body MOVEMENT -- the basis of actigraphic wake scoring (Cole-Kripke)
  - respiratory irregularity / sighs
  - stage regression (deep/REM -> light/awake) and the AWAKE label itself
  - loss of bed presence (got up)

Grading (NONE / MICRO_AROUSAL / AWAKENING / OUT_OF_BED) uses persistence: a transient
multi-signal blip that self-resolves is a micro-arousal; a sustained one is an awakening.
The underlying multi-signal vote reuses the tested ``WakeDetector``.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional

from sleepctl.controller.wake_detection import WakeDetector
from sleepctl.models import SensorFrame, SleepStage, WakeEvent


class ArousalLevel(str, Enum):
    NONE = "none"
    MICRO = "micro_arousal"     # brief; nudge but stay in maintenance
    AWAKENING = "awakening"     # sustained; enter recovery
    OUT_OF_BED = "out_of_bed"   # left the bed


def _mean(values) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return statistics.fmean(vals) if vals else None


@dataclass
class ArousalAssessment:
    level: ArousalLevel
    score: float
    confidence: float
    signals: List[str] = field(default_factory=list)
    wake_event: Optional[WakeEvent] = None

    @property
    def is_awakening(self) -> bool:
        return self.level in (ArousalLevel.AWAKENING, ArousalLevel.OUT_OF_BED)


class ArousalDetector:
    def __init__(self, cfg=None) -> None:
        t = getattr(cfg, "tunables", None)
        self.hr_surge = getattr(t, "arousal_hr_surge_bpm", 6.0)
        self.hrv_drop_frac = getattr(t, "arousal_hrv_drop_frac", 0.15)
        self.move_threshold = getattr(t, "arousal_movement", 0.4)
        self.persistence = getattr(t, "arousal_persistence_samples", 3)
        self.voter = WakeDetector(min_signals=getattr(t, "wake_min_signals", 3))
        self._elevated_streak = 0

    def reset(self) -> None:
        self._elevated_streak = 0

    def assess(
        self,
        frame: SensorFrame,
        recent: List[SensorFrame],
        now: datetime,
        sleep_hr_baseline: Optional[float] = None,
        sleep_hrv_baseline: Optional[float] = None,
    ) -> ArousalAssessment:
        # Bed exit ends the night regardless of physiology.
        if frame.presence is False:
            self._elevated_streak = 0
            return ArousalAssessment(ArousalLevel.OUT_OF_BED, 1.0, 1.0, ["bed_exit"])

        window = recent[-12:] if recent else []
        base_hr = sleep_hr_baseline if sleep_hr_baseline is not None else _mean(
            [f.heart_rate for f in window[:-2]])
        base_hrv = sleep_hrv_baseline if sleep_hrv_baseline is not None else _mean(
            [f.hrv for f in window[:-2]])

        signals: List[str] = []
        score = 0.0

        # HR surge (graded by magnitude) — the autonomic arousal marker.
        if frame.heart_rate is not None and base_hr is not None:
            delta = frame.heart_rate - base_hr
            if delta >= self.hr_surge:
                score += min(0.4, 0.25 + 0.03 * (delta - self.hr_surge))
                signals.append("hr_surge")
        # HRV drop (sympathetic shift).
        if frame.hrv is not None and base_hrv:
            if frame.hrv <= base_hrv * (1.0 - self.hrv_drop_frac):
                score += 0.2
                signals.append("hrv_drop")
        # Movement.
        if frame.movement is not None and frame.movement >= self.move_threshold:
            score += min(0.35, 0.2 + 0.3 * (frame.movement - self.move_threshold))
            signals.append("movement")
        # Respiratory irregularity.
        rrs = [f.respiratory_rate for f in window if f.respiratory_rate is not None]
        if len(rrs) >= 4 and frame.respiratory_rate is not None:
            if statistics.pstdev(rrs) > 1.5:
                score += 0.12
                signals.append("resp_irregular")
        # Stage regression / awake label.
        prev_stage = window[-1].stage if window else SleepStage.UNKNOWN
        if frame.stage in (SleepStage.LIGHT, SleepStage.AWAKE) and prev_stage in (
                SleepStage.DEEP, SleepStage.REM):
            score += 0.15
            signals.append("stage_regression")
        if frame.stage is SleepStage.AWAKE:
            score += 0.25
            signals.append("awake_stage")

        score = min(1.0, score)

        # Persistence: how long has the disturbance been elevated?
        elevated = score >= 0.45 or frame.stage is SleepStage.AWAKE
        self._elevated_streak = self._elevated_streak + 1 if elevated else 0

        # The tested multi-signal vote (returned as the WakeEvent for logging/back-compat).
        wake_event = self.voter.evaluate(frame, recent, now)

        # Grade.
        if (frame.stage is SleepStage.AWAKE and self._elevated_streak >= self.persistence) \
                or self._elevated_streak >= self.persistence + 1:
            level = ArousalLevel.AWAKENING
        elif score >= 0.45 or wake_event is not None:
            level = ArousalLevel.MICRO
        else:
            level = ArousalLevel.NONE

        confidence = min(1.0, 0.4 + 0.6 * score)
        return ArousalAssessment(level, score, confidence, signals, wake_event)
