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
    hot_sleeper_cool_bias_f: float = -1.5
    alarm_vibration_enabled: bool = False  # silence: thermal-only smart wake
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
    # Outdoor weather is only an ambient FALLBACK when the Pod reports no bed/room temp.
    weather_enabled: bool = True
    weather_latitude: float = 42.3601   # Boston, MA
    weather_longitude: float = -71.0589


@dataclass
class AppConfig:
    profile: UserProfile = field(default_factory=UserProfile)
    benchmarks: Benchmarks = field(default_factory=Benchmarks)
    tunables: Tunables = field(default_factory=Tunables)

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
        ):
            overrides = data.get(section_name) or {}
            if not is_dataclass(section_obj):
                continue
            valid = {f.name for f in fields(section_obj)}
            for key, value in overrides.items():
                if key in valid:
                    setattr(section_obj, key, value)
        return cfg
