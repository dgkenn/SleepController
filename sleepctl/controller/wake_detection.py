"""Multi-signal wake detection.

The user's primary problem is staying asleep, so awakenings are a first-class error
signal. We declare a *probable* awakening only when several independent signals fire
together (voting), and otherwise do nothing dramatic (return None -> hold). This makes
the detector robust to noisy single-signal blips.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Optional

from sleepctl.models import SensorFrame, SleepStage, WakeEvent


def _finite(values: list[float]) -> list[float]:
    """Drop None and non-finite (NaN/Inf) values — a bad sensor reading must not poison the stats
    (statistics.pstdev raises on Inf, and NaN silently corrupts every downstream comparison)."""
    return [v for v in values if v is not None and math.isfinite(v)]


def _mean(values: list[float]) -> Optional[float]:
    vals = _finite(values)
    return statistics.fmean(vals) if vals else None


def _stdev(values: list[float]) -> float:
    vals = _finite(values)
    return statistics.pstdev(vals) if len(vals) >= 2 else 0.0


class WakeDetector:
    """Votes across signals to decide whether an awakening is occurring."""

    def __init__(self, min_signals: int = 3) -> None:
        self.min_signals = min_signals

    def evaluate(
        self,
        frame: SensorFrame,
        recent: list[SensorFrame],
        now: Optional[datetime] = None,
    ) -> Optional[WakeEvent]:
        signals: list[str] = []
        # Use a rolling baseline from the recent (pre-this-frame) window.
        window = recent[-10:] if recent else []

        hrs = [f.heart_rate for f in window]
        movements = [f.movement for f in window]
        rrs = [f.respiratory_rate for f in window]
        confs = [f.stage_confidence for f in window]

        base_hr = _mean(hrs)
        base_move = _mean(movements)
        base_rr_sd = _stdev(rrs)
        base_conf = _mean(confs)

        # 1) movement spike vs baseline
        if frame.movement is not None and base_move is not None:
            if frame.movement > base_move + max(0.15, 2 * _stdev(movements)):
                signals.append("movement_spike")
        elif frame.movement is not None and base_move is None and frame.movement > 0.5:
            signals.append("movement_spike")

        # 2) rising heart rate
        if frame.heart_rate is not None and base_hr is not None:
            if frame.heart_rate > base_hr + 5.0:
                signals.append("hr_rise")

        # 3) drop in stage confidence
        if frame.stage_confidence is not None and base_conf is not None:
            if frame.stage_confidence < base_conf - 0.2:
                signals.append("confidence_drop")

        # 4) return to awake/light from deeper sleep
        prev_stage = window[-1].stage if window else SleepStage.UNKNOWN
        if frame.stage in (SleepStage.AWAKE, SleepStage.LIGHT) and prev_stage in (
            SleepStage.DEEP,
            SleepStage.REM,
        ):
            signals.append("stage_regression")
        if frame.stage is SleepStage.AWAKE:
            signals.append("awake_stage")

        # 5) increased respiratory variability
        if frame.respiratory_rate is not None and base_rr_sd > 0:
            recent_rr = [f.respiratory_rate for f in window[-3:] if f.respiratory_rate]
            if recent_rr and _stdev(recent_rr + [frame.respiratory_rate]) > 1.8 * base_rr_sd:
                signals.append("resp_variability")

        # 6) sudden break in a stable low-motion pattern
        if base_move is not None and base_move < 0.1 and frame.movement is not None:
            if frame.movement > 0.4:
                signals.append("low_motion_break")

        # de-duplicate while preserving order
        signals = list(dict.fromkeys(signals))

        if len(signals) >= self.min_signals:
            confidence = min(1.0, len(signals) / 5.0)
            return WakeEvent(
                timestamp=frame.timestamp,
                confidence=confidence,
                signals=signals,
            )
        return None
