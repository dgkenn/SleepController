"""A long gap between accruals is a different night.

The reset on entering IDLE is the intended path, but it lives in the intent block and several
guards return from the tick before reaching it -- the stale-data guard in particular, which fires
on every tick of a day with no wearable. So on 2026-09-04 the accumulator still carried
2026-09-01's totals across three idle days: deep read 57.9 min, which is exactly 30.3 from the
earlier night plus 27.6 from this one, and REM read 97.9 against a rollup that scored ZERO REM.
"""

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.controller import ARCHITECTURE_GAP_RESET_MIN, SleepController
from sleepctl.models import SleepStage


def _c():
    c = SleepController(AppConfig())
    c._arch_deep_min = 30.3
    c._arch_rem_min = 97.9
    c._arch_light_min = 242.9
    return c


def test_a_multi_day_gap_starts_a_new_night():
    c = _c()
    t = datetime(2026, 9, 1, 5, 0)
    c._arch_last_ts = t
    c._accrue_architecture(t + timedelta(days=3), SleepStage.DEEP)
    assert c._arch_rem_min == 0.0
    assert c._arch_light_min == 0.0
    # ...and only this tick's own elapsed time may land in the bucket, which a 3-day dt cannot.
    assert c._arch_deep_min == 0.0


def test_a_within_night_dropout_does_not_reset():
    """A sensor gap of a few minutes is not a new night; resetting on it would erase the
    architecture every time the band stuttered."""
    c = _c()
    t = datetime(2026, 9, 1, 2, 0)
    c._arch_last_ts = t
    c._accrue_architecture(t + timedelta(minutes=6), SleepStage.REM)
    assert c._arch_rem_min >= 97.9


def test_the_gap_threshold_sits_between_the_two():
    """Comfortably longer than any within-night dropout, comfortably shorter than a day."""
    assert 60.0 < ARCHITECTURE_GAP_RESET_MIN < 720.0


def test_a_backwards_clock_skips_the_step_rather_than_accruing_or_resetting():
    """A backward step is the DST fall-back on a naive local clock (2026-11-01 01:59:30 EDT ->
    01:00 EST), not a different night. It used to reset -- zeroing 60 min of deep and 40 of REM
    at 02:00 mid-night. Now it accrues nothing, keeps the totals, and re-anchors."""
    c = _c()
    t = datetime(2026, 9, 1, 5, 0)
    c._arch_last_ts = t
    back = t - timedelta(hours=1)
    c._accrue_architecture(back, SleepStage.LIGHT)
    assert c._arch_rem_min == 97.9
    assert c._arch_deep_min == 30.3
    assert c._arch_light_min == 242.9          # nothing accrued for the negative step
    assert c._arch_last_ts == back             # re-anchored, so the next tick accrues normally
    c._accrue_architecture(back + timedelta(minutes=1), SleepStage.LIGHT)
    assert 243.8 < c._arch_light_min < 244.0


def test_normal_accrual_still_adds():
    c = SleepController(AppConfig())
    t = datetime(2026, 9, 1, 2, 0)
    c._arch_last_ts = t
    c._accrue_architecture(t + timedelta(minutes=1), SleepStage.DEEP)
    assert 0.9 < c._arch_deep_min < 1.1
