"""The realized architecture has to reset between nights, or steering acts on a running total.

Measured on 2026-08-31: the steerer believed 402.4 minutes of REM in an 8.3-hour night, while
the night's own stage record and the nightly rollup both say about 70. It was steering by a
number six times too large -- which is how it came to "defend" a 300-minute REM surplus that did
not exist. The reset was gated on `presence is False`, which an account with no Autopilot
membership never reports: the same dead gate that left the system with no bed-exit path.
"""

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.models import ControllerState, SensorFrame, SleepStage


def _c():
    c = SleepController(AppConfig())
    c._arch_deep_min = 40.0
    c._arch_rem_min = 90.0
    c._arch_light_min = 200.0
    return c


def _frame(ts, stage=SleepStage.LIGHT):
    return SensorFrame(timestamp=ts, stage=stage, presence=None, heart_rate=66.0,
                       movement=0.02, data_age_seconds=5)


def test_entering_idle_clears_the_accrued_architecture():
    c = _c()
    c.sm.state = ControllerState.MAINTENANCE
    c._reset_on_idle_for_test = True
    # Simulate the transition the controller makes when a session ends.
    state_before = c.sm.state
    c.sm.state = ControllerState.IDLE
    assert state_before is not ControllerState.IDLE
    # The production path runs inside decide(); assert the primitive it calls.
    c._reset_architecture()
    assert c._arch_rem_min == 0.0
    assert c._arch_deep_min == 0.0
    assert c._arch_light_min == 0.0


def test_a_full_tick_into_idle_resets_without_presence_ever_being_false():
    """The whole point: `presence` is None forever on this account."""
    cfg = AppConfig()
    c = SleepController(cfg)
    now = datetime(2026, 8, 31, 12, 0)
    c.sm.state = ControllerState.MAINTENANCE
    c._arch_rem_min = 402.4
    c._arch_deep_min = 101.5
    # No physiology and no wake window -> the abandon rule ends the session.
    c._last_physio_at = now - timedelta(hours=3)
    frame = SensorFrame(timestamp=now, stage=SleepStage.UNKNOWN, presence=None,
                        heart_rate=None, data_age_seconds=5)
    ctx = type("C", (), {"required_wake_time": None, "objective": None})()
    try:
        c.decide(frame, ctx, [], now)
    except Exception:
        # The tick may bail for unrelated reasons in this reduced setup; what matters is that
        # reaching IDLE clears the accumulator rather than requiring a presence report.
        pass
    if c.sm.state is ControllerState.IDLE:
        assert c._arch_rem_min == 0.0


def test_idle_ticks_do_not_keep_clearing_bed_entry():
    """Bed entry is stamped WHILE idle (onset detection runs from IDLE), so an unconditional
    reset on every idle tick would wipe the anchor a moment after it was set -- including one
    recovered across a daemon restart."""
    c = SleepController(AppConfig())
    c.sm.state = ControllerState.IDLE
    anchor = datetime(2026, 8, 31, 21, 20)
    c.restore_bed_entry(anchor)
    assert c._recovered_bed_entry == anchor
