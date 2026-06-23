"""Personalization config for sleepctl.

Defaults are tailored to the target user: 5'9"/190 lb hot sleeper, back/side sleeper,
needs silence, primary problem is staying asleep (sleep maintenance), late-night worker
with variable early wake times. ``from_yaml`` lets these be overridden per deployment.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass


# Control-policy priority order: when signals conflict, earlier wins.
CONTROL_PRIORITY: list[str] = [
    "sleep_maintenance",
    "stage_confidence",
    "hrv_hr_trend",
    "sleep_opportunity",
    "deep_sleep",
    "sleep_efficiency",
    "room_temp",
    "secondary_context",
]


@dataclass
class UserProfile:
    height_in: int = 69
    weight_lb: int = 190
    hot_sleeper: bool = True
    positions: list[str] = field(default_factory=lambda: ["back", "side"])
    needs_silence: bool = True
    primary_issue: str = "sleep_maintenance"


@dataclass
class Benchmarks:
    total_sleep_min_target: int = 480
    deep_min_min: int = 90
    deep_min_ideal: int = 108
    rem_min_min: int = 90
    rem_min_ideal: int = 120
    sleep_efficiency_min: float = 0.85
    sleep_efficiency_ideal: float = 0.90
    wake_events_ideal: int = 1
    wake_events_max: int = 2
    onset_latency_min: int = 10
    onset_latency_max: int = 20
    hrv_target_ms: int = 70
    # Escalation thresholds from Eight Sleep's Autopilot RCT (SLEEP 2024): if the prior
    # night fell below these stage fractions, increase the temperature-offset magnitude.
    deep_pct_floor: float = 0.15  # deep sleep < 15% of the night
    rem_pct_floor: float = 0.20   # REM sleep  < 20% of the night


@dataclass
class Tunables:
    max_step_f: float = 2.0  # max temperature change per correction
    min_hold_minutes: int = 20  # hold a change this long before re-evaluating in-night
    min_hold_nights: int = 3  # nights before judging an intervention across nights
    variability_cap_f: float = 3.0  # cap total thermal swing within a window
    wake_window_min: int = 30  # smart-wake window before required wake time
    induction_minutes_normal: int = 30
    induction_minutes_short: int = 15
    stale_data_seconds: int = 420  # ~7 min; refuse to act on data older than this
    wake_recovery_minutes: int = 20
    # Accurate sleep-onset detection (asleep vs lying in bed awake). Onset is only declared
    # after a *persistent* run of multi-signal sleep evidence; onset is back-dated to its
    # start so latency reflects when you actually fell asleep.
    onset_persistence_min: int = 10     # sustained sleep required (clinical persistent-sleep)
    onset_min_signals: int = 3          # of: asleep stage, HR drop, stillness, slowed resp, HRV rise
    onset_hr_drop_bpm: float = 3.0      # HR below awake-in-bed baseline
    onset_still_movement: float = 0.15  # movement at/under this = stillness
    onset_movement_unreliable: float = 0.45  # above this, BCG HR/HRV/RR are untrustworthy
    onset_hrv_rise_frac: float = 0.08   # HRV this fraction above awake baseline
    onset_min_stage_conf: float = 0.4   # ignore low-confidence stage labels
    onset_resp_regular_cv: float = 0.06  # breathing-rate CV at/under this = regular (asleep)
    hot_sleeper_cool_bias_f: float = -1.5
    alarm_vibration_enabled: bool = False  # silence during sleep: no audio alarms
    # Smart wake: heat + gentle VIBRATION at the optimal (light-sleep) moment. Vibration is
    # tactile, not audio, so "silence" is preserved. Audio is never used.
    wake_vibration_enabled: bool = True
    wake_vibration_power: int = 50  # 0-100; gentle default
    # Manual-override learning: how strongly the learned setpoint is anchored toward the
    # user's repeated manual temperature choices (revealed preference), per nightly update.
    manual_preference_gain: float = 0.5  # fraction of (manual_median - current) applied
    manual_preference_min_count: int = 3  # need this many manual overrides before anchoring
    # Target WATER temperatures on the real Eight Sleep 55-110 °F scale (level 0 ~= 81 °F).
    # For a hot sleeper these sit on the cool side: 70 °F -> level ~-49, 66 °F -> ~-68.
    neutral_temp_f: float = 70.0
    deep_bias_temp_f: float = 66.0
    wake_ramp_temp_f: float = 74.0
    rem_warm_offset_f: float = 1.5  # small warm bias in REM (Autopilot RCT) above neutral
    level_min: int = -100
    level_max: int = 100
    # Composite (effective) temperature control. Effective comfort is a blend of the
    # COVERED body (bed surface temp) and EXPOSED skin (room/ambient air):
    #   effective = composite_bed_weight*bed + (1-composite_bed_weight)*ambient.
    # A proportional loop nudges the water temp to drive effective -> target.
    composite_bed_weight: float = 0.75   # ~25% of comfort attributed to exposed skin
    composite_feedback_gain: float = 0.6  # °F water step per °F effective error (slew-capped)
    # Actuation latency: minutes from a water-temp command until the bed meaningfully
    # responds. The control loop is latency-aware — it damps fresh corrections while the
    # previous command is still taking effect (prevents overshoot/oscillation), and
    # time-targeted ramps (wake) start this many minutes early. Learned per-user; this is
    # the default/floor.
    thermal_response_lag_min: float = 12.0
    # Outdoor weather is only an ambient FALLBACK when the Pod reports no bed/room temp.
    weather_enabled: bool = True
    weather_latitude: float = 42.3601   # Boston, MA
    weather_longitude: float = -71.0589


@dataclass
class MLConfig:
    """Gates + hyperparameters for the self-learning module (conservative defaults)."""

    min_nights: int = 14          # data-sufficiency gate before ML may act
    conf_min: float = 0.35        # minimum model confidence to act at all
    base_margin: float = 0.5      # reward improvement required (scaled by 1/confidence)
    lookahead_nights: int = 2     # K-night reward attribution for delayed effects
    ridge_lambda: float = 1.0
    retrain_window_nights: int = 60


@dataclass
class AppConfig:
    profile: UserProfile = field(default_factory=UserProfile)
    benchmarks: Benchmarks = field(default_factory=Benchmarks)
    tunables: Tunables = field(default_factory=Tunables)
    ml: MLConfig = field(default_factory=MLConfig)

    @classmethod
    def default(cls) -> "AppConfig":
        return cls()

    def default_setpoints(self):
        """Build the starting (learnable) SetpointProfile from these tunables."""
        from sleepctl.models import SetpointProfile

        t = self.tunables
        return SetpointProfile(
            neutral_f=t.neutral_temp_f,
            deep_bias_f=t.deep_bias_temp_f,
            rem_warm_offset_f=t.rem_warm_offset_f,
            wake_ramp_f=t.wake_ramp_temp_f,
            composite_bed_weight=t.composite_bed_weight,
            version=0,
            source="default",
        )

    @classmethod
    def from_yaml(cls, path) -> "AppConfig":
        """Load overrides from a YAML file; missing file -> defaults.

        YAML may contain top-level keys ``profile``, ``benchmarks``, ``tunables``,
        each a mapping of field -> value. Unknown keys are ignored.
        """
        import os

        cfg = cls.default()
        if not path or not os.path.exists(path):
            return cfg
        import yaml  # imported lazily so the module loads without PyYAML

        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

        for section_name, section_obj in (
            ("profile", cfg.profile),
            ("benchmarks", cfg.benchmarks),
            ("tunables", cfg.tunables),
            ("ml", cfg.ml),
        ):
            overrides = data.get(section_name) or {}
            if not is_dataclass(section_obj):
                continue
            valid = {f.name for f in fields(section_obj)}
            for key, value in overrides.items():
                if key in valid:
                    setattr(section_obj, key, value)
        return cfg
