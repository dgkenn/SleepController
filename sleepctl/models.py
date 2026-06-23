"""Shared data model for sleepctl.

These dataclasses and enums are the stable contract every other module depends on:
adapters produce ``SensorFrame``/``NightSummary``/``ContextRecord``, the controller
emits ``Decision``/``Intervention``/``WakeEvent``, and storage persists all of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# --------------------------------------------------------------------------- enums


class SleepStage(Enum):
    AWAKE = "awake"
    LIGHT = "light"
    DEEP = "deep"
    REM = "rem"
    UNKNOWN = "unknown"


class ControllerState(Enum):
    IDLE = "idle"
    CALIBRATION = "calibration"
    INDUCTION = "induction"
    MAINTENANCE = "maintenance"
    WAKE_RECOVERY = "wake_recovery"
    WAKE_WINDOW = "wake_window"


class NightObjective(Enum):
    """Per-night optimisation objective, selected from the schedule / night mode."""

    OPTIMIZE = "optimize"            # normal night: hit all benchmarks
    DAMAGE_CONTROL = "damage_control"  # short work night: max quality per hour
    RECOVERY = "recovery"           # off day / sleep-debt payback: max total recovery


class CorrectionAction(Enum):
    HOLD = "hold"
    WARMER = "warmer"
    COOLER = "cooler"
    ESCALATE = "escalate"
    REVERT = "revert"


class ThermalIntent(Enum):
    WIND_DOWN = "wind_down"
    INDUCTION_COOL = "induction_cool"
    DEEP_BIAS_COOL = "deep_bias_cool"
    REM_NEUTRAL = "rem_neutral"
    WAKE_RAMP = "wake_ramp"
    STABILIZE = "stabilize"
    NEUTRAL = "neutral"


# ---------------------------------------------------------------------- dataclasses


@dataclass
class SensorFrame:
    """One sampled moment of in-bed physiology + thermal state.

    Eight Sleep cloud data is delivered with latency, so ``data_age_seconds`` lets the
    controller refuse to act on stale data.
    """

    timestamp: datetime
    stage: SleepStage = SleepStage.UNKNOWN
    stage_confidence: Optional[float] = None
    heart_rate: Optional[float] = None
    hrv: Optional[float] = None
    respiratory_rate: Optional[float] = None
    movement: Optional[float] = None
    presence: Optional[bool] = None
    bed_temp_f: Optional[float] = None
    room_temp_f: Optional[float] = None
    commanded_level: Optional[int] = None  # last -100..100 device level sent
    data_age_seconds: Optional[float] = None

    def is_stale(self, max_age_seconds: float) -> bool:
        """True when freshness is unknown or older than ``max_age_seconds``."""
        if self.data_age_seconds is None:
            return True
        return self.data_age_seconds > max_age_seconds


@dataclass
class WakeEvent:
    """A detected (probable) awakening, with the signals that voted for it."""

    timestamp: datetime
    confidence: float
    signals: list[str] = field(default_factory=list)


@dataclass
class Decision:
    """The controller's output for a single tick — also the per-tick output contract."""

    timestamp: datetime
    state: ControllerState
    objective: NightObjective
    thermal_intent: ThermalIntent
    target_temp_f: float
    target_level: int  # -100..100 device level
    action: CorrectionAction
    reason: str
    confidence: float
    log_payload: dict = field(default_factory=dict)


@dataclass
class Intervention:
    """A thermal correction the controller made, tracked for the learning loop."""

    timestamp: datetime
    state: ControllerState
    action: CorrectionAction
    magnitude_f: float
    reason: str
    held: Optional[bool] = None
    reverted: Optional[bool] = None
    outcome_delta: Optional[float] = None


