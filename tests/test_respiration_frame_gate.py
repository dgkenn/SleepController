"""A contested breathing rate never reaches the frame; a confident one does, with its source."""
from datetime import datetime

from sleepctl.adapters.wearable import WearableSample, fuse_sample
from sleepctl.models import SensorFrame, SleepStage


def _frame():
    return SensorFrame(timestamp=datetime(2026, 9, 8, 23, 0), stage=SleepStage.UNKNOWN, presence=None)


def test_a_confident_rate_reaches_the_frame_with_its_source():
    f = _frame()
    fuse_sample(f, WearableSample(timestamp=datetime.now(), heart_rate=60.0, respiratory_rate=13.4,
                                  respiratory_rate_conf=0.9, respiratory_rate_source="rsa+acc", age_seconds=1.0))
    assert f.respiratory_rate == 13.4 and f.respiratory_rate_source == "rsa+acc"


def test_a_contested_rate_is_withheld_but_logged():
    f = _frame()
    fuse_sample(f, WearableSample(timestamp=datetime.now(), heart_rate=60.0, respiratory_rate=20.0,
                                  respiratory_rate_conf=0.4, respiratory_rate_source="acc(disagree)", age_seconds=1.0))
    assert f.respiratory_rate is None and f.respiratory_rate_conf == 0.4


def test_a_rate_without_a_confidence_is_accepted_for_compatibility():
    f = _frame()
    fuse_sample(f, WearableSample(timestamp=datetime.now(), heart_rate=60.0, respiratory_rate=14.0, age_seconds=1.0))
    assert f.respiratory_rate == 14.0
