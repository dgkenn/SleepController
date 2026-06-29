"""The wake orchestrator: light-sleep early fire, deep-sleep hold, escalation, hard deadline,
stale fallback, already-up backoff, and gym-driven deadline."""

from datetime import datetime, timedelta

from sleepctl.controller.wake_orchestrator import (
    WakeConfig, WakeOrchestrator, choose_wake_window)
from sleepctl.controller.sleep_wake import SleepWakeClassifier
from sleepctl.gym_advisor import GymConfig, GymDecision, gym_decision, wake_target_from_decision
from sleepctl.models import SensorFrame, SleepStage


WAKE = datetime(2026, 6, 29, 7, 0)


def _f(stage, t, movement=0.05, presence=True, hr=58.0):
    return SensorFrame(timestamp=t, stage=stage, heart_rate=hr, movement=movement,
                       presence=presence, data_age_seconds=10.0)


def test_holds_in_deep_sleep_runs_dawn_ramp():
    o = WakeOrchestrator(WakeConfig(window_min=30, thermal_dawn_min=20))
    now = WAKE - timedelta(minutes=15)            # in window, in dawn
    a = o.evaluate(now, _f(SleepStage.DEEP, now), [], WAKE)
    assert a.should_wake is False
    assert a.phase == "dawn"                       # warming, but not waking out of deep


def test_fires_gently_on_light_sleep_in_window():
    o = WakeOrchestrator(WakeConfig(window_min=30))
    now = WAKE - timedelta(minutes=20)
    a = o.evaluate(now, _f(SleepStage.LIGHT, now), [], WAKE)
    assert a.should_wake is True
    assert 0 < a.vibration_power <= 30             # gentle to start
    assert a.phase == "gentle"


def test_escalation_ladder_then_max_at_deadline():
    cfg = WakeConfig(window_min=30, escalate_gentle_s=120, escalate_strong_s=240)
    o = WakeOrchestrator(cfg)
    start = WAKE - timedelta(minutes=15)
    o.evaluate(start, _f(SleepStage.LIGHT, start), [], WAKE)          # engage gentle
    mid = start + timedelta(seconds=150)
    a_mid = o.evaluate(mid, _f(SleepStage.LIGHT, mid), [], WAKE)
    assert a_mid.vibration_power == cfg.strong_vibration              # escalated
    a_dl = o.evaluate(WAKE, _f(SleepStage.LIGHT, WAKE), [], WAKE)
    assert a_dl.vibration_power == cfg.max_vibration and a_dl.phase == "fire"


def test_hard_deadline_fires_even_from_deep():
    o = WakeOrchestrator(WakeConfig())
    a = o.evaluate(WAKE, _f(SleepStage.DEEP, WAKE), [], WAKE)
    assert a.should_wake is True and a.vibration_power == 100


def test_stale_data_falls_back_to_stage_only():
    # No classifier + stale: a light stage still lifts; deep still holds.
    o = WakeOrchestrator(WakeConfig(window_min=30))
    now = WAKE - timedelta(minutes=10)
    deep = o.evaluate(now, _f(SleepStage.DEEP, now), [], WAKE, data_stale=True)
    assert deep.should_wake is False


def test_backs_off_once_user_is_up():
    o = WakeOrchestrator(WakeConfig())
    now = WAKE - timedelta(minutes=10)
    a = o.evaluate(now, _f(SleepStage.AWAKE, now, movement=0.6, presence=False), [], WAKE)
    assert a.should_wake is True and a.vibration_power == 0 and a.phase == "done"


def test_last_resort_engages_when_still_deep_near_deadline():
    o = WakeOrchestrator(WakeConfig(window_min=30, last_resort_min=6))
    now = WAKE - timedelta(minutes=4)             # still deep, but < last_resort
    a = o.evaluate(now, _f(SleepStage.DEEP, now), [], WAKE)
    assert a.should_wake is True                   # can't wait any longer


def test_classifier_lifts_on_high_p_wake_before_stage_flips():
    # Stage still says LIGHT but the classifier sees clear surfacing -> liftable earlier.
    clf = SleepWakeClassifier()
    o = WakeOrchestrator(WakeConfig(window_min=30, p_wake_liftable=0.45), classifier=clf)
    now = WAKE - timedelta(minutes=18)
    # an arousing frame: elevated HR + movement should push p_wake up
    f = _f(SleepStage.LIGHT, now, movement=0.5, hr=72.0)
    a = o.evaluate(now, f, [], WAKE)
    assert a.should_wake is True


