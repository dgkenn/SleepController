"""Predictive awakening pre-emption — trend-based precursor detection.

Sleep maintenance is the #1 goal: prevent awakenings, not just react to them. The existing
``WakeRiskAssessor`` scores risk from the *current* frame (HR above baseline, running warm,
circadian zone, recurring time). This adds the **leading-edge** signal: the slow
physiological *drift* in the minutes before an arousal — rising HR, decaying HRV, building
micro-movement, breathing losing regularity, the bed warming — fit as trends over a short
rolling window. Catching the drift earlier buys lead time to fire a gentle ``SETTLE_COOL``
nudge and abort the awakening before it happens.

Outputs feed ``SleepController._preempt_cool`` alongside the wake-risk assessor (a union: any
strong predictor pre-empts). Deliberately silent and conservative — never acts in deep sleep,
and ignores signals when movement says the BCG values are untrustworthy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from sleepctl.models import SensorFrame, SleepStage


def _slope_per_min(frames: List[SensorFrame], attr: str) -> Optional[float]:
    """Least-squares slope of ``attr`` vs time, in units per minute. None if too sparse."""
    pts = [(f.timestamp, getattr(f, attr)) for f in frames if getattr(f, attr) is not None]
    if len(pts) < 4:
        return None
    t0 = pts[0][0]
    xs = [(t - t0).total_seconds() / 60.0 for t, _ in pts]
    ys = [float(v) for _, v in pts]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom <= 1e-9:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def _cv(values: List[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    if len(vals) < 3:
        return None
    m = sum(vals) / len(vals)
    if m <= 1e-6:
        return None
    var = sum((v - m) ** 2 for v in vals) / len(vals)
    return (var ** 0.5) / m


@dataclass
class PrecursorAssessment:
    score: float                 # 0..1 leading-edge arousal pressure
    should_preempt: bool         # True -> add a SETTLE_COOL nudge now
    reasons: List[str] = field(default_factory=list)
    signals: dict = field(default_factory=dict)  # raw trend values, for transparency/logging

    def to_dict(self) -> dict:
        return {"score": round(self.score, 3), "should_preempt": self.should_preempt,
                "reasons": self.reasons, "signals": self.signals}


class PrecursorDetector:
    """Detects the slow pre-arousal drift over a short rolling window."""

    def __init__(self, cfg=None) -> None:
        t = getattr(cfg, "tunables", None)
        self.window_min = getattr(t, "precursor_window_min", 4.0)
        self.hr_creep_slope = getattr(t, "precursor_hr_creep_slope", 0.6)    # bpm/min
        self.hrv_decay_slope = getattr(t, "precursor_hrv_decay_slope", -0.8)  # ms/min
        self.move_rise_slope = getattr(t, "precursor_move_rise_slope", 0.02)  # /min
        self.bed_warm_slope = getattr(t, "precursor_bed_warm_slope", 0.15)    # °F/min
        self.resp_cv_rise = getattr(t, "precursor_resp_cv_rise", 0.08)        # CV threshold
        self.move_unreliable = getattr(t, "onset_movement_unreliable", 0.45)
        self.preempt_threshold = getattr(t, "precursor_preempt_threshold", 0.40)

    def detect(
        self,
        frame: SensorFrame,
        recent: List[SensorFrame],
        now: datetime,
        sleep_hr_baseline: Optional[float] = None,
        sleep_hrv_baseline: Optional[float] = None,
    ) -> PrecursorAssessment:
        # window of frames within the last `window_min` minutes (plus the current frame)
        cutoff = now.timestamp() - self.window_min * 60.0
        window = [f for f in (recent or []) if f.timestamp.timestamp() >= cutoff]
        window = (window + [frame])[-40:]
        score = 0.0
        reasons: List[str] = []
        signals: dict = {}

        # Movement reliability gate: high motion makes BCG HR/HRV/RR untrustworthy. Still use
        # the movement trend itself (restlessness IS a precursor), but down-weight autonomics.
        cur_move = frame.movement if frame.movement is not None else 0.0
        autonomic_ok = cur_move < self.move_unreliable

        hr_slope = _slope_per_min(window, "heart_rate")
        if autonomic_ok and hr_slope is not None:
            signals["hr_slope_bpm_min"] = round(hr_slope, 2)
            if hr_slope >= self.hr_creep_slope:
                score += 0.22
                reasons.append("hr_creep")

        hrv_slope = _slope_per_min(window, "hrv")
        if autonomic_ok and hrv_slope is not None:
            signals["hrv_slope_ms_min"] = round(hrv_slope, 2)
            if hrv_slope <= self.hrv_decay_slope:
                score += 0.18
                reasons.append("hrv_decay")

        move_slope = _slope_per_min(window, "movement")
        if move_slope is not None:
            signals["move_slope_per_min"] = round(move_slope, 3)
            if move_slope >= self.move_rise_slope:
                score += 0.22
                reasons.append("restlessness_building")

        bed_slope = _slope_per_min(window, "bed_temp_f")
        if bed_slope is not None:
            signals["bed_slope_f_min"] = round(bed_slope, 3)
            if bed_slope >= self.bed_warm_slope:
                score += 0.16
                reasons.append("bed_warming")

        if autonomic_ok:
            rr_cv = _cv([f.respiratory_rate for f in window[-8:]])
            if rr_cv is not None:
                signals["resp_cv"] = round(rr_cv, 3)
                if rr_cv >= self.resp_cv_rise:
                    score += 0.12
                    reasons.append("resp_irregular")

        score = min(1.0, score)
        # Never pre-empt out of deep sleep (protect slow-wave); require real evidence.
        should = score >= self.preempt_threshold and frame.stage is not SleepStage.DEEP
        return PrecursorAssessment(score=score, should_preempt=should,
                                   reasons=reasons, signals=signals)
