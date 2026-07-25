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
        assert d.log_payload["stage_source"] in ("model", "heuristic")
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
