"""Proactive wake-risk assessment — prevent awakenings before they happen.

Sleep maintenance is the user's #1 problem, so the controller doesn't just react to
awakenings; it watches for the PRECURSORS of one and pre-empts with a gentle cooling assist
while the sleeper is still asleep. Awakenings (especially for a hot sleeper) are usually
preceded by:

  - heart rate creeping up above the established sleep baseline (autonomic arousal)
  - movement / tossing increasing
  - the bed running warm relative to target (thermal load building) -- the dominant trigger
    for a hot sleeper
  - respiration becoming irregular
  - being in a vulnerable stage (light sleep / a cycle boundary)

Each contributes to a 0..1 risk score. A learned, per-user ``WakeProfile`` (built from the
history of when and at what temperature this person actually wakes) adds two more votes:
proximity to a recurring awakening time, and the bed running near the person's measured
"too warm to stay asleep" threshold. That is the ML-tuned part -- the precursors are general,
the thresholds and timing are learned from the individual.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from sleepctl.models import SensorFrame, SleepStage


def _mean(values) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return statistics.fmean(vals) if vals else None


@dataclass
class WakeProfile:
    """Per-user awakening phenotype. Starts from an EVIDENCE-BACKED preset and is refined by
    the ML from the user's own history (see ml/wake_profile.py).

    ``source`` is "preset" until enough personal data accrues, then "learned" (or "blended").
    """

    awakening_minutes: List[int] = field(default_factory=list)  # minutes-past-midnight clusters
    warm_temp_threshold_f: Optional[float] = None  # bed temp above which awakenings cluster
    cluster_window_min: int = 25                   # +/- window around each recurring time
    # Cycle/circadian vulnerability (evidence-based, personalised by lead-time learning):
    #   - awakenings cluster at NREM-REM cycle boundaries (~every 90 min)
    #   - the back half of the night is lighter and more wake-prone (sleep pressure spent)
    #   - the circadian core-temperature nadir/rise (~4-5 a.m.) is a hot-sleeper danger zone,
    #     compounded because thermoregulation is suspended in the REM that dominates late sleep
    cycle_len_min: int = 90
    cycle_boundary_window_min: int = 12
    back_half_after_cycle: int = 3        # extra vigilance after ~3 cycles
    circadian_window: tuple = (210, 330)  # 03:30-05:30 minutes-past-midnight (Tmin zone)
    source: str = "preset"

    @classmethod
    def evidence_default(cls) -> "WakeProfile":
        """Literature-based starting point used before personal data exists.

        No fixed personal clock-times yet (those are learned), but the cycle-boundary,
        back-half, and circadian-nadir vulnerabilities are seeded so prevention works from
        night one and is then tuned to the individual.
        """
        return cls(awakening_minutes=[], warm_temp_threshold_f=None, source="preset")

    def near_recurring_time(self, now: datetime) -> bool:
        m = now.hour * 60 + now.minute
        for c in self.awakening_minutes:
            d = min(abs(m - c), 1440 - abs(m - c))  # wrap around midnight
            if d <= self.cluster_window_min:
                return True
        return False

    def in_circadian_danger_zone(self, now: datetime) -> bool:
        m = now.hour * 60 + now.minute
        lo, hi = self.circadian_window
        return lo <= m <= hi

    def near_cycle_boundary(self, minutes_since_onset: Optional[float]) -> bool:
        if minutes_since_onset is None or minutes_since_onset < self.cycle_len_min - 20:
            return False
        phase = minutes_since_onset % self.cycle_len_min
        # close to the END of a cycle (boundary) from either side
        return phase <= self.cycle_boundary_window_min or \
            phase >= self.cycle_len_min - self.cycle_boundary_window_min

    def in_back_half(self, minutes_since_onset: Optional[float]) -> bool:
        return (minutes_since_onset is not None
                and minutes_since_onset >= self.back_half_after_cycle * self.cycle_len_min)


@dataclass
class WakeRisk:
    score: float           # 0..1
    reasons: List[str]
    preempt: bool          # True -> apply a pre-emptive cooling assist now


class WakeRiskAssessor:
    """Scores the live risk of an imminent awakening from precursor trends + the profile."""

    def __init__(self, cfg=None, profile: Optional[WakeProfile] = None) -> None:
        t = getattr(cfg, "tunables", None)
        self.hr_creep = getattr(t, "wake_risk_hr_creep_bpm", 4.0)
        self.move_rise = getattr(t, "wake_risk_movement", 0.3)
        self.warm_margin_f = getattr(t, "wake_risk_warm_margin_f", 1.5)
        self.preempt_threshold = getattr(t, "wake_risk_preempt_threshold", 0.5)
        # Default to the evidence-backed preset until the ML supplies a learned profile.
        self.profile = profile or WakeProfile.evidence_default()

    def assess(
        self,
        frame: SensorFrame,
        recent: List[SensorFrame],
        now: datetime,
        target_temp_f: Optional[float] = None,
        sleep_hr_baseline: Optional[float] = None,
        minutes_since_onset: Optional[float] = None,
    ) -> WakeRisk:
        reasons: List[str] = []
        score = 0.0
        window = recent[-12:] if recent else []

        # 1) HR creeping above the sleep baseline (autonomic arousal precursor)
        base_hr = sleep_hr_baseline if sleep_hr_baseline is not None else _mean(
            [f.heart_rate for f in window[:-3]]) if len(window) > 4 else None
        if frame.heart_rate is not None and base_hr is not None:
            if frame.heart_rate >= base_hr + self.hr_creep:
                score += 0.28
                reasons.append("hr_creep")

        # 2) movement / restlessness rising
        if frame.movement is not None and frame.movement >= self.move_rise:
            score += 0.22
            reasons.append("restless")

        # 3) bed running warm vs target -- the dominant trigger for a hot sleeper
        if frame.bed_temp_f is not None and target_temp_f is not None:
            if frame.bed_temp_f >= target_temp_f + self.warm_margin_f:
                score += 0.30
                reasons.append("running_warm")
        if (self.profile.warm_temp_threshold_f is not None
                and frame.bed_temp_f is not None
                and frame.bed_temp_f >= self.profile.warm_temp_threshold_f):
            score += 0.18
            reasons.append("near_personal_warm_threshold")

        # 4) respiration irregular vs its recent self
        rrs = [f.respiratory_rate for f in window if f.respiratory_rate is not None]
        if len(rrs) >= 4 and frame.respiratory_rate is not None:
            sd = statistics.pstdev(rrs)
            if sd > 1.5:
                score += 0.12
                reasons.append("resp_irregular")

        # 5) vulnerable stage (light sleep / cycle boundary)
        if frame.stage is SleepStage.LIGHT:
            score += 0.10
            reasons.append("light_stage")

        # 6) learned recurring awakening time approaching (personalised)
        if self.profile.near_recurring_time(now):
            score += 0.20
            reasons.append("recurring_wake_window")

        # 7) evidence-based structural vulnerabilities (seeded, then tuned per-user):
        #    cycle boundaries, the lighter back half, and the circadian core-temp nadir —
        #    where a hot sleeper, with REM-suspended thermoregulation, is most exposed.
        if self.profile.near_cycle_boundary(minutes_since_onset):
            score += 0.10
            reasons.append("cycle_boundary")
        if self.profile.in_back_half(minutes_since_onset):
            score += 0.08
            reasons.append("back_half_of_night")
        if self.profile.in_circadian_danger_zone(now):
            score += 0.12
            reasons.append("circadian_nadir")

        score = min(1.0, score)
        # Only pre-empt in non-deep sleep (never jolt deep sleep, which is protective).
        preempt = (score >= self.preempt_threshold
                   and frame.stage is not SleepStage.DEEP)
        return WakeRisk(score=score, reasons=reasons, preempt=preempt)
