"""The learned stager may only be fed motion in the units it was TRAINED on.

The bundled `hrmotion` weights are the scale-sensitive variant, trained on actigraphy counts.
The iPhone's movement index is 0..1 -- a ~17x different scale -- so feeding it would put the
model out of distribution on precisely the feature the variant exists to use. The armband's PMD
accelerometer supplies counts, which is what makes enabling motion safe.
"""
from datetime import datetime

from sleepctl.config import AppConfig
from sleepctl.controller.state_estimator import _activity_series
from sleepctl.models import SensorFrame, SleepStage


def _frame(units, hist=True):
    f = SensorFrame(timestamp=datetime(2026, 8, 28, 23, 0), stage=SleepStage.UNKNOWN,
                    heart_rate=62.0, movement=0.03, presence=True)
    if hist:
        f.activity_history = [(1000.0 + i * 30.0, 7.5) for i in range(8)]
    f.activity_units = units
    return f


def test_armband_counts_are_passed_through():
    out = _activity_series([], _frame("counts"))
    assert out and len(out) == 8


def test_the_phone_index_is_refused():
    """Silently falls back to HR-only rather than feeding a 17x-off scale to the model."""
    assert _activity_series([], _frame("phone_index")) is None


def test_unknown_units_are_refused():
    assert _activity_series([], _frame(None)) is None


def test_motion_is_enabled_now_that_counts_are_available():
    assert AppConfig.default().tunables.stager_use_motion is True


def test_the_per_frame_movement_fallback_is_never_used():
    """`activity_units` describes the HISTORY series. `frame.movement` is the fused 0..1 index,
    so falling back to it would smuggle in the very scale error the units check prevents --
    while still carrying a "counts" label."""
    f = _frame("counts", hist=False)
    assert f.movement is not None, "the frame does carry a movement value to be tempted by"
    assert _activity_series([], f) is None


def test_a_recent_buffer_of_frames_is_not_used_as_a_counts_series_either():
    f = _frame("counts", hist=False)
    assert _activity_series([_frame("counts", hist=False)], f) is None
