"""Structural constraints on the hypnogram, from the nights that needed them.

    2026-08-29    0.0% REM
    2026-08-30   69.0% REM, 0.4% deep -- REM/AWAKE flipping every 1-2 minutes for hours
    2026-08-31   16.3% REM, 14.2% deep, with DEEP scored 2 minutes after bed entry
"""

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.hypnogram import (HypnogramConstraint, architecture_plausible,
                                           constrain)
from sleepctl.models import SleepStage

T0 = datetime(2026, 8, 30, 21, 37)


def _hc():
    return HypnogramConstraint()


def test_rem_before_sleep_onset_is_not_rem():
    """2026-08-30 scored REM from 22:13 against a sleep-onset latency of 77.7 minutes."""
    v = _hc().apply(SleepStage.REM, 0.7, T0, AppConfig(), sleep_onset_time=None)
    assert v.stage is SleepStage.LIGHT
    assert v.reason == "before_sleep_onset"


def test_deep_two_minutes_after_onset_is_not_deep():
    """2026-08-31: bed entry 21:24, DEEP at 21:26."""
    v = _hc().apply(SleepStage.DEEP, 0.6, T0 + timedelta(minutes=2), AppConfig(),
                    sleep_onset_time=T0)
    assert v.stage is SleepStage.LIGHT
    assert v.reason == "deep_too_early_after_onset"


def test_rem_at_a_plausible_latency_is_left_alone():
    v = _hc().apply(SleepStage.REM, 0.7, T0 + timedelta(minutes=95), AppConfig(),
                    sleep_onset_time=T0)
    assert v.stage is SleepStage.REM
    assert v.reason is None
    assert v.confidence == 0.7


def test_sleep_does_not_resume_in_rem_after_an_awakening():
    """The rule that ends the R A R A oscillation: re-entry runs through light sleep."""
    hc, cfg = _hc(), AppConfig()
    t = T0 + timedelta(minutes=120)
    hc.observe(SleepStage.AWAKE, t)
    v = hc.apply(SleepStage.REM, 0.7, t + timedelta(minutes=1), cfg, sleep_onset_time=T0)
    assert v.stage is SleepStage.LIGHT
    assert v.reason == "no_light_sleep_since_awakening"


def test_rem_is_allowed_again_once_light_sleep_has_been_re_established():
    hc, cfg = _hc(), AppConfig()
    t = T0 + timedelta(minutes=120)
    hc.observe(SleepStage.AWAKE, t)
    hc.observe(SleepStage.LIGHT, t + timedelta(minutes=1))
    late = t + timedelta(minutes=1 + cfg.tunables.reentry_light_min + 1)
    assert hc.apply(SleepStage.REM, 0.7, late, cfg, T0).stage is SleepStage.REM


def test_an_awake_label_is_never_touched():
    """Wake responsiveness is the one thing this must not trade away."""
    hc, cfg = _hc(), AppConfig()
    hc.observe(SleepStage.AWAKE, T0)
    v = hc.apply(SleepStage.AWAKE, 0.9, T0 + timedelta(seconds=30), cfg, sleep_onset_time=None)
    assert v.stage is SleepStage.AWAKE
    assert v.reason is None
    assert v.confidence == 0.9


def test_light_passes_through_untouched():
    v = _hc().apply(SleepStage.LIGHT, 0.5, T0, AppConfig(), sleep_onset_time=None)
    assert v.stage is SleepStage.LIGHT and v.reason is None


def test_a_reclassified_epoch_loses_confidence():
    """The model said REM and we overruled it structurally; presenting LIGHT at full confidence
    would launder a disagreement into an observation."""
    v = _hc().apply(SleepStage.REM, 0.8, T0, AppConfig(), sleep_onset_time=None)
    assert v.confidence is not None and v.confidence < 0.8


def test_constraints_can_be_switched_off():
    cfg = AppConfig()
    cfg.tunables.hypnogram_constraints = False
    assert _hc().apply(SleepStage.REM, 0.7, T0, cfg, sleep_onset_time=None).stage is SleepStage.REM


def test_constrain_does_not_rewrite_the_stage_source():
    """`stage_source` names the estimator and is consumed as a fixed vocabulary."""
    hc = _hc()
    stage, conf, source = constrain((SleepStage.REM, 0.7, "model"), T0, AppConfig(), hc, None)
    assert stage is SleepStage.LIGHT
    assert source == "model"
    assert hc.last_reason == "before_sleep_onset"


# ---------------------------------------------------------------- architecture plausibility
def test_the_2026_08_30_architecture_is_rejected():
    """336 min REM against 2 min deep -- which the steerer read as a 216-minute REM surplus."""
    ok, why = architecture_plausible(deep_min=2.0, rem_min=336.1, light_min=79.0)
    assert ok is False
    assert why is not None and why.startswith("rem_fraction")


def test_a_normal_architecture_is_accepted():
    ok, why = architecture_plausible(deep_min=61.2, rem_min=70.7, light_min=254.8)
    assert ok is True and why is None


def test_too_little_sleep_to_judge_is_not_called_implausible():
    """Early in a night every fraction is noise; blocking the steerer on that would disable it
    exactly when it has the most night left to act on."""
    ok, why = architecture_plausible(deep_min=0.0, rem_min=30.0, light_min=10.0)
    assert ok is True and why is None
