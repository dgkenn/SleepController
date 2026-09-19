"""Wearable bed entry needs a full window of WORN readings, and a still one.

2026-09-18 20:06: the band had connected at 20:04, the entry window held five heart-rate readings
of 80-120 bpm among older frames with none at all, and the night opened on someone still up and
about. The bed warmed for an empty bed and induction started twenty minutes early.
"""
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.bed_exit import BedExitDetector
from sleepctl.models import SensorFrame, SleepStage


def _frame(ts, hr=None, movement=None):
    return SensorFrame(timestamp=ts, heart_rate=hr, movement=movement, stage=SleepStage.LIGHT)


def _window(hrs, movement=None, start=datetime(2026, 9, 18, 20, 0)):
    return [_frame(start + timedelta(minutes=i), h, movement) for i, h in enumerate(hrs)]


def test_a_window_of_mostly_unworn_frames_is_refused_for_lack_of_history():
    cfg, det = AppConfig(), BedExitDetector()
    frames = _window([None] * 7 + [72.0, 74.0, 73.0, 75.0])
    why = det.blocks_entry(frames[-1], frames[:-1], cfg)
    assert why is not None and "history" in why


def test_the_2026_09_18_entry_is_refused_as_someone_moving_about():
    """Five readings of 80-120 bpm among frames with no heart rate at all."""
    cfg, det = AppConfig(), BedExitDetector()
    frames = _window([None] * 6 + [90.0, 113.0, 120.0, 89.0, 80.0])
    why = det.blocks_entry(frames[-1], frames[:-1], cfg)
    assert why is not None and "swinging" in why


def test_a_wide_heart_rate_swing_is_someone_moving_about():
    cfg, det = AppConfig(), BedExitDetector()
    frames = _window([90.0, 113.0, 120.0, 89.0, 80.0, 92.0, 78.0, 96.0, 88.0, 84.0, 90.0])
    why = det.blocks_entry(frames[-1], frames[:-1], cfg)
    assert why is not None and "swinging" in why


def test_a_settling_heart_rate_within_the_spread_is_bed_entry():
    """Going from 88 to 72 over ten minutes is lying down, not walking."""
    cfg, det = AppConfig(), BedExitDetector()
    frames = _window([88.0, 86.0, 84.0, 82.0, 80.0, 78.0, 76.0, 75.0, 74.0, 73.0, 72.0])
    assert det.blocks_entry(frames[-1], frames[:-1], cfg) is None


def test_the_spread_gate_is_tunable():
    cfg, det = AppConfig(), BedExitDetector()
    cfg.tunables.bed_entry_max_hr_spread = 60.0
    frames = _window([90.0, 113.0, 90.0, 89.0, 80.0, 92.0, 78.0, 96.0, 88.0, 84.0, 90.0])
    assert det.blocks_entry(frames[-1], frames[:-1], cfg) is None
