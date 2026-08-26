"""Tests for the controller sanity gate (validation-stack gate 1).

The gate answers "is the deployed controller behaving like the validated one?" -- an
engineering question with no ground truth required. These tests pin the two properties that
matter: it must FAIL the real failure modes measured on this deployment, and it must not
confuse UNMEASURED with BROKEN.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sleepctl.eval.controller_sanity import compute_controller_sanity


def _rows(stages, start=None, step_min=1.0, state="maintenance"):
    t0 = start or datetime(2026, 8, 24, 23, 0)
    return [{"ts": (t0 + timedelta(minutes=i * step_min)).isoformat(),
             "stage": s, "controller_state": state}
            for i, s in enumerate(stages)]


def _ivs(actions, start=None, step_min=5.0):
    t0 = start or datetime(2026, 8, 24, 23, 0)
    return [{"ts": (t0 + timedelta(minutes=i * step_min)).isoformat(), "action": a}
            for i, a in enumerate(actions)]


def _healthy_night():
    """~7 h with realistic 20-30 min bouts, front-loaded deep, ~20% REM."""
    plan = [("light", 20), ("deep", 30), ("light", 25), ("rem", 20),
            ("light", 25), ("deep", 25), ("light", 30), ("rem", 25),
            ("light", 30), ("rem", 25), ("light", 25), ("awake", 5),
            ("light", 25), ("rem", 25), ("light", 30), ("deep", 15), ("light", 30)]
    stages = []
    for s, n in plan:
        stages.extend([s] * n)
    return stages


# ------------------------------------------------------------------ passes a sane night
def test_a_well_behaved_night_passes():
    s = compute_controller_sanity(_rows(_healthy_night()), _ivs(["cooler"] * 4))
    assert s["insufficient"] is False
    assert s["passed"] is True, s["failed_checks"]


# ------------------------------------------------------------------ the real failure modes
def test_flapping_staging_fails_the_flip_gate():
    """THE 2026-08-23/24 failure: 183-242 stage flips per night against a CV-validated true
    rate of ~7.6 and an unsmoothed model prediction of ~10.3 -- the deployed estimator does not
    behave like either validated configuration."""
    stages = ["light", "rem"] * 200          # flips every single sample
    s = compute_controller_sanity(_rows(stages))
    assert s["passed"] is False
    assert "flip_rate" in s["failed_checks"]
    assert "bout_length" in s["failed_checks"]


def test_thermal_hunting_fails_even_when_staging_is_fine():
    """A controller can fail on CONTROL behaviour with perfectly good labels: 31 of 36
    consecutive interventions were direction reversals on 2026-08-24. The existing oscillation
    guardrail could not catch them -- it ignores reversals below 0.75F and these were all
    sub-threshold, so it flagged one of thirty-one."""
    s = compute_controller_sanity(_rows(_healthy_night()),
                                  _ivs(["cooler", "warmer"] * 30, step_min=2.0))
    assert s["passed"] is False
    assert s["failed_checks"] == ["thermal_stability"]


def test_implausible_architecture_is_flagged():
    stages = (["rem"] * 300) + (["light"] * 100)      # REM at 75% of sleep
    s = compute_controller_sanity(_rows(stages))
    assert "rem_fraction" in s["failed_checks"]


def test_flat_deep_profile_is_flagged_as_not_front_loaded():
    """Real SWS is heavily front-loaded. Measured 5.6%/5.6%/8.9% by third on 2026-08-24 --
    slightly inverted, i.e. the estimator is not tracking sleep pressure at all."""
    third = ["light"] * 40 + ["deep"] * 10
    s = compute_controller_sanity(_rows(third + third + (["light"] * 30 + ["deep"] * 20)))
    assert "deep_front_loaded" in s["failed_checks"]


# ------------------------------------------------------------------ unmeasured != broken
def test_a_night_with_no_staging_is_insufficient_not_failed():
    """The 2026-08-22/25 case: the controller never left IDLE so nothing was staged. That is a
    missing measurement, and must never be scored as a controller failure -- they have entirely
    different remedies."""
    s = compute_controller_sanity(_rows(["unknown"] * 500))
    assert s["insufficient"] is True
    assert s["passed"] is None


def test_a_handful_of_samples_is_insufficient():
    s = compute_controller_sanity(_rows(["light"] * 5))
    assert s["insufficient"] is True


def test_no_rows_at_all_is_insufficient_and_does_not_raise():
    assert compute_controller_sanity([], []) ["insufficient"] is True
    assert compute_controller_sanity(None, None)["insufficient"] is True
