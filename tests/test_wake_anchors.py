"""Tests for recall-free objective wake anchors (validation-stack gate 2)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sleepctl.eval.wake_anchors import evaluate_wake_anchors


def _rows(stages, start=None, step_min=1.0):
    t0 = start or datetime(2026, 8, 24, 23, 0)
    return [{"ts": (t0 + timedelta(minutes=i * step_min)).isoformat(), "stage": s}
            for i, s in enumerate(stages)]


def test_it_reproduces_the_documented_two_of_six_failure():
    """The precedent this module generalizes: six objectively-evidenced awakenings, of which the
    stager caught two and called three of the misses REM (state_estimator.py). A night shaped
    like that must score exactly that way."""
    stages = ["light"] * 60
    for i in (10, 20):                 # two correctly identified as awake
        stages[i] = "awake"
    for i in (30, 40, 50):             # three missed AS REM -- the documented failure mode
        stages[i] = "rem"
    stages[55] = "deep"                # one missed as deep
    t0 = datetime(2026, 8, 24, 23, 0)
    anchors = [t0 + timedelta(minutes=i) for i in (10, 20, 30, 40, 50, 55)]

    res = evaluate_wake_anchors(_rows(stages), anchors)
    assert res["n_anchors"] == 6 and res["matched"] == 6
    assert res["hits"] == 2
    assert res["misses"] == 4
    assert res["missed_as"] == {"rem": 3, "deep": 1}
    assert res["miss_rate"] == round(4 / 6, 3)


def test_a_perfect_estimator_has_a_zero_miss_rate():
    stages = ["light"] * 30
    for i in (5, 15, 25):
        stages[i] = "awake"
    t0 = datetime(2026, 8, 24, 23, 0)
    res = evaluate_wake_anchors(_rows(stages), [t0 + timedelta(minutes=i) for i in (5, 15, 25)])
    assert res["misses"] == 0 and res["miss_rate"] == 0.0


def test_an_anchor_with_no_nearby_sample_is_unmatched_not_a_miss():
    """A sensor gap must never be scored as a staging error -- unmeasured and wrong are
    different failures with different remedies."""
    t0 = datetime(2026, 8, 24, 23, 0)
    res = evaluate_wake_anchors(_rows(["light"] * 10, start=t0),
                                [t0 + timedelta(hours=5)])
    assert res["unmatched"] == 1
    assert res["matched"] == 0 and res["misses"] == 0
    assert res["miss_rate"] is None


def test_unknown_staging_does_not_count_as_a_miss():
    """"unknown" means the estimator declined to call it, which is not the same as calling an
    awake moment asleep."""
    t0 = datetime(2026, 8, 24, 23, 0)
    res = evaluate_wake_anchors(_rows(["unknown"] * 10, start=t0), [t0 + timedelta(minutes=5)])
    assert res["misses"] == 0 and res["hits"] == 1


def test_no_anchors_or_no_rows_is_reported_not_raised():
    assert evaluate_wake_anchors([], [])["n_anchors"] == 0
    assert evaluate_wake_anchors(None, None)["reason"]
    assert evaluate_wake_anchors(_rows(["light"] * 5), [])["reason"]
