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
    # Active cool-snap on waking (OPT-IN): once you're CONFIRMED up, briefly run the bed cold to
    # kill residual sleepiness via a cool-skin alerting stimulus (Te Lindert & Van Someren 2018) —
    # the same lever that makes warm skin sleep-permissive, run in reverse. Plausibly well-suited
    # to a hot sleeper. OFF by default because it changes what the bed does around your alarm;
    # turning it on emits ThermalIntent.WAKE_COLD_SNAP during the post-wake phase, bounded by the
    # usual slew/clamp chain. Gated on CONFIRMED wake, so it can never cool you while asleep.
    wake_cold_snap_enabled: bool = False
    wake_cold_snap_f: float = 62.0   # post-wake cool target (absolute, not an offset from neutral)
    wake_cold_snap_min: int = 10     # how long to hold it after you surface
    induction_minutes_normal: int = 30
    induction_minutes_short: int = 15
    # On-demand onset induction: a small WARM nudge speeds sleep onset (cutaneous warming,
    # Raymann/Van Someren). Kept small + comfort-capped for a hot sleeper, then cooled once
    # asleep. ``onset_warm_nudge_f`` is °F above neutral; the cap bounds it.
    onset_warm_nudge_f: float = 1.0
    onset_warm_comfort_cap_f: float = 2.0
    # 3-phase onset induction (cold settle -> brief warm pulse -> consolidate cool). The opener
    # drives the bed genuinely cold to shed a hot sleeper's heat and prime peripheral
    # vasoconstriction, so the brief warm pulse yields a stronger vasodilation contrast (core-temp
    # drop -> sleepiness); it then cools again to consolidate. Durations are minutes-in-bed; on a
    # short night (DAMAGE_CONTROL) both phases are compressed (see induction.py). The really-cold
    # target itself lives on the learnable SetpointProfile as ``onset_cold_settle_f``.
    induction_cold_settle_min: int = 12
    induction_warm_pulse_min: int = 10
    onset_cold_settle_temp_f: float = 60.0  # really-cold opener target (seeds the profile)
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
    # 2, not 3: respiration is PAYWALLED on this account (0 of 17k samples ever carried one), so
    # `respiration_slowed` and `respiration_regular` can never fire -- 2 of the 7 signals, and the
    # code's own strongest sleep-vs-quiet-wake discriminators. That left only `asleep_stage` and
    # `stillness` dependable, so a 3-signal bar needed an intermittent third to hold for 10
    # UNBROKEN minutes and onset could never confirm (observed: a full night stuck in INDUCTION).
    # Compensated by the tighter stillness bar below. Revisit if RSA-derived respiration lands.
    onset_min_signals: int = 2          # of: asleep stage, HR drop, stillness, slowed resp, HRV rise
    onset_hr_drop_bpm: float = 3.0      # HR below awake-in-bed baseline
    onset_still_movement: float = 0.10  # movement at/under this = stillness (tightened from 0.15
                                        # to offset the lower onset_min_signals above)
    onset_movement_unreliable: float = 0.45  # above this, BCG HR/HRV/RR are untrustworthy
    onset_hrv_rise_frac: float = 0.08   # HRV this fraction above awake baseline
    onset_min_stage_conf: float = 0.4   # ignore low-confidence stage labels
    onset_resp_regular_cv: float = 0.06  # breathing-rate CV at/under this = regular (asleep)
    # --- vitals-based sleep-stage estimate (external HR sensor, e.g. Polar Verity Sense) --------
    # When the Pod provides no sleep stage (staging unavailable/paywalled) but a fresh HR feed is
    # present, derive a COARSE stage (AWAKE/LIGHT/DEEP; never REM) from HR/HRV/movement so onset
    # detection + all maintenance-time steering can engage off the wearable alone. Only overrides a
    # stage that is UNKNOWN -- a real Pod stage always wins -- and its confidence is capped below a
    # Pod label so downstream trust stays appropriately soft. See sleepctl/controller/state_estimator.py.
    estimate_stage_from_vitals: bool = True
    est_stage_awake_movement: float = 0.25   # movement at/above this => AWAKE
    est_stage_awake_hr_delta: float = 6.0    # HR this far above the sleep baseline => AWAKE
    est_stage_deep_hr_delta: float = -3.0    # HR this far BELOW the sleep baseline => DEEP-eligible
    est_stage_deep_movement: float = 0.06    # movement at/under this (sustained) => DEEP-eligible
    est_stage_deep_sustain: int = 4          # consecutive quiescent frames required to call DEEP
    est_stage_max_conf: float = 0.5          # cap heuristic-estimate confidence below a real Pod stage
    # Learned wearable stager (sleepctl/ml/sleep_staging, trained on PhysioNet sleep-accel). Preferred
    # over the heuristic above when its weights are bundled and enough HR history exists; falls back
    # to the heuristic otherwise. Confidence capped below a real Pod stage (staging from a wrist HR
    # feed is genuinely coarse -- see the CV metrics in docs). HR-only by default so it works with the
    # Verity alone; the iPhone's motion keeps feeding the wake/arousal detectors separately.
    use_learned_stager: bool = True
    stager_min_hr_samples: int = 5           # need at least this many recent HR samples to trust it
    est_model_conf_cap: float = 0.7          # cap the learned model's stage confidence
    # Feed a movement signal into the stager's HR+motion variant. Kept OFF -- for a MEASURED
    # reason rather than the original unit-mismatch worry. Final CV, all 31 subjects / 25,663
    # epochs (an earlier read of this used a 5,711-epoch partial set and is superseded):
    #
    #                        4-class k   wake k   deep MAE   onset MAE
    #   HR only                 0.436     0.450     23.0m       5.4m
    #   HR + motion             0.455     0.516     26.5m       6.7m
    #   HR + motion scale-free  0.444     0.536     28.5m       5.3m
    #
    # On the full data motion DOES help staging (+0.019 kappa) and wake (+0.066) -- more than it
    # did on the partial set -- but it still degrades the two outputs the controller actually
    # consumes: realized deep minutes (23.0 -> 26.5 min error, the architecture steering's input)
    # and onset timing (5.4 -> 6.7 min). The decision therefore stands, but it is now a genuine
    # trade rather than a clear win, and is worth revisiting per-user.
    #
    # Motion buys wake detection (+0.077 kappa) but degrades precisely what the stager's output is
    # USED for: 4-class staging, realized deep minutes (which the architecture steering compares
    # against the ideal curve) and onset timing. And the controller does not need the stager for
    # wake anyway -- its arousal / wake-risk / precursor detectors already consume movement
    # directly, so that gain is largely redundant while the staging loss is not.
    # Note the scale-free variant BEAT absolute counts even though the Verity now supplies
    # unit-matched actigraphy, contradicting the assumption that matched units would win --
    # normalizing within the night evidently generalizes better across people.
    # Revisit per-user once enough of the user's own nights exist to evaluate on them directly.
    stager_use_motion: bool = False
    # Let the interpretable (clock-free) heuristic upgrade a model "light" to DEEP when it has
    # positive physiological evidence -- sustained stillness plus HR below the trailing sleep
    # baseline. The learned stager's deep emission is suppressed by its own clock features after
    # the first ~100 min (measured: deep 0-2% live vs 0.60 CV recall; resetting only
    # minutes_since_onset moved max p_deep 0.008 -> 0.558), while the heuristic scored deep 17.7%
    # on the same night, inside the 15-20% literature range. Set False to trust the model alone.
    deep_corroboration: bool = True

    # Absolute-anchor wake test (see state_estimator._absolute_wake). Every other wake test is
    # relative to a TRAILING baseline, which a sustained HR elevation defeats -- the baseline just
    # rises with it. Against a labelled weightlifting session (band worn, HR to 168) the estimator
    # called it sleep 70.6% of the time, 17.6% of that DEEP, versus 17.7% deep during real sleep.
    # `resting + 25` separated them (67.1% of lifting caught, 2.3% of real sleep mislabelled).
    # OFF until the resting baseline is actually learned (it is None today) and the delta is
    # validated over several nights -- this drives the state machine and all thermal steering.
    est_stage_absolute_wake_enabled: bool = False
    est_stage_absolute_wake_delta_bpm: float = 25.0

    # Accelerometer wake evidence (see state_estimator._actigraphy_wake). Scored against HARD
    # labels -- the timestamps of messages typed during the night. Of 6 message-proven awakenings
    # in the 2026-08-04 sleep period the HR stager caught 2, labelling three of the misses REM
    # while the user was awake and typing. A single-minute PIM >= 5 test caught 6/6 while
    # flagging 9.8% of the period (38 min WASO vs the stager's 21.5). Requires wearable "counts"
    # units; the phone's 0..1 index is a ~17x different scale.
    #
    # ON. The 6 labels are positives only, so the 9.8%-of-night flagged rate is
    # plausible-WASO reasoning rather than a measured false-positive rate -- but leaving it off
    # is not the neutral choice it looks like. The in-night steerer consumes REM/deep ACCRUAL,
    # and the stager was crediting REM for time the user was demonstrably awake and typing; with
    # steering in its 'act' arm that mislabelled accrual actively drives thermal maneuvers. A
    # 6/6-vs-2/6 effect with a clear mechanism beats preserving a known-wrong baseline.
    # Re-evaluate against the first night's flagged rate: if WASO comes back implausibly high,
    # raise est_stage_actigraphy_wake_pim rather than disabling outright.
    est_stage_actigraphy_wake_enabled: bool = True
    est_stage_actigraphy_wake_pim: float = 5.0
    est_stage_actigraphy_wake_window_s: float = 60.0
    # HARD clamp of the commanded target to the personal comfort band (from the comfort sweep /
    # repo.get_comfort_profile), applied only in MAINTENANCE/WAKE_RECOVERY. The guardrail only
    # WARNS about an out-of-band target and the sole hard clamp is the device's 55-110 F range,
    # which is meaningless when a real usable range spans ~2 F. Without sensed bed temperature
    # (paywalled) the thermal loop is open-loop, and one measured night drifted to the too-warm
    # edge for hours and then overshot to the too-cold edge -- awakenings at both ends. This is
    # the backstop for that. INDUCTION (deliberate cold opener) and WAKE_WINDOW (deliberate warm
    # ramp) are exempt by design.
    # Use OUTDOOR weather as the exposed-skin ambient in the composite inversion when the
    # bedroom sensor is unavailable. OFF: the inversion divides the ambient term by
    # composite_bed_weight, so it AMPLIFIES -- a measured night's 62.3->84.5 F forecast swing
    # moved commanded water ~7.4 F (~32 device levels) at a near-constant effective target, and
    # the sleeper woke at both the warm and cold ends. With ambient None the inversion returns
    # the effective target unchanged, which is the honest behaviour for an unknown term. Outdoor
    # weather still drives the separate, CAPPED feed-forward bias (precomp_max_bias_f).
    ambient_outdoor_fallback: bool = False
    comfort_clamp_enabled: bool = True
    comfort_clamp_margin_f: float = 0.5   # allow this much beyond the measured band edges
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
    # Reconciliation with the WAKE-UP trajectory: stop deepening this many minutes (on top of the
    # smart-wake window) before the deadline, so the steerer hands the bed cleanly to the wake-up
    # ramp and never drives you into deep sleep right before you need to surface (deep-sleep wake =
    # grogginess/inertia — Brooks & Lack 2006). Standoff = wake_window_min + this guard (~a cycle).
    steer_prewake_guard_min: float = 45.0
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
    # --- Arousal / awakening detection thresholds (see controller/arousal.py) -----------
    # Graded micro-arousal/awakening detector: HR surge, HRV drop, movement, and how many
    # consecutive elevated samples count as sustained (vs. a transient blip).
    arousal_hr_surge_bpm: float = 6.0     # HR above sleep baseline that counts as a surge
    arousal_hrv_drop_frac: float = 0.15   # fractional HRV drop vs baseline that counts as a drop
    arousal_movement: float = 0.4         # movement at/above this counts as arousal-level motion
    arousal_persistence_samples: int = 3  # consecutive elevated samples => sustained (not a blip)
    # --- Wake-risk pre-emption thresholds (see controller/wake_risk.py) ------------------
    # Multi-signal vote (shared with the arousal detector's WakeDetector) + the precursor score
    # that drives proactive settle-cool pre-emption.
    # 2, not 3. Validated against 10 movement-measured awakenings on a real night, with
    # RSA-derived respiration restoring the resp_variability signal that could never fire before:
    #   min_signals=3 -> recall  5/10, false-positive fraction 0.00
    #   min_signals=2 -> recall 10/10, false-positive fraction 0.16   <- chosen
    # Two of the seven wake signals remain structurally dead here (confidence_drop -- stage
    # confidence is pinned at est_model_conf_cap; stage_regression -- the HR stager rarely emits
    # deep/REM to regress FROM), so a 3-signal bar demanded nearly every LIVE signal at once.
    # Recall is the right thing to buy: this project treats awakenings as the #1 error signal, and
    # a missed one is silent -- it teaches the precursor/lead-time learners nothing and skips
    # pre-emption entirely -- whereas a false one only mis-scores a night.
    wake_min_signals: int = 2                # signals required for the multi-signal wake vote
    wake_risk_hr_creep_bpm: float = 4.0      # HR above baseline that counts as "creeping up"
    wake_risk_movement: float = 0.3          # movement at/above this counts as restlessness
    wake_risk_warm_margin_f: float = 1.5     # bed this far above target counts as "running warm"
    wake_risk_preempt_threshold: float = 0.5  # combined risk score that triggers a pre-empt
    # --- Data-quality gate (do-no-harm on untrustworthy frames) --------------------------
    # Below this score, confidence is down-weighted and the decision is biased toward HOLD
    # (see ``sleepctl.controller.data_quality`` + ``SleepController.decide``).
    data_quality_hold_score: float = 0.5     # below this: force a conservative HOLD
    data_quality_downweight_score: float = 0.8  # below this: scale confidence by the score
    # --- Decision guardrail (trajectory-level invariant monitor) -------------------------
    # Backstop over the recent decision/frame TRAJECTORY, not any single sub-module. See
    # ``sleepctl.controller.guardrail``. Findings are {code, severity}; CRITICAL forces HOLD.
    guardrail_window_min: float = 20.0        # recent-history window the guardrail inspects
    # (a) aggressive cooling while HR is rising above the sleep baseline (possible arousal-drive).
    # This is a BACKSTOP, so its bar is deliberately set above the primary ArousalDetector's own
    # hr_surge threshold (6.0 bpm, see arousal.py) -- it should only fire on a clearer, more
    # sustained signal than the routine arousal path already handles, to stay rare/conservative.
    guardrail_hr_rise_bpm: float = 8.0        # HR this far above sleep baseline = "rising"
    guardrail_cool_run_count: int = 5         # this many consecutive COOLER actions = "sustained"
    # (b) target outside the personal comfort band (from repo.get_comfort_profile)
    guardrail_comfort_margin_f: float = 2.0   # allowed slack beyond the learned edges
    # (c) thermal oscillation: rapid target reversals over a short window. Deliberately set
    # ABOVE what normal maintenance settle-cool/deep-bias/REM-neutral cycling produces (verified
    # against the "normal" full-night scenario) so this only catches genuine hunting/flapping.
    guardrail_oscillation_window_min: float = 30.0
    guardrail_oscillation_reversals: int = 5  # this many direction reversals in the window = flap
    guardrail_oscillation_min_delta_f: float = 1.5  # ignore reversals smaller than this
    # (d) sustained commanded-vs-device divergence (reuses thermal_health if available)
    guardrail_stall_ticks: int = 3            # consecutive "stalled" ThermalHealth reads to flag
    # --- Calendar-driven shift planning ---------------------------------------------------
    # For a DAY (or evening) shift picked up from the work calendar, how long before the shift
    # start to set the auto-wake alarm — time to get up, ready, and out the door. Night shifts
    # don't get a morning alarm at all (the daytime-sleep/banking plan handles those instead).
    shift_prep_buffer_min: int = 90
    # --- 3AM WAKE targeted analysis: personal recurring-wake-window pre-emption -----------
    # Gated, OPTIONAL pre-emption keyed off ``sleepctl.analysis.wake_patterns.
    # wake_analysis_report``'s recurring-window findings (clock-time bins where THIS user
    # disproportionately wakes, learned from their own logged history). Conservative by
    # design: it only ever ADDS one more vote to the existing settle-cool pre-emption union
    # (wake-risk OR precursor-drift OR micro-arousal) in ``SleepController.decide`` -- never
    # replaces those signals -- and it stays completely silent (log-only, no thermal effect)
    # until BOTH the minimum-nights and confidence gates clear. Whatever nudge it does apply
    # still runs through the same slew-limit / variability-cap / 55-110°F clamp as every other
    # thermal move (see ``ThermalController``), plus its own small dedicated cap below.
    wake_window_preempt_enabled: bool = True
    wake_window_preempt_min_nights: int = 10          # nights of monitored data required in-window
    wake_window_preempt_confidence_min: float = 0.55  # cluster confidence required to act
    wake_window_preempt_lead_min: float = 20.0        # start smoothing this long before the window
    wake_window_preempt_max_f: float = 0.5            # hard cap on the extra pre-emptive nudge


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
class EfficacyTrialConfig:
    """Gates for the randomized efficacy MICRO-trials (sleepctl.ml.efficacy_trial): on a capped
    fraction of ELIGIBLE (normal, full-length) nights, randomize active-vs-sham control to
    measure the controller's true causal effect. On by default but deliberately conservative --
    a low sham fraction, a real minimum-nights bar before a verdict is reported, and an
    auto-stop guardrail that kills sham assignment the moment it's trending worse."""

    enabled: bool = True             # on by default -- conservative caps below keep it safe
    sham_fraction: float = 0.2       # target share of ELIGIBLE nights run as 'sham'; hard-capped
                                     # at sleepctl.ml.efficacy_trial.MAX_SHAM_FRACTION (0.25)
    min_nights_before_verdict: int = 10  # per-arm nights required before analyze_trials asserts
                                         # a significant effect either way
    auto_stop_min_n: int = 6         # per-arm nights required before the auto-stop guardrail
                                     # is even allowed to evaluate (never acts on a hunch)
    auto_stop_threshold: float = 1.0  # sham mean wake_events must exceed active mean by at
                                      # least this many events/night to trip the guardrail


