"""Accurate sleep-onset detection — telling *asleep* from *lying in bed awake*.

A single light-sleep label (or a drowsy dip) is not sleep onset: quiet wakefulness is
easily misread as N1 by any staging model. So, like the wake detector, this votes across
several independent physiological signals AND requires the sleep state to *persist* before
declaring onset — mirroring the clinical "persistent sleep" rule (onset = the start of the
first sustained run of sleep, not the first stray sleep epoch).

We strategically combine EVERY reliable Pod-2 signal (ballistocardiography gives HR, HRV,
respiration, movement, plus the staging model's label + confidence), each contributing an
independent vote — and we exploit the *shape* of the signals, not just thresholds:

  - stage is asleep (light/deep/REM) with adequate staging confidence
  - heart rate drops below the awake-in-bed baseline (vagal slowing at onset) ...
  - ... and is trending DOWN across the window (onset is a progressive decline)
  - HRV rises above the awake baseline (parasympathetic activation)
  - respiration slows below baseline ...
  - ... and becomes REGULAR — low breath-to-breath variability is one of the strongest
    discriminators of true sleep from quiet wakefulness (awake breathing is irregular)
  - movement falls to stillness

Reliability gating: the Pod's BCG-derived HR/HRV/RR are only trustworthy when the body is
still, so a high-movement sample cannot confirm onset (it resets the run) and the staging
label alone is never enough. Bed temperature is deliberately NOT used — it is actively driven
by the controller's own heating, so it is confounded.

When a run of qualifying samples lasts >= ``persistence_min`` minutes, onset is confirmed and
**back-dated to the start of that run**, so sleep-onset latency reflects when you actually
fell asleep — not when you got into bed. This keeps the controller, the cycle plan, and the
metrics from being fooled by time spent lying awake.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from sleepctl.models import SensorFrame, SleepStage


def _mean(values) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return statistics.fmean(vals) if vals else None


def _cv(values) -> Optional[float]:
    """Coefficient of variation (sd/mean) — low = regular signal."""
    vals = [v for v in values if v is not None]
    if len(vals) < 3:
        return None
    m = statistics.fmean(vals)
    if m == 0:
        return None
    return statistics.pstdev(vals) / m


@dataclass
class SleepOnsetEvent:
    timestamp: datetime          # confirmed (back-dated) onset time
    confidence: float
    signals: List[str]
    latency_min: Optional[float] = None  # bed-entry -> onset, if bed entry known


class SleepOnsetDetector:
    """Stateful, multi-signal, persistence-gated sleep-onset detector."""

    def __init__(self, cfg=None) -> None:
        t = getattr(cfg, "tunables", None)
        self.min_signals = getattr(t, "onset_min_signals", 3)
        self.persistence_min = getattr(t, "onset_persistence_min", 10)
        self.hr_drop = getattr(t, "onset_hr_drop_bpm", 3.0)
        self.still_move = getattr(t, "onset_still_movement", 0.15)
        self.move_unreliable = getattr(t, "onset_movement_unreliable", 0.45)
        self.hrv_rise_frac = getattr(t, "onset_hrv_rise_frac", 0.08)
        self.min_stage_conf = getattr(t, "onset_min_stage_conf", 0.4)
        self.resp_regular_cv = getattr(t, "onset_resp_regular_cv", 0.06)
        # internal state
        self._run_start: Optional[datetime] = None
        self._run_len = 0
        self._confirmed: Optional[SleepOnsetEvent] = None

    @property
    def onset_time(self) -> Optional[datetime]:
        return self._confirmed.timestamp if self._confirmed else None

    def reset(self) -> None:
        self._run_start = None
        self._run_len = 0
        self._confirmed = None

    def _awake_baseline(self, recent: List[SensorFrame]) -> dict:
        """Estimate the awake-in-bed baseline from recent AWAKE frames."""
        awake = [f for f in recent if f.stage is SleepStage.AWAKE]
        pool = awake if len(awake) >= 3 else recent  # fall back to whole window
        return {
            "hr": _mean([f.heart_rate for f in pool]),
            "rr": _mean([f.respiratory_rate for f in pool]),
            "hrv": _mean([f.hrv for f in pool]),
            "rr_cv": _cv([f.respiratory_rate for f in pool]),
        }

    def _signals(self, frame: SensorFrame, base: dict, recent: List[SensorFrame]) -> List[str]:
        sig: List[str] = []
        if frame.stage in (SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM):
            if frame.stage_confidence is None or frame.stage_confidence >= self.min_stage_conf:
                sig.append("asleep_stage")
        if frame.heart_rate is not None and base["hr"] is not None:
            if frame.heart_rate <= base["hr"] - self.hr_drop:
                sig.append("hr_drop")
        # HR trending down across the window (onset is a progressive decline, not a blip)
        win_hr = [f.heart_rate for f in recent[-8:]]
        early, late = win_hr[: max(1, len(win_hr) // 2)], win_hr[len(win_hr) // 2:]
        e, l = _mean(early), _mean(late)
        if e is not None and l is not None and l <= e - 1.0:
            sig.append("hr_trend_down")
        if frame.movement is not None and frame.movement <= self.still_move:
            sig.append("stillness")
        if frame.respiratory_rate is not None and base["rr"] is not None:
            if frame.respiratory_rate <= base["rr"] - 0.5:
                sig.append("respiration_slowed")
        # Respiratory REGULARITY: breathing steadies markedly at sleep onset. A low recent CV
        # vs the (more variable) awake baseline is a strong, movement-robust sleep signal.
        recent_cv = _cv([f.respiratory_rate for f in recent[-6:]] + [frame.respiratory_rate])
        if recent_cv is not None and recent_cv <= self.resp_regular_cv:
            if base["rr_cv"] is None or recent_cv <= base["rr_cv"] * 0.8:
                sig.append("respiration_regular")
        if frame.hrv is not None and base["hrv"]:
            if frame.hrv >= base["hrv"] * (1.0 + self.hrv_rise_frac):
                sig.append("hrv_rise")
        return sig

    def evaluate(
        self,
        frame: SensorFrame,
        recent: List[SensorFrame],
        now: datetime,
        bed_entry_time: Optional[datetime] = None,
    ) -> Optional[SleepOnsetEvent]:
        """Feed one sample. Returns the confirmed onset event once (then on every later
        call), or None while still awake / not yet persistent."""
        if self._confirmed is not None:
            return self._confirmed

        # Must be in bed to fall asleep.
        if frame.presence is False:
            self._run_start, self._run_len = None, 0
            return None

        # Reliability gate: the BCG HR/HRV/RR are only valid when still. A high-movement
        # sample can't be sleep onset and breaks the run, regardless of the stage label.
        if frame.movement is not None and frame.movement > self.move_unreliable:
            self._run_start, self._run_len = None, 0
            return None

        base = self._awake_baseline(recent or [])
        sig = self._signals(frame, base, recent or [])
        qualifies = (
            frame.stage in (SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM)
            and len(sig) >= self.min_signals
        )

        if qualifies:
            if self._run_start is None:
                self._run_start = frame.timestamp or now
            self._run_len += 1
            elapsed = (now - self._run_start).total_seconds() / 60.0
            if elapsed >= self.persistence_min - 1e-9:
                latency = None
                if bed_entry_time is not None:
                    latency = max(0.0, (self._run_start - bed_entry_time).total_seconds() / 60.0)
                self._confirmed = SleepOnsetEvent(
                    timestamp=self._run_start,
                    confidence=min(1.0, len(sig) / 5.0),
                    signals=sig,
                    latency_min=latency,
                )
                return self._confirmed
        else:
            # broke the run (a stray sleep label or a return to wakefulness) -> reset
            self._run_start, self._run_len = None, 0
        return None
