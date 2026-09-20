"""A person who is plainly up inside the wake window ends the session.

2026-09-20: awake at 04:25, WAKE_WINDOW from 04:29, a workout at 130-150 bpm from 05:28 -- and
the session ran to the window's close at 05:45, because the deadline outranked the bed-exit
rule outright. Evidence that has held a quarter of an hour in the window is someone who has
already woken.
"""
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.models import ContextRecord, ControllerState, SensorFrame, SleepStage


def _frame(ts, hr, stage=SleepStage.AWAKE):
    return SensorFrame(timestamp=ts, stage=stage, stage_confidence=0.6, heart_rate=hr,
                       movement=0.03, data_age_seconds=5.0)


def _in_window():
    cfg = AppConfig()
    c = SleepController(cfg)
    wake = datetime(2026, 9, 20, 5, 0)
    c.restore_session_state("maintenance", wake - timedelta(hours=7), {"deep_min": 30.0})
    for _ in range(40):
        c.bed_exit_detector.observe_sleeping(64.0)
    ctx = ContextRecord(date="2026-09-19", required_wake_time=wake)
    return c, cfg, wake, ctx


def _run(c, ctx, start, minutes, hr):
    recent = [_frame(start - timedelta(minutes=i), 64.0, SleepStage.LIGHT) for i in range(12, 0, -1)]
    ended_at = None
    for i in range(minutes):
        t = start + timedelta(minutes=i)
        c.decide(_frame(t, hr), ctx, recent, t)
        recent = (recent + [_frame(t, hr)])[-30:]
        if c.sm.state is ControllerState.IDLE and ended_at is None:
            ended_at = t
    return ended_at


def test_a_workout_inside_the_wake_window_ends_the_session_after_the_hold():
    c, cfg, wake, ctx = _in_window()
    start = wake + timedelta(minutes=20)          # inside the window (close is 60 min)
    ended = _run(c, ctx, start, 30, hr=135.0)
    assert ended is not None
    held = (ended - start).total_seconds() / 60.0
    assert held >= cfg.tunables.bed_exit_wake_window_persist_min
    assert held < cfg.tunables.wake_window_close_min - 20    # before the window would have closed
    assert "bed exit" in (c.sm.reason or "")


def test_awake_in_bed_at_a_resting_rate_does_not_end_the_window():
    c, cfg, wake, ctx = _in_window()
    start = wake + timedelta(minutes=20)
    assert _run(c, ctx, start, 30, hr=72.0) is None
    assert c.sm.state is not ControllerState.IDLE


def test_the_held_time_is_reported():
    from sleepctl.controller.bed_exit import BedExitDetector
    cfg, det = AppConfig(), BedExitDetector()
    for _ in range(40):
        det.observe_sleeping(64.0)
    t0 = datetime(2026, 9, 20, 5, 20)
    recent = []
    for i in range(12):
        f = _frame(t0 + timedelta(minutes=i), 120.0)
        a = det.assess(f, recent, cfg, f.timestamp)
        recent = (recent + [f])[-30:]
    assert a.held_min is not None and a.held_min >= 2.0
    assert a.to_dict()["held_min"] == round(a.held_min, 1)