@dataclass
class ThermalTrialConfig:
    """Gates for the n-of-1 THERMAL DOSE-RESPONSE trial (sleepctl.ml.thermal_trial): on a
    capped, block-balanced fraction of ELIGIBLE (normal, full-length) nights, randomize the
    MAINTENANCE-phase neutral setpoint across ``offset_ladder_f`` -- a small ladder of °F
    offsets around the learned neutral -- to find which offset minimizes THIS user's
    wake_events (the #1 complaint: staying asleep). Deliberately includes mild WARMING
    arms (+0.4, +0.8) alongside cooling ones: Raymann et al. 2008 (Brain,
    DOI 10.1093/brain/awm315) found a +0.4 C skin-temperature rise SUPPRESSED nocturnal
    wakefulness, the opposite of this controller's default cool bias -- we don't know this
    user's personal dose-response, so the trial has to be able to test both directions.

    OFF by default: unlike the efficacy micro-trial (which only toggles active/sham CONTROL,
    never the temperature itself), this changes what temperature the bed actually runs at
    overnight, so it must be explicitly opted into.
    """

    enabled: bool = False              # OFF by default -- changes what the bed does at night
    # Maintenance-offset ladder (°F, relative to the learned neutral_f). 0.0 (or
    # ``control_offset_f``) is the current policy / control arm. Offsets are clamped to
    # +/-``comfort_band_f`` before use -- see sleepctl.ml.thermal_trial._clamped_ladder.
    offset_ladder_f: list = field(default_factory=lambda: [-1.5, -0.75, 0.0, 0.4, 0.8])
    control_offset_f: float = 0.0      # the "do nothing different" arm -- today's real policy
    comfort_band_f: float = 2.0        # hard comfort clamp on any offset, regardless of ladder
    # Target share of ELIGIBLE nights that run a NON-control offset (the rest run control).
    # Hard-capped at sleepctl.ml.thermal_trial.MAX_EXPERIMENTAL_FRACTION (0.6) -- a dose-response
    # ladder has several experimental arms (unlike the single-arm efficacy sham), so a larger
    # cap is defensible, but the trial must still not dominate the schedule.
    experimental_fraction: float = 0.5
    min_nights_before_verdict: int = 8  # per-arm nights required before analyze_dose_response
                                        # will name a winner (never trusts a 3-night difference)
    auto_stop_min_n: int = 6           # per-arm nights required before auto-stop may evaluate
    auto_stop_threshold: float = 1.0   # an arm's mean wake_events must exceed control's by at
                                       # least this many events/night to be auto-suspended


