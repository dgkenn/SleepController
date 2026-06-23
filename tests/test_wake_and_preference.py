"""Tests for the heat+vibration smart wake and manual-override (revealed-preference) learning."""

from __future__ import annotations

from datetime import datetime, timedelta

from sleepctl.adapters.calendar import ManualCalendarSource
from sleepctl.adapters.simulator import SimulatorActuator, SimulatorSource
from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.controller.smart_wake import SmartWakeRoutine
from sleepctl.loop.nightly import NightlyUpdater
from sleepctl.loop.runtime import Runtime
from sleepctl.models import ActionRecord, NightSummary, SetpointProfile
from sleepctl.storage.repository import Repository


def test_smart_wake_programs_vibration_alarm_in_window():
    cfg = AppConfig.default()
    assert cfg.tunables.wake_vibration_enabled and cfg.tunables.wake_vibration_power > 0
    swr = SmartWakeRoutine(cfg)
    wake = datetime(2026, 6, 24, 7, 0)
    # outside the window -> no alarm
    assert swr.alarm_spec(wake - timedelta(hours=2), wake) is None
    # inside the window -> a vibration + thermal alarm (audio off)
    spec = swr.alarm_spec(wake - timedelta(minutes=10), wake)
    assert spec is not None
    assert spec.vibration_power == cfg.tunables.wake_vibration_power
    assert spec.audio is False


def test_runtime_sends_wake_alarm_with_vibration():
    cfg = AppConfig.default()
    start = datetime(2026, 6, 23, 23, 0)
    source = SimulatorSource("normal", seed=7, start=start)
    actuator = SimulatorActuator(source)
    repo = Repository(":memory:")
    wake = start + timedelta(minutes=source.length)
    ctx = ManualCalendarSource(required_wake_time=wake, bedtime=start).get_context(
        start.date().isoformat())
    Runtime(cfg, source, actuator, repo, controller=SleepController(cfg)).replay(ctx)
    # exactly one wake alarm programmed, with gentle vibration and audio off
    assert len(actuator.alarms) >= 1
    _t, vibration, _thermal = actuator.alarms[0]
    assert vibration == cfg.tunables.wake_vibration_power


def test_manual_overrides_anchor_setpoint():
    cfg = AppConfig.default()
    repo = Repository(":memory:")
    # user repeatedly manually cools to 64F
    for i in range(4):
        repo.log_action(ActionRecord(date=f"2026-06-1{i}", action_name="manual_override",
                                     params={"target_f": 64.0}, source="manual"))
    from sleepctl.ml.preference import revealed_preference
    p = cfg.default_setpoints()  # deep_bias 66, neutral 70
    anchored = revealed_preference(repo, p, cfg)
    assert anchored is not None
    # both knobs move toward 64 (cooler), bounded
    assert anchored.deep_bias_f < p.deep_bias_f
    assert anchored.neutral_f < p.neutral_f
    assert anchored.source == "manual_pref"
    # too few manual overrides -> no anchoring
    repo2 = Repository(":memory:")
    repo2.log_action(ActionRecord(date="2026-06-10", action_name="manual_override",
                                  params={"target_f": 64.0}, source="manual"))
    assert revealed_preference(repo2, p, cfg) is None


def test_manual_heavy_night_is_confounded():
    from sleepctl.ml.confounders import is_confounded
    from sleepctl.ml.dataset import FeatureRow

    clean = FeatureRow(date="d", setpoint_version=0, manual_overrides=0)
    heavy = FeatureRow(date="d", setpoint_version=0, manual_overrides=4)
    assert not is_confounded(clean)
    assert is_confounded(heavy)  # excluded from automated attribution
