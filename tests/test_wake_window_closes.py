"""The wake window has to close, and the abandon clock has to survive a restart.

Both of these let a session outlive the night on real, published data:

  2026-08-25   WAKE_RECOVERY from 12:00 to 18:37 -- 6.6 hours, 786 ticks, zero heart rate,
               zero movement, commanding the bed the whole time.
  2026-08-27   induction/maintenance until 11:21 through a walking-around morning.
"""

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.state_machine import SleepStateMachine
from sleepctl.models import ControllerState, SensorFrame, SleepStage


def _frame(ts, stage=SleepStage.UNKNOWN, hr=None):
    return SensorFrame(timestamp=ts, stage=stage, heart_rate=hr)


def _sm(state):
    sm = SleepStateMachine(AppConfig())
    sm.state = state
    return sm


def test_wake_window_closes_without_a_bed_exit():
    """WAKE_WINDOW was written to hold 'until the user leaves the bed', and a bed exit needs
    `presence is False` -- which an account with no Autopilot membership never reports. It was a
    terminal state escapable only by restarting the daemon."""
    cfg = AppConfig()
    wake = datetime(2026, 8, 28, 6, 0)
    sm = _sm(ControllerState.WAKE_WINDOW)
    late = wake + timedelta(minutes=cfg.tunables.wake_window_close_min + 5)
    assert sm.transition(_frame(late), late, False, wake) is ControllerState.IDLE


def test_wake_window_stays_open_inside_its_bounds():
    cfg = AppConfig()
    wake = datetime(2026, 8, 28, 6, 0)
    sm = _sm(ControllerState.WAKE_WINDOW)
    inside = wake + timedelta(minutes=cfg.tunables.wake_window_close_min - 5)
    assert sm.transition(_frame(inside), inside, False, wake) is ControllerState.WAKE_WINDOW


def test_wake_recovery_cannot_outlive_the_window():
    """2026-08-25 exactly: recovery needs `_is_asleep`, which needs a stage, which needs a feed.
    With no feed the stable streak can never build and the state has no exit at all."""
    cfg = AppConfig()
    wake = datetime(2026, 8, 25, 6, 0)
    sm = _sm(ControllerState.WAKE_RECOVERY)
    late = wake + timedelta(hours=6)
    assert sm.transition(_frame(late), late, False, wake) is ControllerState.IDLE


def test_a_closed_window_does_not_pull_maintenance_into_it():
    """An unbounded `now >= required_wake - window` stayed true for the rest of the day, so any
    later session was dragged straight into WAKE_WINDOW."""
    wake = datetime(2026, 8, 28, 6, 0)
    sm = _sm(ControllerState.MAINTENANCE)
    much_later = wake + timedelta(hours=9)
    got = sm.transition(_frame(much_later, SleepStage.LIGHT), much_later, False, wake)
    assert got is ControllerState.MAINTENANCE


# ------------------------------------------------------------- the abandon clock across restarts
def _controller():
    from sleepctl.controller.controller import SleepController
    return SleepController(AppConfig())


def test_restore_last_physio_seeds_the_abandon_clock():
    c = _controller()
    stale = datetime(2026, 8, 25, 11, 0)
    c.restore_last_physio(stale)
    assert c._recovered_physio_at == stale


def test_restore_last_physio_never_overrides_a_live_clock():
    """Like `restore_bed_entry`, it only ever fills a gap -- calling it on a running session
    must not rewind a clock the live path has already set."""
    c = _controller()
    live = datetime(2026, 8, 25, 12, 0)
    c._last_physio_at = live
    c.restore_last_physio(datetime(2026, 8, 25, 6, 0))
    assert c._recovered_physio_at is None
    assert c._last_physio_at == live


def test_restore_last_physio_ignores_none():
    c = _controller()
    c.restore_last_physio(None)
    assert c._recovered_physio_at is None
