"""Ultradian sleep-cycle predictor — lets the wake orchestrator be ANTICIPATORY, not reactive.

Sleep runs in ~90-min NREM↔REM cycles; deep (slow-wave) dominates early cycles and thins out
toward morning, with light/REM ascents between. A reactive alarm waits to *see* light sleep; a
better one *predicts* the next light-sleep ascent and decides whether it's worth waiting for it
(a few minutes of patience can mean a near-zero-inertia wake instead of a forced deep-sleep wake).

This is a lightweight on-line estimator: it watches stage transitions through the night, learns
this night's typical deep-bout length, and predicts minutes-until-next-light. Heuristic, honest
about its confidence, and degrades gracefully when data is thin.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from sleepctl.models import SleepStage

_LIGHT = (SleepStage.LIGHT, SleepStage.AWAKE, SleepStage.REM)
_DEFAULT_DEEP_BOUT_MIN = 22.0      # typical slow-wave bout when we have no per-night estimate
_DEFAULT_CYCLE_MIN = 90.0


@dataclass
class CycleState:
    in_light: bool                      # currently in a light/REM/awake (liftable) phase
    minutes_to_next_light: float        # 0 if already light; estimate if in deep
    minutes_in_stage: float
    typical_deep_bout_min: float
    confidence: float                   # 0..1 (grows with observed transitions)

    def to_dict(self) -> dict:
        return {"in_light": self.in_light,
                "minutes_to_next_light": round(self.minutes_to_next_light, 1),
                "minutes_in_stage": round(self.minutes_in_stage, 1),
                "typical_deep_bout_min": round(self.typical_deep_bout_min, 1),
                "confidence": round(self.confidence, 2)}


class SleepCyclePredictor:
    """Stateful per-night. Call ``observe`` each tick; ``predict`` for the current estimate."""

    def __init__(self, cycle_min: float = _DEFAULT_CYCLE_MIN) -> None:
        self.cycle_min = cycle_min
        self._transitions: List[Tuple[datetime, SleepStage]] = []   # stage-change points
        self._deep_bouts: List[float] = []                          # observed deep-bout minutes
        self._deep_entered: Optional[datetime] = None

    def reset(self) -> None:
        self._transitions.clear()
        self._deep_bouts.clear()
        self._deep_entered = None

    def observe(self, now: datetime, stage: SleepStage) -> None:
        if not self._transitions or self._transitions[-1][1] is not stage:
            # close out a deep bout when leaving deep
            if self._deep_entered is not None and stage is not SleepStage.DEEP:
                self._deep_bouts.append((now - self._deep_entered).total_seconds() / 60.0)
                self._deep_entered = None
            if stage is SleepStage.DEEP and self._deep_entered is None:
                self._deep_entered = now
            self._transitions.append((now, stage))

    def _typical_deep_bout(self) -> float:
        if not self._deep_bouts:
            return _DEFAULT_DEEP_BOUT_MIN
        s = sorted(self._deep_bouts)
        return s[len(s) // 2]                       # median observed bout this night

    def predict(self, now: datetime, current_stage: SleepStage) -> CycleState:
        stage_start = self._transitions[-1][0] if self._transitions else now
        minutes_in_stage = (now - stage_start).total_seconds() / 60.0
        typical = self._typical_deep_bout()
        # confidence grows with how much of the night we've actually observed (transitions/bouts).
        conf = min(1.0, 0.2 + 0.1 * len(self._transitions) + 0.15 * len(self._deep_bouts))

        if current_stage in _LIGHT:
            return CycleState(True, 0.0, minutes_in_stage, typical, conf)

        # In deep: predict time until this slow-wave bout ends (next light ascent).
        remaining = max(0.0, typical - minutes_in_stage)
        return CycleState(False, remaining, minutes_in_stage, typical, conf)
