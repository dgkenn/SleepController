"""The opt-in post-wake cool snap.

Cool skin is alerting the same way warm skin is sleep-permissive (Te Lindert & Van Someren 2018),
so a brief cold stimulus after you've surfaced attacks sleep inertia with the lever that caused it.

The safety property that matters: it is gated on CONFIRMED wake, so it can never cool a sleeper.
Previously the flag existed and was read into WakeConfig but nothing consumed it — turning it on
silently did nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.thermal import ThermalController
from sleepctl.controller.wake_orchestrator import WakeConfig, WakeOrchestrator
from sleepctl.models import NightObjective, SensorFrame, SleepStage, ThermalIntent

DEADLINE = datetime(2026, 7, 28, 6, 30)


def _frame(now, stage=SleepStage.AWAKE, hr=70.0, move=0.6, presence=True):
    return SensorFrame(timestamp=now, stage=stage, presence=presence, heart_rate=hr,
                       hrv=40.0, respiratory_rate=15.0, movement=move,
                       bed_temp_f=74.0, room_temp_f=68.0, data_age_seconds=5)


def _confirm_up(orch, start):
    """Drive enough surfacing ticks that the orchestrator confirms the wake."""
    act = None
    for i in range(6):
        now = start + timedelta(minutes=i)
        act = orch.evaluate(now, _frame(now), [], DEADLINE)
        if orch._confirmed:
            return now, act
    return start, act


# ------------------------------------------------------------------ the intent itself
def test_cold_snap_intent_maps_to_the_configured_target():
    cfg = AppConfig.default()
    pol = ThermalController(cfg)
    t = pol.target_for(ThermalIntent.WAKE_COLD_SNAP, NightObjective.OPTIMIZE, hot_sleeper=True)
    assert abs(t - cfg.tunables.wake_cold_snap_f) < 1e-6


def test_cold_snap_does_not_double_down_on_the_hot_sleeper_bias():
    """It's already at one extreme; applying the cool bias on top would overshoot."""
    cfg = AppConfig.default()
    pol = ThermalController(cfg)
    hot = pol.target_for(ThermalIntent.WAKE_COLD_SNAP, NightObjective.OPTIMIZE, hot_sleeper=True)
    cool = pol.target_for(ThermalIntent.WAKE_COLD_SNAP, NightObjective.OPTIMIZE, hot_sleeper=False)
    assert hot == cool


def test_cold_snap_is_colder_than_the_wake_ramp():
    cfg = AppConfig.default()
    pol = ThermalController(cfg)
    snap = pol.target_for(ThermalIntent.WAKE_COLD_SNAP, NightObjective.OPTIMIZE, hot_sleeper=True)
    ramp = pol.target_for(ThermalIntent.WAKE_RAMP, NightObjective.OPTIMIZE, hot_sleeper=True)
    assert snap < ramp


# ------------------------------------------------------------------ orchestration
def test_disabled_by_default_no_cold_snap():
    orch = WakeOrchestrator(WakeConfig(window_min=30))
    up_at, _ = _confirm_up(orch, DEADLINE - timedelta(minutes=5))
    act = orch.evaluate(up_at + timedelta(minutes=1), _frame(up_at), [], DEADLINE)
    assert act.thermal_intent is not ThermalIntent.WAKE_COLD_SNAP


def test_enabled_emits_the_cold_snap_after_confirmed_wake():
    orch = WakeOrchestrator(WakeConfig(window_min=30, cold_snap_enabled=True, cold_snap_min=10))
    up_at, _ = _confirm_up(orch, DEADLINE - timedelta(minutes=5))
    assert orch._confirmed, "precondition: wake must be confirmed"
    act = orch.evaluate(up_at + timedelta(minutes=1), _frame(up_at), [], DEADLINE)
    assert act.phase == "post_wake"
    assert act.thermal_intent is ThermalIntent.WAKE_COLD_SNAP
    assert "cool snap" in act.reason


