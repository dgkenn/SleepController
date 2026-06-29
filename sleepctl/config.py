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
    # Post-wake circadian light dose: hold the dawn bulbs bright + the therapy lamp ON for this
    # many minutes AFTER you've surfaced, then stand them down. Dawn-simulation trials hold light
    # for ~20 min past wake to lock in alertness (Gabel 2014; SAD light-box dosing 30–60 min).
    post_wake_light_min: int = 20
    # Active cool-snap on waking (OPT-IN, not yet wired): once you're confirmed up, briefly drop
    # the bed cool to kill residual sleepiness via a cool-skin alerting stimulus (Te Lindert & Van
    # Someren 2018) — plausibly well-suited to a hot sleeper. Off by default; flipping this on is a
    # no-op until the cooling maneuver is implemented (the flag is plumbed so the wiring is a small
    # follow-up, and the per-person thermal_wake learner can later own the magnitude/direction).
    wake_cold_snap_enabled: bool = False
    wake_cold_snap_f: float = 62.0   # intended post-wake cool target (placeholder until wired)
    induction_minutes_normal: int = 30
    induction_minutes_short: int = 15
    # On-demand onset induction: a small WARM nudge speeds sleep onset (cutaneous warming,
    # Raymann/Van Someren). Kept small + comfort-capped for a hot sleeper, then cooled once
    # asleep. ``onset_warm_nudge_f`` is °F above neutral; the cap bounds it.
    onset_warm_nudge_f: float = 1.0
    onset_warm_comfort_cap_f: float = 2.0
    # Nap mode thresholds (literature-backed: Brooks & Lack 2006; Patterson 2023).
    nap_power_max_min: int = 25      # <= this -> power nap (stay light, avoid SWS, cap wake)
    nap_cycle_min_min: int = 60      # >= this (up to ~110) -> full-cycle nap, smart-wake light
    nap_cycle_target_min: int = 90   # one NREM-REM cycle
    nap_late_hour: int = 16          # naps starting at/after this hour can erode night sleep
    nap_inertia_buffer_min: int = 20 # advise this buffer before anything critical post-nap
    stale_data_seconds: int = 420  # ~7 min; refuse to act on data older than this
    wake_recovery_minutes: int = 20
    # Live telemetry cadence. The dashboard snapshot (sensor + device health) is refreshed
    # on a fast, decoupled tick so the UI never shows data older than this — independent of
    # the slower control-decision cadence. Kept under the Eight Sleep cloud's own ~30s
    # user-data update floor; polling faster catches each new cloud value sooner (the
    # diminishing-returns floor is the cloud itself — true sub-30s telemetry needs Tier 1
    # raw capture). The heavier device-data poll (water/online/priming) stays slower.
    live_telemetry_seconds: float = 15.0
    live_device_refresh_seconds: float = 60.0
    telemetry_stale_seconds: float = 30.0  # flag the snapshot if sensor data exceeds this age
    # Thermal-response health check: confirm the bed is ACTUALLY heating/cooling using the
    # Hub's own water-derived `device_level` (NOT cover-side bed temp, which tracks ambient).
    # Verified live: under max cool the device level fell ~5 levels/min; under heat it climbed
    # to +100. A flat device level while commanded to change => fault (low water/cover/hardware).
    thermal_at_target_margin: int = 8       # |target-device| <= this => at setpoint (healthy)
    thermal_response_window_min: int = 8     # window over which to judge progress toward target
    thermal_min_progress_levels: int = 5     # min level movement toward target to count responsive
    # Predictive awakening pre-emption: detect the slow pre-arousal DRIFT (trends over a short
    # window) before a full awakening, to buy lead time for a gentle SETTLE_COOL nudge.
    precursor_window_min: float = 4.0          # rolling window for trend fits
    precursor_hr_creep_slope: float = 0.6      # bpm/min rise => autonomic arousal building
    precursor_hrv_decay_slope: float = -0.8    # ms/min fall => sympathetic shift
    precursor_move_rise_slope: float = 0.02    # /min rise in micro-movement => restlessness
    precursor_bed_warm_slope: float = 0.15     # °F/min bed warming trend
    precursor_resp_cv_rise: float = 0.08       # breathing-rate CV => losing regularity
    precursor_preempt_threshold: float = 0.40  # combined score that triggers a pre-empt
    # Evidence (Busek 2005, PMID 16163654): a rise in HRV spectral energy is the EARLIEST,
    # strongest precursor of cortical arousal -> weight HRV decay highest, HR/movement next.
    precursor_w_hrv: float = 0.26
    precursor_w_hr: float = 0.18
    precursor_w_move: float = 0.20
    precursor_w_bed: float = 0.16
    precursor_w_resp: float = 0.10
    # Sleep-instability (CAP-rate proxy): density of micro-movement bursts in the window.
    # In unstable windows (Zucconi 1995, PMID 7797629) awakenings cluster, so pre-empt sooner.
    precursor_instability_move: float = 0.25   # movement above this = a burst
    precursor_instability_gain: float = 0.12   # lower the pre-empt threshold by up to this
    # Maintenance "settle" nudge: a SMALL, comfort-bounded thermal move at a vulnerable moment.
    # SIGNED + learnable per phenotype: cutaneous warming can suppress awakenings (Raymann 2008,
    # DOI 10.1093/brain/awm315) yet over-cooling drives alertness (Fronczek 2008,
    # DOI 10.1093/sleep/31.2.233) -> the controller learns the sign/magnitude that prevents
    # THIS user's awakenings. Default cool (hot sleeper), bounded by the cap.
    maintenance_settle_nudge_f: float = -1.0   # <0 cooler, >0 warmer (relative to neutral)
    maintenance_settle_cap_f: float = 2.0
    # Environmental pre-compensation: feed-forward bed bias from tonight's outdoor forecast so
    # the bed is ahead of an overnight heat soak (hot sleeper) instead of chasing it.
    precomp_hot_threshold_f: float = 62.0      # overnight mean outdoor above this => cool bias
    precomp_cold_threshold_f: float = 40.0     # below this => warm bias
    precomp_f_per_deg: float = 8.0             # °F outdoor per 1°F of bed bias
    precomp_max_bias_f: float = 2.0            # cap the feed-forward bias
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
    # In-night architecture steering ("nudge me deeper"). A bounded, awakening-risk-VETOED
    # fast loop inside MAINTENANCE: when the realized deep curve is behind its front-loaded
    # ideal AND you're in light sleep AND wake-risk is low, drive the bed toward the deep
    # setpoint to bias you deeper (Autopilot RCT: cooler -> more deep). Slew/variability/clamp
    # still bound everything; it never fights an awakening. Asymmetric by design — see
    # docs/ARCHITECTURE_STEERING.md. The deepen maneuver is the workhorse and ON by default;
    # the back-third REM-unblock ("nudge lighter") is OFF until A/B proves it per person.
    inight_steering_enabled: bool = True
    steer_deepen_max_fraction: float = 0.6   # only deepen in the front ~60% of the night (SWS is
                                             # front-loaded; deep is barely steerable late)
    steer_deepen_min_deficit_min: float = 8.0  # require a real deep deficit before nudging
    steer_response_horizon_min: float = 20.0   # window to score the maneuver's stage response
    steer_deep_front_p: float = 0.6          # deep cumulative-ideal exponent (<1 = front-loaded)
    steer_rem_back_q: float = 1.6            # REM cumulative-ideal exponent (>1 = back-loaded)
    steer_rem_unblock_enabled: bool = False  # the off-by-default "nudge lighter" REM-unblock
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
