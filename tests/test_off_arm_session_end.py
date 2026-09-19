"""The band saying it is off the arm ends the session and blocks a new one.

2026-09-19 05:07: the band reported "in charger" (PMD error 13); the forwarder fell back to the
generic heart-rate service, the session stayed in MAINTENANCE and the health page read
"steering blind" for an hour after the sleeper had got up.
"""
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.models import ControllerState, SensorFrame, SleepStage


def _frame(ts, hr=64.0, off_arm=None):
    return SensorFrame(timestamp=ts, stage=SleepStage.LIGHT, stage_confidence=0.7,
                       heart_rate=hr, movement=0.02, data_age_seconds=5.0,
                       wearable_off_arm=off_arm)


def _asleep_controller():
    cfg = AppConfig()
    c = SleepController(cfg)
    t0 = datetime(2026, 9, 19, 1, 0)
    c.restore_session_state("maintenance", t0 - timedelta(hours=3), {"deep_min": 30.0})
    assert c.sm.state is ControllerState.MAINTENANCE
    return c, t0


def test_a_charging_band_ends_the_session():
    c, t0 = _asleep_controller()
    recent = [_frame(t0 - timedelta(minutes=i)) for i in range(12, 0, -1)]
    c.decide(_frame(t0, hr=None, off_arm=True), None, recent, t0)
    assert c.sm.state is ControllerState.IDLE
    assert "off the arm" in (c.sm.reason or "")
    assert c.bed_exit_events and "off the arm" in " ".join(c.bed_exit_events[-1]["reasons"])


def test_without_the_report_the_same_tick_keeps_the_session():
    c, t0 = _asleep_controller()
    recent = [_frame(t0 - timedelta(minutes=i)) for i in range(12, 0, -1)]
    c.decide(_frame(t0, hr=None), None, recent, t0)
    assert c.sm.state is not ControllerState.IDLE


def test_bed_entry_is_refused_while_the_band_is_off_the_arm():
    c = SleepController(AppConfig())
    t0 = datetime(2026, 9, 19, 21, 0)
    recent = [_frame(t0 - timedelta(minutes=i), hr=70.0 + (i % 3)) for i in range(12, 0, -1)]
    assert c._wearable_bed_entry(_frame(t0, hr=71.0, off_arm=True), recent, c.cfg) is False
    assert "off the arm" in (c.last_bed_entry_block or "")
    assert c._wearable_bed_entry(_frame(t0, hr=71.0), recent, c.cfg) is True
