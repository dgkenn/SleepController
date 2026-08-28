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

    #: Signals derived from the STAGE ESTIMATOR rather than from a sensor directly. All three
    #: can fire from one observation -- the label moving to AWAKE out of DEEP/REM sets
    #: ``stage_regression`` AND ``awake_stage``, and the confidence wobble that usually
    #: accompanies it sets ``confidence_drop`` -- so a min_signals=3 quorum could be met
    #: entirely by one noisy estimate with no physiological corroboration at all.
    #:
    #: Measured on 2026-08-27: 25 of 51 wake ticks had neither elevated heart rate nor
    #: movement. Three votes from one estimator is one vote.
    STAGER_SIGNALS = frozenset({"stage_regression", "awake_stage", "confidence_drop"})

    def __init__(self, min_signals: int = 3, require_independent: bool = True, cfg=None) -> None:
        self.min_signals = min_signals
        self.cfg = cfg
        # At least one signal must come from OUTSIDE the stage estimator (movement, heart rate
        # or respiration). The stager is allowed to make the case; it is not allowed to be the
        # only witness.
        self.require_independent = require_independent

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

        # 6b) WEARABLE ACTIGRAPHY -- the armband's own PIM counts, read from the dense activity
        # history rather than the per-tick `movement` field. This is genuinely independent
        # physiological evidence and it is the best wake signal available (6/6 against
        # message-timestamp ground truth, versus the HR stager's 2/6).
        #
        # It has to be counted HERE rather than left to speak through the stage label: an
        # actigraphy wake expresses itself by driving the stage to AWAKE, which the independence
        # rule below classifies as stager-derived. Without this the rule would suppress exactly
        # the awakenings it should trust most.
        try:
            from sleepctl.controller.state_estimator import _actigraphy_wake
            if self.cfg is not None and _actigraphy_wake(frame, self.cfg):
                signals.append("actigraphy_motion")
        except Exception:
            pass

        # 6) sudden break in a stable low-motion pattern
        if base_move is not None and base_move < 0.1 and frame.movement is not None:
            if frame.movement > 0.4:
                signals.append("low_motion_break")

        # de-duplicate while preserving order
        signals = list(dict.fromkeys(signals))

        independent = [x for x in signals if x not in self.STAGER_SIGNALS]
        if self.require_independent and not independent:
            return None
        if len(signals) >= self.min_signals:
            confidence = min(1.0, len(signals) / 5.0)
            return WakeEvent(
                timestamp=frame.timestamp,
                confidence=confidence,
                signals=signals,
            )
        return None