def test_cold_snap_never_fires_before_wake_is_confirmed():
    """The safety property: a sleeping user must never be cold-snapped."""
    orch = WakeOrchestrator(WakeConfig(window_min=30, cold_snap_enabled=True))
    now = DEADLINE - timedelta(minutes=25)
    for i in range(10):
        t = now + timedelta(minutes=i)
        act = orch.evaluate(t, _frame(t, stage=SleepStage.DEEP, hr=52.0, move=0.02), [], DEADLINE)
        assert act.thermal_intent is not ThermalIntent.WAKE_COLD_SNAP, act.phase
    assert not orch._confirmed


def test_cold_snap_expires_after_its_window():
    orch = WakeOrchestrator(WakeConfig(window_min=30, cold_snap_enabled=True, cold_snap_min=10))
    up_at, _ = _confirm_up(orch, DEADLINE - timedelta(minutes=5))
    late = orch.evaluate(up_at + timedelta(minutes=11), _frame(up_at), [], DEADLINE)
    assert late.thermal_intent is not ThermalIntent.WAKE_COLD_SNAP
    assert late.phase == "done"


def test_bed_exit_ends_the_cold_snap():
    """Cooling an empty bed is pointless; presence=False must stand it down immediately."""
    orch = WakeOrchestrator(WakeConfig(window_min=30, cold_snap_enabled=True, cold_snap_min=10))
    up_at, _ = _confirm_up(orch, DEADLINE - timedelta(minutes=5))
    gone = orch.evaluate(up_at + timedelta(minutes=1),
                         _frame(up_at, presence=False), [], DEADLINE)
    assert gone.thermal_intent is not ThermalIntent.WAKE_COLD_SNAP


def test_post_wake_no_longer_requires_the_light_to_be_enabled():
    """Regression: the post-wake phase used to be gated on light_enabled, so a cool snap with no
    smart bulb configured could never have been reached."""
    orch = WakeOrchestrator(WakeConfig(window_min=30, cold_snap_enabled=True,
                                       light_enabled=False, cold_snap_min=10))
    up_at, _ = _confirm_up(orch, DEADLINE - timedelta(minutes=5))
    act = orch.evaluate(up_at + timedelta(minutes=1), _frame(up_at), [], DEADLINE)
    assert act.phase == "post_wake"
    assert act.light_level == 0.0, "no bulb configured -> no light commanded"


def test_light_and_snap_compose_and_report_both():
    orch = WakeOrchestrator(WakeConfig(window_min=30, cold_snap_enabled=True,
                                       light_enabled=True, post_wake_light_min=20,
                                       cold_snap_min=10))
    up_at, _ = _confirm_up(orch, DEADLINE - timedelta(minutes=5))
    act = orch.evaluate(up_at + timedelta(minutes=1), _frame(up_at), [], DEADLINE)
    assert "bright light dose" in act.reason and "cool snap" in act.reason
    assert act.light_level == 1.0
    assert act.thermal_intent is ThermalIntent.WAKE_COLD_SNAP


def test_light_outlasts_the_snap():
    """Different durations: after the snap expires the light dose continues on NEUTRAL."""
    orch = WakeOrchestrator(WakeConfig(window_min=30, cold_snap_enabled=True,
                                       light_enabled=True, post_wake_light_min=20,
                                       cold_snap_min=10))
    up_at, _ = _confirm_up(orch, DEADLINE - timedelta(minutes=5))
    act = orch.evaluate(up_at + timedelta(minutes=15), _frame(up_at), [], DEADLINE)
    assert act.phase == "post_wake"
    assert act.thermal_intent is ThermalIntent.NEUTRAL
    assert act.light_level == 1.0


def test_config_flag_reaches_the_orchestrator():
    cfg = AppConfig.default()
    cfg.tunables.wake_cold_snap_enabled = True
    cfg.tunables.wake_cold_snap_min = 7
    wc = WakeConfig.from_tunables(cfg.tunables)
    assert wc.cold_snap_enabled is True and wc.cold_snap_min == 7
