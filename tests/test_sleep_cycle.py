"""Ultradian sleep-cycle predictor: light detection, deep-bout learning, time-to-next-light."""

from datetime import datetime, timedelta

from sleepctl.controller.sleep_cycle import SleepCyclePredictor
from sleepctl.models import SleepStage


T0 = datetime(2026, 6, 29, 3, 0)


def test_light_stage_is_in_light_with_zero_wait():
    p = SleepCyclePredictor()
    p.observe(T0, SleepStage.LIGHT)
    s = p.predict(T0, SleepStage.LIGHT)
    assert s.in_light is True and s.minutes_to_next_light == 0.0


def test_deep_predicts_remaining_bout():
    p = SleepCyclePredictor()
    start = T0
    p.observe(start, SleepStage.DEEP)            # enter deep
    now = start + timedelta(minutes=10)
    s = p.predict(now, SleepStage.DEEP)
    assert s.in_light is False
    # default bout ~22 min, ~10 elapsed -> ~12 remaining
    assert 8 <= s.minutes_to_next_light <= 16
    assert s.minutes_in_stage >= 9


def test_learns_this_nights_deep_bout_length():
    p = SleepCyclePredictor()
    # a completed 12-min deep bout, then back to deep
    p.observe(T0, SleepStage.DEEP)
    p.observe(T0 + timedelta(minutes=12), SleepStage.LIGHT)     # closes a 12-min bout
    p.observe(T0 + timedelta(minutes=20), SleepStage.DEEP)
    s = p.predict(T0 + timedelta(minutes=20), SleepStage.DEEP)
    assert abs(s.typical_deep_bout_min - 12.0) <= 1.0          # learned, not the 22-min default
