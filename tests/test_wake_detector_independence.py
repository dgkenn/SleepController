"""Three of the wake voter's six signals come from the SAME stage estimator, so a min_signals=3
quorum could be met entirely by one noisy stage label with no physiological corroboration:
the label moving to AWAKE out of DEEP/REM sets both `stage_regression` and `awake_stage`, and
the confidence wobble that accompanies it sets `confidence_drop`.

Measured on 2026-08-27: 25 of 51 wake ticks had neither elevated heart rate nor movement.
Three votes from one estimator is one vote.
"""
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.wake_detection import WakeDetector
from sleepctl.models import SensorFrame, SleepStage

T0 = datetime(2026, 8, 27, 23, 0)


def _f(i, stage=SleepStage.LIGHT, hr=66.0, move=0.02, rr=14.0, conf=0.6):
    return SensorFrame(timestamp=T0 + timedelta(minutes=i), stage=stage, stage_confidence=conf,
                       heart_rate=hr, hrv=40.0, respiratory_rate=rr, movement=move, presence=True)


def _window(n=10, **kw):
    return [_f(i, **kw) for i in range(n)]


def test_the_stage_estimator_alone_cannot_declare_an_awakening():
    """AWAKE out of DEEP with a confidence drop is three signals from one estimator."""
    recent = _window(stage=SleepStage.DEEP, conf=0.8)
    frame = _f(11, stage=SleepStage.AWAKE, conf=0.3, hr=66.0, move=0.02)
    assert WakeDetector().evaluate(frame, recent) is None


def test_the_same_stage_evidence_plus_one_real_signal_does_fire():
    """The stager is allowed to make the case; it just cannot be the only witness."""
    recent = _window(stage=SleepStage.DEEP, conf=0.8)
    frame = _f(11, stage=SleepStage.AWAKE, conf=0.3, hr=80.0, move=0.02)   # + hr_rise
    assert WakeDetector().evaluate(frame, recent) is not None


def test_movement_corroboration_also_satisfies_independence():
    recent = _window(stage=SleepStage.DEEP, conf=0.8)
    frame = _f(11, stage=SleepStage.AWAKE, conf=0.3, hr=66.0, move=0.9)
    assert WakeDetector().evaluate(frame, recent) is not None


def test_wearable_actigraphy_counts_as_independent_evidence():
    """An actigraphy wake expresses itself by driving the stage to AWAKE. Without counting the
    counts themselves, the independence rule would suppress the single best wake signal we have
    (6/6 against ground truth, versus the HR stager's 2/6)."""
    cfg = AppConfig.default()
    cfg.tunables.est_stage_actigraphy_wake_enabled = True
    recent = _window(stage=SleepStage.DEEP, conf=0.8)
    frame = _f(11, stage=SleepStage.AWAKE, conf=0.3, hr=66.0, move=0.02)
    frame.activity_history = [(1000.0 + k * 10.0, 25.6) for k in range(6)]
    frame.activity_units = "counts"
    assert WakeDetector(cfg=cfg).evaluate(frame, recent) is not None


def test_phone_scale_activity_does_not_count_as_corroboration():
    """The phone's 0..1 index is a ~17x different scale; a PIM threshold on it is meaningless."""
    cfg = AppConfig.default()
    cfg.tunables.est_stage_actigraphy_wake_enabled = True
    recent = _window(stage=SleepStage.DEEP, conf=0.8)
    frame = _f(11, stage=SleepStage.AWAKE, conf=0.3, hr=66.0, move=0.02)
    frame.activity_history = [(1000.0 + k * 10.0, 25.6) for k in range(6)]
    frame.activity_units = "phone_index"
    assert WakeDetector(cfg=cfg).evaluate(frame, recent) is None


def test_the_independence_rule_can_be_disabled():
    recent = _window(stage=SleepStage.DEEP, conf=0.8)
    frame = _f(11, stage=SleepStage.AWAKE, conf=0.3, hr=66.0, move=0.02)
    assert WakeDetector(require_independent=False).evaluate(frame, recent) is not None


def test_an_independent_signal_alone_still_needs_the_full_quorum():
    """Independence is a necessary condition, not a substitute for the vote count."""
    recent = _window()
    frame = _f(11, hr=80.0)          # hr_rise only
    assert WakeDetector().evaluate(frame, recent) is None
