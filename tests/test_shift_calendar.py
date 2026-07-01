"""Tests for classify_shift, the pure day/night classifier that bridges the OAuth-free ICS
calendar feed (each event IS a shift, start-end) into the shift planner's Shift.kind. See
sleepctl/adapters/calendar.py for the full rationale; the calendar-sync + daemon-wake-hook
behavior built on top of this lives in dashboard/api/tests/test_shift_calendar.py (it needs the
services layer + a repo, which aren't available from this pure-sleepctl test tree)."""

from datetime import datetime

from sleepctl.adapters.calendar import classify_shift


def test_classify_evening_start_is_night():
    # 16:00 (4pm) is the night boundary -- evening starts count as night.
    assert classify_shift(datetime(2026, 7, 6, 19, 0)) == "night"
    assert classify_shift(datetime(2026, 7, 6, 16, 0)) == "night"


def test_classify_overnight_early_morning_start_is_night():
    # A shift starting at 00:00-03:59 (e.g. picking up mid-rotation) is still "night".
    assert classify_shift(datetime(2026, 7, 6, 0, 0)) == "night"
    assert classify_shift(datetime(2026, 7, 6, 3, 59)) == "night"


def test_classify_morning_start_is_day():
    assert classify_shift(datetime(2026, 7, 6, 7, 0)) == "day"
    assert classify_shift(datetime(2026, 7, 6, 4, 0)) == "day"  # exact lower boundary -> day


def test_classify_afternoon_start_is_day():
    assert classify_shift(datetime(2026, 7, 6, 15, 59)) == "day"


def test_classify_boundary_just_before_night_cutoff():
    assert classify_shift(datetime(2026, 7, 6, 15, 0)) == "day"