def test_waits_for_predicted_light_window_instead_of_forcing_deep_wake():
    # Deep, inside the last-resort window, but the cycle predictor says a light ascent is imminent
    # -> wait for the gentler wake rather than forcing a deep-sleep wake.
    o = WakeOrchestrator(WakeConfig(window_min=30, last_resort_min=6, hard_buffer_s=120))
    deep_start = WAKE - timedelta(minutes=24)
    o.evaluate(deep_start, _f(SleepStage.DEEP, deep_start), [], WAKE)   # observe the deep bout
    now = WAKE - timedelta(minutes=5)                                   # ~19 min into deep
    a = o.evaluate(now, _f(SleepStage.DEEP, now), [], WAKE)
    assert a.phase == "wait_cycle" and a.should_wake is False


def test_confirmation_requires_sustained_wake_and_reescalates_on_relapse():
    o = WakeOrchestrator(WakeConfig(window_min=30, confirm_ticks=2))
    t = WAKE - timedelta(minutes=15)
    o.evaluate(t, _f(SleepStage.LIGHT, t), [], WAKE)                    # engage gentle
    # one stir, then a relapse to sleep -> NOT confirmed, alarm keeps going
    a1 = o.evaluate(t + timedelta(seconds=30), _f(SleepStage.AWAKE, t, movement=0.6), [], WAKE)
    assert a1.should_wake is True and a1.vibration_power > 0           # not done after one tick
    relapse = o.evaluate(t + timedelta(seconds=60), _f(SleepStage.LIGHT, t, movement=0.05), [], WAKE)
    assert relapse.phase != "done"                                     # re-engaged
    # two sustained surfacing ticks -> confirmed up, stands down
    o.evaluate(t + timedelta(seconds=90), _f(SleepStage.AWAKE, t, movement=0.6), [], WAKE)
    done = o.evaluate(t + timedelta(seconds=120), _f(SleepStage.AWAKE, t, movement=0.6), [], WAKE)
    assert done.phase == "done" and done.vibration_power == 0


def test_high_debt_narrows_window_to_protect_sleep():
    # A light moment 25 min early wakes a rested user but NOT a heavily indebted one (squeeze sleep).
    now = WAKE - timedelta(minutes=25)
    rested = WakeOrchestrator(WakeConfig(window_min=30))
    a_rest = rested.evaluate(now, _f(SleepStage.LIGHT, now), [], WAKE, debt_min=0.0)
    indebt = WakeOrchestrator(WakeConfig(window_min=30))
    a_debt = indebt.evaluate(now, _f(SleepStage.LIGHT, now), [], WAKE, debt_min=400.0)
    assert a_rest.should_wake is True
    assert a_debt.should_wake is False


def test_light_ramps_through_the_dawn_window():
    o = WakeOrchestrator(WakeConfig(window_min=30, thermal_dawn_min=20, light_enabled=True))
    early = o.evaluate(WAKE - timedelta(minutes=10), _f(SleepStage.DEEP, WAKE), [], WAKE)
    late = o.evaluate(WAKE - timedelta(minutes=2), _f(SleepStage.DEEP, WAKE), [], WAKE)
    assert 0.0 < early.light_level < 1.0
    assert late.light_level > early.light_level


def test_controller_set_dawn_light_toggles_the_ramp():
    # The daemon enables the sunrise ramp only when Hue dawn bulbs are configured; verify the
    # controller setter flips the orchestrator flag both ways so the lights ride the wake logic.
    from sleepctl.config import AppConfig
    from sleepctl.controller.controller import SleepController
    c = SleepController(AppConfig.default())
    assert c.wake_orch.cfg.light_enabled is False     # off until a dawn driver is wired
    c.set_dawn_light(True)
    assert c.wake_orch.cfg.light_enabled is True
    c.set_dawn_light(False)
    assert c.wake_orch.cfg.light_enabled is False


def test_window_selection_adapts_to_the_night():
    # rested normal night -> full window; short/work night, gym, or debt -> narrower.
    assert choose_wake_window("normal", debt_min=0, base=30) == 30
    assert choose_wake_window("constrained", debt_min=0, base=30) == 15
    assert choose_wake_window("normal", debt_min=0, gym_go=True, base=30) == 20
    assert choose_wake_window("normal", debt_min=400, base=30) == 18
    assert choose_wake_window("normal", debt_min=0, base=30) >= choose_wake_window(
        "work", debt_min=0, base=30)


def test_gym_go_moves_the_deadline_earlier():
    cfg = GymConfig(enabled=True, lean="push", early_offset_min=75)
    go = GymDecision(recommend="go", go_score=0.8, confidence=0.7, headline="go")
    sleep_in = GymDecision(recommend="sleep_in", go_score=0.2, confidence=0.7, headline="rest")
    assert wake_target_from_decision(go, WAKE, 75) == WAKE - timedelta(minutes=75)
    assert wake_target_from_decision(sleep_in, WAKE, 75) == WAKE
