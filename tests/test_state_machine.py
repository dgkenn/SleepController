"""The controller state machine, tested directly.

``SleepStateMachine`` decides which phase the night is in, and every thermal intent follows from
that. It was covered only indirectly, through whole-night controller tests — which means a
transition bug shows up as "the temperature looked odd on a simulated night" rather than as
"INDUCTION never advances". These pin the transition table itself.

The two properties worth stating out loud, because both protect sleep:
  * lying in bed awake must NOT be mistaken for sleep onset,
  * the wake window must win over everything, from any state, so an alarm cannot be missed.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from sleepctl.config import AppConfig
from sleepctl.controller.state_machine import SleepStateMachine
from sleepctl.models import ControllerState, SensorFrame, SleepStage

NOW = datetime(2026, 7, 29, 23, 0)
WAKE = datetime(2026, 7, 30, 7, 0)


def _frame(stage=SleepStage.AWAKE, presence=True):
    return SensorFrame(timestamp=NOW, stage=stage, presence=presence, heart_rate=60.0,
                       hrv=50.0, respiratory_rate=14.0, movement=0.1, bed_temp_f=72.0,
                       room_temp_f=68.0, data_age_seconds=10)


def _sm(state=ControllerState.IDLE):
    return SleepStateMachine(AppConfig.default(), state=state)


def _step(sm, stage=SleepStage.AWAKE, now=NOW, wake_detected=False, wake_time=WAKE,
          presence=True, onset_confirmed=None):
    return sm.transition(_frame(stage, presence), now, wake_detected, wake_time, onset_confirmed)


# ------------------------------------------------------------------ IDLE -> INDUCTION
def test_getting_into_bed_starts_induction():
    sm = _sm()
    assert _step(sm) is ControllerState.INDUCTION
    assert sm.reason == "got into bed"


def test_an_empty_bed_stays_idle():
    sm = _sm()
    assert _step(sm, presence=False) is ControllerState.IDLE


def test_calibration_also_advances_on_presence():
    sm = _sm(ControllerState.CALIBRATION)
    assert _step(sm) is ControllerState.INDUCTION


# ------------------------------------------------------------------ onset
def test_lying_in_bed_awake_never_confirms_onset():
    """The property that protects the whole night: awake-in-bed must not read as asleep."""
    sm = _sm(ControllerState.INDUCTION)
    for _ in range(20):
        assert _step(sm, stage=SleepStage.AWAKE) is ControllerState.INDUCTION


def test_the_fallback_heuristic_needs_a_streak_not_one_sample():
    """With no onset detector wired, a single asleep sample must not flip the state — one
    mislabelled epoch would otherwise start maintenance while the user is still awake."""
    sm = _sm(ControllerState.INDUCTION)
    assert _step(sm, stage=SleepStage.LIGHT) is ControllerState.INDUCTION
    assert _step(sm, stage=SleepStage.LIGHT) is ControllerState.MAINTENANCE
    assert sm.reason == "sleep onset confirmed"


def test_the_asleep_streak_resets_on_an_awake_sample():
    sm = _sm(ControllerState.INDUCTION)
    _step(sm, stage=SleepStage.LIGHT)
    _step(sm, stage=SleepStage.AWAKE)          # breaks the run
    assert _step(sm, stage=SleepStage.LIGHT) is ControllerState.INDUCTION


@pytest.mark.parametrize("stage", [SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM])
def test_every_sleep_stage_counts_toward_onset(stage):
    sm = _sm(ControllerState.INDUCTION)
    _step(sm, stage=stage)
    assert _step(sm, stage=stage) is ControllerState.MAINTENANCE


@pytest.mark.parametrize("stage", [SleepStage.AWAKE, SleepStage.UNKNOWN])
def test_awake_and_unknown_do_not_count_toward_onset(stage):
    sm = _sm(ControllerState.INDUCTION)
    for _ in range(5):
        assert _step(sm, stage=stage) is ControllerState.INDUCTION


def test_an_explicit_detector_overrides_the_heuristic_in_both_directions():
    """When the real onset detector is wired it is authoritative — it can confirm onset on the
    first sample, and can withhold it however long the stage says 'asleep'."""
    early = _sm(ControllerState.INDUCTION)
    assert _step(early, stage=SleepStage.AWAKE,
                 onset_confirmed=True) is ControllerState.MAINTENANCE

    withheld = _sm(ControllerState.INDUCTION)
    for _ in range(10):
        assert _step(withheld, stage=SleepStage.DEEP,
                     onset_confirmed=False) is ControllerState.INDUCTION


# ------------------------------------------------------------------ awakening + recovery
def test_a_detected_awakening_enters_recovery():
    sm = _sm(ControllerState.MAINTENANCE)
    assert _step(sm, stage=SleepStage.AWAKE,
                 wake_detected=True) is ControllerState.WAKE_RECOVERY
    assert sm.reason == "awakening detected"


def test_recovery_requires_both_time_and_stability():
    """Neither a quiet minute nor a long unstable stretch is enough on its own."""
    cfg = AppConfig.default()
    sm = SleepStateMachine(cfg, state=ControllerState.MAINTENANCE)
    _step(sm, stage=SleepStage.AWAKE, wake_detected=True)      # -> WAKE_RECOVERY

    # stable, but too soon
    soon = NOW + timedelta(seconds=30)
    _step(sm, stage=SleepStage.LIGHT, now=soon)
    assert _step(sm, stage=SleepStage.LIGHT, now=soon) is ControllerState.WAKE_RECOVERY

    # long enough, but not stable
    later = NOW + timedelta(minutes=cfg.tunables.wake_recovery_minutes + 1)
    assert _step(sm, stage=SleepStage.AWAKE, now=later) is ControllerState.WAKE_RECOVERY

    # both -> back to maintenance
    _step(sm, stage=SleepStage.LIGHT, now=later)
    assert _step(sm, stage=SleepStage.LIGHT, now=later) is ControllerState.MAINTENANCE
    assert sm.reason == "physiology re-stabilized"


def test_a_fresh_awakening_during_recovery_resets_the_stability_streak():
    cfg = AppConfig.default()
    sm = SleepStateMachine(cfg, state=ControllerState.MAINTENANCE)
    _step(sm, stage=SleepStage.AWAKE, wake_detected=True)
    later = NOW + timedelta(minutes=cfg.tunables.wake_recovery_minutes + 1)
    _step(sm, stage=SleepStage.LIGHT, now=later)
    _step(sm, stage=SleepStage.LIGHT, now=later, wake_detected=True)   # resets
    assert _step(sm, stage=SleepStage.LIGHT, now=later) is ControllerState.WAKE_RECOVERY


# ------------------------------------------------------------------ the wake window wins
@pytest.mark.parametrize("start", [ControllerState.INDUCTION, ControllerState.MAINTENANCE,
                                   ControllerState.WAKE_RECOVERY])
def test_the_wake_window_is_entered_from_every_sleeping_state(start):
    """An alarm must never be missable because of which phase the night happened to be in."""
    sm = _sm(start)
    inside = WAKE - timedelta(minutes=5)
    assert _step(sm, stage=SleepStage.DEEP, now=inside) is ControllerState.WAKE_WINDOW
    assert sm.reason == "entered wake window"


def test_the_wake_window_outranks_a_live_awakening():
    sm = _sm(ControllerState.MAINTENANCE)
    inside = WAKE - timedelta(minutes=5)
    assert _step(sm, stage=SleepStage.AWAKE, now=inside,
                 wake_detected=True) is ControllerState.WAKE_WINDOW


def test_the_window_opens_exactly_wake_window_min_before_the_deadline():
    cfg = AppConfig.default()
    w = cfg.tunables.wake_window_min
    sm = _sm(ControllerState.MAINTENANCE)
    assert _step(sm, stage=SleepStage.DEEP,
                 now=WAKE - timedelta(minutes=w + 1)) is ControllerState.MAINTENANCE
    assert _step(sm, stage=SleepStage.DEEP,
                 now=WAKE - timedelta(minutes=w)) is ControllerState.WAKE_WINDOW


def test_no_wake_time_means_no_wake_window():
    sm = _sm(ControllerState.MAINTENANCE)
    assert _step(sm, stage=SleepStage.DEEP, wake_time=None) is ControllerState.MAINTENANCE


def test_the_wake_window_holds_until_the_bed_is_empty():
    sm = _sm(ControllerState.WAKE_WINDOW)
    past = WAKE + timedelta(minutes=10)
    assert _step(sm, stage=SleepStage.AWAKE, now=past) is ControllerState.WAKE_WINDOW
    assert _step(sm, stage=SleepStage.AWAKE, now=past,
                 presence=False) is ControllerState.IDLE
    assert sm.reason == "left bed after wake time"


def test_leaving_the_bed_mid_night_does_not_end_the_session():
    """A bathroom trip before the wake time must not reset the night to IDLE."""
    sm = _sm(ControllerState.MAINTENANCE)
    mid = NOW + timedelta(hours=1)
    assert _step(sm, stage=SleepStage.AWAKE, now=mid,
                 presence=False) is not ControllerState.IDLE


# ------------------------------------------------------------------ reporting
def test_a_held_state_says_so_rather_than_still_reporting_init():
    sm = _sm(ControllerState.MAINTENANCE)
    _step(sm, stage=SleepStage.DEEP)
    assert sm.reason == "hold state"
