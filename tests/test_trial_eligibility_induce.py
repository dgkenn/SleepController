"""An evening "help me fall asleep" is a normal night, so the trials may randomize it.

Every night since the trials were armed started with that button, so session_mode read
"induce", the eligibility gate refused it, and neither trial randomized a single night --
the thermal trial reported 0 resolved nights after a week "enabled" (2026-09-22).
"""
import pytest

from sleepctl.ml import efficacy_trial, thermal_trial


@pytest.mark.parametrize("mod", [thermal_trial, efficacy_trial])
def test_an_evening_induce_on_a_normal_night_is_eligible(mod):
    for hour in (18, 21, 23, 0, 2, 3):
        assert mod.is_eligible({"night_type": "normal", "session_mode": "induce",
                                "started_hour": hour}), hour


@pytest.mark.parametrize("mod", [thermal_trial, efficacy_trial])
def test_a_daytime_induce_is_still_a_nap_and_refused(mod):
    for hour in (4, 9, 13, 17):
        assert not mod.is_eligible({"night_type": "normal", "session_mode": "induce",
                                    "started_hour": hour}), hour


@pytest.mark.parametrize("mod", [thermal_trial, efficacy_trial])
def test_a_caller_that_does_not_know_the_hour_gets_the_old_answer(mod):
    assert not mod.is_eligible({"night_type": "normal", "session_mode": "induce"})


@pytest.mark.parametrize("mod", [thermal_trial, efficacy_trial])
def test_naps_and_non_normal_nights_stay_out(mod):
    assert not mod.is_eligible({"night_type": "normal", "session_mode": "nap", "started_hour": 22})
    assert not mod.is_eligible({"night_type": "recovery", "session_mode": "induce",
                                "started_hour": 22})
    assert not mod.is_eligible({"night_type": None, "session_mode": "night"})
    assert mod.is_eligible({"night_type": "normal", "session_mode": "night"})


def test_both_trials_keep_the_identical_gate():
    """Deliberately duplicated so one trial's change cannot silently alter the other's --
    which also means they must be changed together."""
    cases = [{"night_type": nt, "session_mode": sm, "started_hour": h}
             for nt in ("normal", "recovery", None)
             for sm in ("night", "induce", "nap")
             for h in (None, 2, 9, 22)]
    for c in cases:
        assert thermal_trial.is_eligible(c) == efficacy_trial.is_eligible(c), c