@dataclass
class NightSummary:
    """End-of-night rollup (layer 2 of the dataset)."""

    date: str  # ISO date
    bedtime: Optional[datetime] = None
    wake_time: Optional[datetime] = None
    total_sleep_min: Optional[float] = None
    sleep_onset_latency_min: Optional[float] = None
    deep_min: Optional[float] = None
    rem_min: Optional[float] = None
    light_min: Optional[float] = None
    wake_events: Optional[int] = None
    waso_min: Optional[float] = None
    sleep_efficiency: Optional[float] = None
    avg_hr: Optional[float] = None
    avg_hrv: Optional[float] = None
    avg_respiratory_rate: Optional[float] = None
    temp_profile_summary: dict = field(default_factory=dict)
    intervention_summary: dict = field(default_factory=dict)
    setpoint_version: Optional[int] = None  # which SetpointProfile produced this night
    outcome_score: Optional[float] = None   # computed multi-objective reward for the night


@dataclass
class ContextRecord:
    """Daytime / schedule antecedents (layer 3 of the dataset)."""

    date: str  # ISO date
    required_wake_time: Optional[datetime] = None
    work_start_time: Optional[datetime] = None
    first_commitment: Optional[datetime] = None
    outdoor_temp_f: Optional[float] = None  # ambient context (weather), for comfort offset
    sleep_opportunity_min: Optional[float] = None
    is_short_sleep_day: Optional[bool] = None
    schedule_variable: Optional[bool] = None
    steps: Optional[int] = None
    workout_timing: Optional[datetime] = None
    workout_intensity: Optional[float] = None
    resting_hr_trend: Optional[float] = None
    hr_recovery: Optional[float] = None
    strain: Optional[float] = None
    caffeine: Optional[bool] = None
    alcohol: Optional[bool] = None
    screen_time_min: Optional[float] = None
    stress: Optional[float] = None
    travel: Optional[bool] = None
    illness: Optional[bool] = None
    late_night_work: Optional[bool] = None
    routine_complete: Optional[bool] = None
    # Night mode hint: "work"/"constrained", "recovery"/"off", "normal", or None/"auto"
    # (inferred from the wake schedule + sleep debt). Drives the controller objective.
    night_type: Optional[str] = None
    # Subjective morning check-in labels (0-10), feed the reward modestly.
    subjective_quality: Optional[float] = None
    grogginess: Optional[float] = None
    daytime_performance: Optional[float] = None


@dataclass
class ActionRecord:
    """A learning action the ML/policy chose for a night, with its predictions + result.

    This closes the action -> outcome loop: ``predicted`` are the model's expected effects,
    ``reward_observed`` is filled in once the night's outcome is known.
    """

    date: str  # ISO night date
    action_name: str
    params: dict = field(default_factory=dict)
    predicted: dict = field(default_factory=dict)
    confidence: float = 0.0
    reward_observed: Optional[float] = None
    applied: bool = False
    source: str = "policy"  # "policy" | "ml" | "fallback"
    creates_version: Optional[int] = None  # the SetpointProfile version this action produced


@dataclass
class SetpointProfile:
    """The per-user, learnable composite-temperature setpoint — the object the learning
    loop (and, later, the ML model) tailors over time.

    Holds the **effective comfort** targets (°F, on the blended composite scale) and the
    blend weight. It is versioned and stamped with its ``source`` so every night's outcome
    can be attributed to the exact setpoint that produced it (clean ML training rows).
    """

    neutral_f: float
    deep_bias_f: float
    rem_warm_offset_f: float
    wake_ramp_f: float
    composite_bed_weight: float
    version: int = 0
    source: str = "default"  # "default" | "policy" | "ml"
    updated: Optional[datetime] = None


@dataclass
class Baselines:
    """Rolling 7/14-day statistics, keyed like ``"hrv_7d_median"``.

    Kept dict-based so the learning module can add metrics without a schema change.
    """

    metrics: dict[str, float] = field(default_factory=dict)
    updated: Optional[datetime] = None

    def get(self, key: str, default: Optional[float] = None) -> Optional[float]:
        return self.metrics.get(key, default)
