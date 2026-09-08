"""The controller records what the wearable actually fed it, per tick."""

from datetime import datetime

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.models import ContextRecord, SensorFrame, SleepStage


def _tick(frame):
    c = SleepController(AppConfig())
    ctx = ContextRecord(date="2026-09-08")
    return c.decide(frame, ctx, [], datetime(2026, 9, 8, 2, 0))


def test_dense_inputs_are_recorded():
    f = SensorFrame(timestamp=datetime(2026, 9, 8, 2, 0), stage=SleepStage.UNKNOWN,
                    heart_rate=62.0, movement=0.02, data_age_seconds=5)
    f.hr_history = [(1.0, 62.0)] * 40
    f.activity_history = [(1.0, 2.0)] * 40
    f.activity_units = "counts"
    d = _tick(f)
    wi = d.log_payload["wearable_inputs"]
    assert wi == {"hr_history_n": 40, "activity_history_n": 40, "activity_units": "counts"}


def test_absent_inputs_read_as_zero_not_missing():
    f = SensorFrame(timestamp=datetime(2026, 9, 8, 2, 0), stage=SleepStage.UNKNOWN,
                    heart_rate=62.0, data_age_seconds=5)
    d = _tick(f)
    assert d.log_payload["wearable_inputs"] == {
        "hr_history_n": 0, "activity_history_n": 0, "activity_units": None}