@dataclass
class AppConfig:
    profile: UserProfile = field(default_factory=UserProfile)
    benchmarks: Benchmarks = field(default_factory=Benchmarks)
    tunables: Tunables = field(default_factory=Tunables)
    ml: MLConfig = field(default_factory=MLConfig)
    efficacy_trial: EfficacyTrialConfig = field(default_factory=EfficacyTrialConfig)
    thermal_trial: ThermalTrialConfig = field(default_factory=ThermalTrialConfig)

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
            onset_cold_settle_f=t.onset_cold_settle_temp_f,
            version=0,
            source="default",
        )

    @classmethod
    def from_yaml(cls, path) -> "AppConfig":
        """Load overrides from a YAML file; missing file -> defaults.

        YAML may contain top-level keys ``profile``, ``benchmarks``, ``tunables``,
        ``ml``, ``efficacy_trial``, ``thermal_trial``, each a mapping of field -> value.
        Unknown keys are ignored.
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
            ("efficacy_trial", cfg.efficacy_trial),
            ("thermal_trial", cfg.thermal_trial),
        ):
            overrides = data.get(section_name) or {}
            if not is_dataclass(section_obj):
                continue
            valid = {f.name for f in fields(section_obj)}
            for key, value in overrides.items():
                if key in valid:
                    setattr(section_obj, key, value)
        return cfg
