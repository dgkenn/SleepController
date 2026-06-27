"""Closing the n-of-1 loop: an active experiment's assigned arm actually modifies tonight's
SetpointProfile (previously arms were scheduled but never applied)."""

import tempfile

import pytest

from sleepctl.config import AppConfig
from sleepctl.experiment_templates import create_from_template
from sleepctl.experiments import apply_experiment_arm, assign_arm, stop_experiment
from sleepctl.storage.repository import Repository


@pytest.fixture
def repo():
    r = Repository(tempfile.mktemp(suffix=".db"))
    yield r
    r.close()


def _base():
    return AppConfig.default().default_setpoints()


def test_no_active_experiment_passes_profile_through(repo):
    base = _base()
    prof, info = apply_experiment_arm(repo, "2026-07-01", base)
    assert prof is base and info is None


def test_assigned_arm_shifts_the_setpoint(repo):
    exp = create_from_template(repo, "cooler_deep", period=1, washout=1)  # arm_b: deep_bias −2°F
    base = _base()
    # walk dates until we see both an 'a' and a 'b' assignment, checking the applied profile each
    seen = {}
    for d in range(1, 12):
        date = f"2026-07-{d:02d}"
        arm = assign_arm(repo, exp["id"], date)
        prof, info = apply_experiment_arm(repo, date, base)
        if arm == "a":
            seen["a"] = prof.deep_bias_f
            assert info["arm"] == "a"
        elif arm == "b":
            seen["b"] = prof.deep_bias_f
            assert info["applied"] is True
        elif arm == "washout":
            assert prof.deep_bias_f == base.deep_bias_f  # washout = no change
    assert "a" in seen and "b" in seen
    # arm B (the −2°F treatment) is genuinely cooler than arm A
    assert seen["b"] == pytest.approx(base.deep_bias_f - 2.0)
    assert seen["a"] == pytest.approx(base.deep_bias_f)


def test_completed_experiment_stops_applying(repo):
    exp = create_from_template(repo, "cooler_deep", period=1, washout=1)
    assign_arm(repo, exp["id"], "2026-07-01")
    stop_experiment(repo, exp["id"])
    prof, info = apply_experiment_arm(repo, "2026-07-02", _base())
    assert info is None and prof.deep_bias_f == _base().deep_bias_f


def test_controller_setter_swaps_profile():
    from sleepctl.controller.controller import SleepController
    from dataclasses import replace
    sc = SleepController(AppConfig.default())
    new = replace(sc.thermal.profile, deep_bias_f=60.0)
    sc.set_setpoints(new)
    assert sc.thermal.profile.deep_bias_f == 60.0
    sc.set_setpoints(None)  # no-op safe
    assert sc.thermal.profile.deep_bias_f == 60.0
