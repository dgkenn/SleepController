"""A stage-less external HR feed (Polar Verity Sense) must be able to STEER the controller.

Before this, a feed with no Pod sleep stage arrived as ``stage=UNKNOWN`` every tick, and onset
detection + the state machine hard-require a real sleep stage — so the controller got stuck in
INDUCTION and none of the maintenance-time steering ever ran. The vitals-based stage estimate
(sleepctl/controller/state_estimator.py), overlaid onto the frame inside ``decide``, closes that
gap: HR/HRV/movement now drive onset → MAINTENANCE → arousal / wake-risk / steering.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.controller.state_estimator import estimate_stage_from_vitals
from sleepctl.models import ContextRecord, ControllerState, SensorFrame, SleepStage


# --------------------------------------------------------------------- estimator unit tests
def _f(hr=56.0, move=0.03):
    return SensorFrame(timestamp=datetime(2026, 6, 23, 1, 0), stage=SleepStage.UNKNOWN,
                       heart_rate=hr, movement=move)


def test_estimator_none_without_hr():
    assert estimate_stage_from_vitals(_f(hr=None), 55.0, []) is None


def test_estimator_awake_on_movement():
    stage, conf = estimate_stage_from_vitals(_f(move=0.4), 55.0, [])
    assert stage is SleepStage.AWAKE and 0 < conf <= 0.5


def test_estimator_awake_on_high_hr():
    stage, _ = estimate_stage_from_vitals(_f(hr=70.0, move=0.02), 55.0, [])
    assert stage is SleepStage.AWAKE


def test_estimator_deep_on_sustained_low_hr_stillness():
    still = [_f(hr=51.0, move=0.01) for _ in range(4)]
    stage, _ = estimate_stage_from_vitals(_f(hr=51.0, move=0.01), 55.0, still)
    assert stage is SleepStage.DEEP


def test_estimator_light_default_when_asleep_but_not_deep():
    # still + HR near baseline, but no sustained-quiescence history -> LIGHT, not DEEP
    stage, _ = estimate_stage_from_vitals(_f(hr=55.0, move=0.03), 55.0, [])
    assert stage is SleepStage.LIGHT


def _onset_hr(i: int) -> float:
    """A physiological fall-asleep heart-rate profile: settle awake-in-bed, decline through onset,
    then steady sleep HR -- all with natural beat-to-beat jitter."""
    import math
    if i < 5:
        base = 66.0
    elif i < 15:
        base = 66.0 - 1.2 * (i - 5)
    else:
        base = 54.0
    return base + 0.9 * math.sin(i * 1.3) + 0.5 * math.cos(i * 2.7)


# --------------------------------------------------------------------- integration: it steers
def test_verity_only_feed_reaches_maintenance_and_runs_steering():
    cfg = AppConfig.default()
    c = SleepController(cfg)
    ctx = ContextRecord(date="2026-06-23")
    recent: list = []
    start = datetime(2026, 6, 23, 23, 0)

    reached = False
    for i in range(60):
        now = start + timedelta(minutes=i)
        # Polar-only: NO Pod stage (UNKNOWN), still, regular breathing, and a PHYSIOLOGICAL
        # fall-asleep HR profile -- a few minutes settling awake-in-bed, then the normal onset
        # decline, then steady sleep HR with natural beat-to-beat jitter. (A dead-flat HR is
        # out-of-distribution: zero variability never occurs in a real recording, and the stager
        # reads HR variability + a downward trend as core onset evidence.)
        frame = SensorFrame(timestamp=now, stage=SleepStage.UNKNOWN, presence=True,
                            heart_rate=_onset_hr(i), hrv=62.0, respiratory_rate=14.0,
                            movement=0.03, bed_temp_f=72.0, room_temp_f=68.0,
                            data_age_seconds=20)
        d = c.decide(frame, ctx, recent, now)
        recent.append(frame)
        # the stage the whole pipeline saw was DERIVED from vitals (learned model or heuristic),
        # not a Pod label
        assert d.log_payload["stage_source"] in ("model", "heuristic", "model+deep")
        assert d.log_payload["stage"] in ("awake", "light", "deep", "rem")
        if c.sm.state is ControllerState.MAINTENANCE:
            reached = True
            break

    assert reached, f"stage-less feed never reached MAINTENANCE (stuck in {c.sm.state})"
    assert c._sleep_onset_time is not None

    # Now prove maintenance-time steering ACTUALLY RUNS off the wearable: the arousal detector
    # must execute on a maintenance tick (it never ran while the feed was stuck in INDUCTION). A
    # still frame with a modest HR surge keeps the BCG reliable (no data-quality hold) so the
    # detector runs and grades it.
    now = start + timedelta(minutes=30)
    surge = SensorFrame(timestamp=now, stage=SleepStage.UNKNOWN, presence=True,
                        heart_rate=66.0, hrv=48.0, respiratory_rate=15.0, movement=0.05,
                        bed_temp_f=72.0, room_temp_f=68.0, data_age_seconds=20)
    c.decide(surge, ctx, recent, now)
    assert c.last_arousal is not None, "arousal detector did not run in MAINTENANCE"


def test_learned_model_drives_when_weights_are_bundled():
    """With the trained sleep_staging weights present, the learned model (not the heuristic)
    supplies the stage for a stage-less HR feed."""
    from sleepctl.ml.sleep_staging.infer import SleepStager
    if not SleepStager.load().available:
        import pytest
        pytest.skip("sleep_staging weights not bundled")

    cfg = AppConfig.default()
    c = SleepController(cfg)
    ctx = ContextRecord(date="2026-06-23")
    recent: list = []
    start = datetime(2026, 6, 23, 23, 0)
    saw_model = False
    for i in range(12):
        now = start + timedelta(minutes=i)
        frame = SensorFrame(timestamp=now, stage=SleepStage.UNKNOWN, presence=True,
                            heart_rate=55.0, hrv=60.0, respiratory_rate=14.0, movement=0.03,
                            bed_temp_f=72.0, room_temp_f=68.0, data_age_seconds=20)
        d = c.decide(frame, ctx, recent, now)
        recent.append(frame)
        if d.log_payload["stage_source"] == "model":
            saw_model = True
    assert saw_model, "learned stager was available but never supplied the stage"


def test_real_pod_stage_is_never_overridden():
    """When the Pod DOES supply a stage, the estimate must not touch it."""
    cfg = AppConfig.default()
    c = SleepController(cfg)
    ctx = ContextRecord(date="2026-06-23")
    now = datetime(2026, 6, 23, 23, 0)
    frame = SensorFrame(timestamp=now, stage=SleepStage.DEEP, presence=True, heart_rate=80.0,
                        movement=0.5, bed_temp_f=72.0, room_temp_f=68.0, data_age_seconds=20)
    d = c.decide(frame, ctx, [], now)
    assert d.log_payload["stage"] == "deep"           # real Pod stage preserved
    assert d.log_payload["stage_source"] == "sensor"  # not estimated


# ------------------------------------------------- out-of-bed guard: no staging while IDLE
def test_no_stage_estimate_while_idle_and_presence_unknown():
    """A still band with the user OUT of bed must not be staged as sleep.

    ``presence`` reads None for long stretches on this Pod, and the overlay's only gate used to
    be ``presence is not False``. So after the user got up, a band left sitting still with a flat
    HR kept scoring as "sustained quiescence below baseline" -- i.e. DEEP. On 2026-08-04 that put
    281 of 296 DEEP samples between 07:00 and 11:58 the morning AFTER the night, versus 15 during
    the night itself, corrupting every night_date-keyed rollup and learner downstream.
    """
    cfg = AppConfig.default()
    c = SleepController(cfg)
    ctx = ContextRecord(date="2026-06-23")
    recent: list = []
    start = datetime(2026, 6, 23, 9, 0)  # mid-MORNING, user is up and about

    for i in range(20):
        now = start + timedelta(minutes=i)
        frame = SensorFrame(timestamp=now, stage=SleepStage.UNKNOWN, presence=None,
                            heart_rate=52.0, hrv=62.0, movement=0.0,
                            bed_temp_f=72.0, room_temp_f=68.0, data_age_seconds=20)
        d = c.decide(frame, ctx, recent, now)
        recent.append(frame)
        assert c.sm.state is ControllerState.IDLE
        assert d.log_payload["stage"] == "unknown", (
            f"tick {i}: staged {d.log_payload['stage']} with the user out of bed")
        assert d.log_payload["stage_source"] == "sensor"


def test_stage_estimate_still_runs_on_the_bed_entry_tick():
    """The guard must not cost the first in-bed tick: the overlay runs BEFORE the state machine
    steps, so the machine is still IDLE when the user has just got into bed. Positive presence
    is what distinguishes that from the out-of-bed case above."""
    cfg = AppConfig.default()
    c = SleepController(cfg)
    ctx = ContextRecord(date="2026-06-23")
    now = datetime(2026, 6, 23, 23, 0)
    frame = SensorFrame(timestamp=now, stage=SleepStage.UNKNOWN, presence=True,
                        heart_rate=58.0, hrv=62.0, movement=0.03,
                        bed_temp_f=72.0, room_temp_f=68.0, data_age_seconds=20)
    d = c.decide(frame, ctx, [], now)
    assert d.log_payload["stage"] != "unknown"
    assert d.log_payload["stage_source"] in ("model", "heuristic", "model+deep")


# ------------------------------------- absolute-anchor wake test (default OFF, see config)
def test_absolute_wake_is_off_by_default():
    """It changes wake sensitivity -- which drives the state machine, arousal detection and all
    thermal steering -- and is calibrated on a single session. It must stay opt-in."""
    from sleepctl.controller.state_estimator import estimate_sleep_stage

    cfg = AppConfig.default()
    assert cfg.tunables.est_stage_absolute_wake_enabled is False

    f = SensorFrame(timestamp=datetime(2026, 6, 23, 1, 0), stage=SleepStage.UNKNOWN,
                    heart_rate=110.0, hrv=40.0, movement=0.01)
    est = estimate_sleep_stage(f, 60.0, [], cfg, resting_hr=60.0)
    assert est is not None and est[2] != "absolute_wake"


def test_absolute_wake_catches_sustained_elevation_the_trailing_baseline_misses():
    """The weightlifting failure: HR far above MEASURED resting, but the trailing baseline has
    risen with it so every relative test reads 'at baseline' and the frame scores as sleep."""
    from sleepctl.controller.state_estimator import estimate_sleep_stage

    cfg = AppConfig.default()
    cfg.tunables.est_stage_absolute_wake_enabled = True

    # trailing baseline == current HR (what a sustained elevation produces), resting is far below
    f = SensorFrame(timestamp=datetime(2026, 6, 23, 1, 0), stage=SleepStage.UNKNOWN,
                    heart_rate=89.0, hrv=40.0, movement=0.01)
    stage, conf, source = estimate_sleep_stage(f, 89.0, [], cfg, resting_hr=61.0)
    assert stage is SleepStage.AWAKE and source == "absolute_wake"


def test_absolute_wake_leaves_real_sleep_alone():
    """A normal sleeping HR near the resting anchor must not be dragged awake."""
    from sleepctl.controller.state_estimator import estimate_sleep_stage

    cfg = AppConfig.default()
    cfg.tunables.est_stage_absolute_wake_enabled = True

    f = SensorFrame(timestamp=datetime(2026, 6, 23, 1, 0), stage=SleepStage.UNKNOWN,
                    heart_rate=73.0, hrv=40.0, movement=0.01)   # night median
    est = estimate_sleep_stage(f, 73.0, [], cfg, resting_hr=61.0)
    assert est is not None and est[2] != "absolute_wake"


def test_absolute_wake_needs_a_measured_resting_anchor():
    """resting_baseline is None on this deployment, so the test must simply not fire."""
    from sleepctl.controller.state_estimator import estimate_sleep_stage

    cfg = AppConfig.default()
    cfg.tunables.est_stage_absolute_wake_enabled = True

    f = SensorFrame(timestamp=datetime(2026, 6, 23, 1, 0), stage=SleepStage.UNKNOWN,
                    heart_rate=150.0, hrv=40.0, movement=0.01)
    est = estimate_sleep_stage(f, 60.0, [], cfg, resting_hr=None)
    assert est is not None and est[2] != "absolute_wake"


# ----------------------------------- accelerometer wake evidence (default OFF, see config)
def _acc_frame(counts, units="counts", hr=62.0):
    f = SensorFrame(timestamp=datetime(2026, 6, 23, 2, 30), stage=SleepStage.UNKNOWN,
                    heart_rate=hr, hrv=45.0, movement=0.02)
    f.activity_history = [(1000.0 + i * 10.0, c) for i, c in enumerate(counts)]
    f.activity_units = units
    return f


def test_actigraphy_wake_is_on_by_default():
    """Enabled deliberately: the stager credited REM for time the user was awake and typing, and
    the in-night steerer consumes that accrual, so leaving it off preserves a known-wrong
    baseline rather than being neutral."""
    from sleepctl.controller.state_estimator import estimate_sleep_stage

    cfg = AppConfig.default()
    assert cfg.tunables.est_stage_actigraphy_wake_enabled is True
    stage, _, source = estimate_sleep_stage(_acc_frame([0.1, 0.2, 40.0]), 60.0, [], cfg)
    assert stage is SleepStage.AWAKE and source == "actigraphy_wake"


def test_actigraphy_wake_can_be_disabled():
    from sleepctl.controller.state_estimator import estimate_sleep_stage

    cfg = AppConfig.default()
    cfg.tunables.est_stage_actigraphy_wake_enabled = False
    est = estimate_sleep_stage(_acc_frame([0.1, 0.2, 40.0]), 60.0, [], cfg)
    assert est is not None and est[2] != "actigraphy_wake"


def test_actigraphy_wake_fires_on_a_single_minute_of_motion():
    """The measured failure mode: brief, low-energy movement from typing on a phone. Requiring
    sustained motion drops sensitivity from 6/6 to 3/6 against message-timestamp ground truth."""
    from sleepctl.controller.state_estimator import estimate_sleep_stage

    cfg = AppConfig.default()
    cfg.tunables.est_stage_actigraphy_wake_enabled = True
    stage, _, source = estimate_sleep_stage(_acc_frame([0.1, 0.2, 0.3, 25.6]), 60.0, [], cfg)
    assert stage is SleepStage.AWAKE and source == "actigraphy_wake"


def test_actigraphy_wake_ignores_quiet_sleep():
    from sleepctl.controller.state_estimator import estimate_sleep_stage

    cfg = AppConfig.default()
    cfg.tunables.est_stage_actigraphy_wake_enabled = True
    est = estimate_sleep_stage(_acc_frame([0.4, 0.6, 0.5, 0.42]), 60.0, [], cfg)
    assert est is not None and est[2] != "actigraphy_wake"


def test_actigraphy_wake_refuses_phone_index_units():
    """The iPhone's 0..1 index is a ~17x different scale (PIM/16.7 measured on a real night), so a
    PIM threshold applied to it would fire on essentially nothing -- or, if inverted, everything.
    Without explicit 'counts' the test must not run at all."""
    from sleepctl.controller.state_estimator import estimate_sleep_stage

    cfg = AppConfig.default()
    cfg.tunables.est_stage_actigraphy_wake_enabled = True
    est = estimate_sleep_stage(_acc_frame([0.1, 40.0], units="phone_index"), 60.0, [], cfg)
    assert est is not None and est[2] != "actigraphy_wake"

    est2 = estimate_sleep_stage(_acc_frame([0.1, 40.0], units=None), 60.0, [], cfg)
    assert est2 is not None and est2[2] != "actigraphy_wake"


# ------------------------------------------------- wake ledger: unblocking trajectory learning
def test_accelerometer_wake_reaches_the_wake_ledger():
    """``raw_samples.wake_event`` is the ONLY input to ``awakening_precursor_profile``
    (``wake_times = [... if wake_event == 1]``), and it was written 0 on every row of a night
    containing 6 message-proven awakenings -- so the personalized precursor trajectory has never
    had a single observation to learn from.

    The arousal voter already counts an AWAKE stage among its signals; it stayed silent because
    the HR stager almost never emitted AWAKE during maintenance (it emitted REM at exactly those
    moments). Once accelerometer counts can drive the stage to AWAKE, the existing voter path
    fires and the ledger fills -- no separate fallback needed.
    """
    cfg = AppConfig.default()
    cfg.tunables.est_stage_actigraphy_wake_enabled = True
    c = SleepController(cfg)
    ctx = ContextRecord(date="2026-06-23")
    recent: list = []
    start = datetime(2026, 6, 23, 23, 0)

    def frame(i, pim):
        f = SensorFrame(timestamp=start + timedelta(minutes=i), stage=SleepStage.UNKNOWN,
                        presence=True, heart_rate=_onset_hr(i), hrv=62.0, respiratory_rate=14.0,
                        movement=0.03, bed_temp_f=72.0, room_temp_f=68.0, data_age_seconds=20)
        f.activity_history = [(1000.0 + i * 60 + k * 10.0, pim) for k in range(6)]
        f.activity_units = "counts"
        return f

    i = 0
    while c.sm.state is not ControllerState.MAINTENANCE and i < 90:
        f = frame(i, 0.4)                       # quiet: well under the PIM gate
        c.decide(f, ctx, recent, start + timedelta(minutes=i))
        recent.append(f)
        i += 1
    assert c.sm.state is ControllerState.MAINTENANCE, "never reached maintenance"

    f = frame(i, 25.6)                          # a real burst of wearable counts
    d = c.decide(f, ctx, recent, start + timedelta(minutes=i))

    assert d.log_payload["stage"] == "awake"
    assert d.log_payload["stage_source"] == "actigraphy_wake"
    assert c.last_wake_event is not None, "accelerometer wake left the learner's ledger empty"
