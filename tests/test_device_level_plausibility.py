"""`user.heating_level` is the DEVICE's readback, and on six occasions across 2026-08-25..27 it
returned exactly -100 for a single poll and then reverted -- e.g. -66 -> -100 -> -61 inside one
minute. The bed slews ~1.5 levels/min cooling and ~4 warming, so it can neither reach -100 from
-66 nor climb back in that time. These are bad reads.

They are not harmless: ControlCycle.pending_level compares this value against the commanded
target with a 5-level tolerance and RE-ASSERTS when they diverge, so each bad read triggered a
spurious device write and a junk intervention row.
"""
from sleepctl.adapters.eightsleep_cloud import EightSleepClient


def _c():
    return EightSleepClient(email="e", password="p")


def test_the_first_reading_is_always_accepted():
    c = _c()
    assert c._plausible_level(-66, 60.0) == -66


def test_the_measured_glitch_is_rejected_and_the_last_good_level_held():
    """The exact observed sequence: -66 -> -100 -> -61 within one minute."""
    c = _c()
    assert c._plausible_level(-66, 60.0) == -66
    assert c._plausible_level(-100, 60.0) == -66, "the impossible jump must not pass through"
    assert c._plausible_level(-61, 60.0) == -61, "a plausible reading resumes normally"
    assert c._rejected_levels == 1


def test_an_ordinary_ramp_passes_untouched():
    c = _c()
    out = [c._plausible_level(lv, 60.0) for lv in range(-68, -30, 4)]
    assert out == list(range(-68, -30, 4))
    assert c._rejected_levels == 0


def test_a_slow_poll_is_given_proportionally_more_room():
    """A 5-minute gap legitimately allows a much larger move than a 1-minute one."""
    c = _c()
    c._plausible_level(-70, 60.0)
    assert c._plausible_level(-30, 300.0) == -30


def test_the_same_jump_is_rejected_over_a_short_poll():
    c = _c()
    c._plausible_level(-70, 60.0)
    assert c._plausible_level(-30, 60.0) == -70


def test_none_passes_through_as_none():
    """No reading is different from a bad reading; the caller must still see 'no data'."""
    c = _c()
    c._plausible_level(-60, 60.0)
    assert c._plausible_level(None, 60.0) is None


def test_a_non_numeric_reading_is_treated_as_no_data():
    c = _c()
    assert c._plausible_level("junk", 60.0) is None


def test_a_missing_age_does_not_raise():
    c = _c()
    c._plausible_level(-60, None)
    assert c._plausible_level(-62, None) == -62


def test_holding_the_last_good_level_rather_than_none_keeps_the_reassert_check_alive():
    """Returning None would read as 'no device data' and disable the drift check entirely --
    turning a bad read into a silently unmonitored bed."""
    c = _c()
    c._plausible_level(-66, 60.0)
    assert c._plausible_level(-100, 60.0) is not None
