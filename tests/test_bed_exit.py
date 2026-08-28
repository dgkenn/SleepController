"""Bed exit on wearable evidence, and the two ways a session used to outlive the night.

Every case here is drawn from a published night. The originals are named in the docstrings so a
future change that breaks one can be checked against what actually happened.
"""

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.bed_exit import BedExitDetector
from sleepctl.models import SensorFrame, SleepStage


def _frame(ts, hr=None, movement=None, stage=SleepStage.LIGHT):
    return SensorFrame(timestamp=ts, heart_rate=hr, movement=movement, stage=stage)


def _run(detector, cfg, samples, start=None):
    """Feed (hr, movement) minute by minute; return the first assessment that fires."""
    t0 = start or datetime(2026, 8, 28, 7, 0)
    recent = []
    fired = None
    for i, (hr, mv) in enumerate(samples):
        f = _frame(t0 + timedelta(minutes=i), hr, mv)
        a = detector.assess(f, recent, cfg, f.timestamp)
        if a.out_of_bed and fired is None:
            fired = a
        recent = (recent + [f])[-30:]
    return fired


def _asleep(detector, hr=68.0, n=40):
    for _ in range(n):
        detector.observe_sleeping(hr)


# ---------------------------------------------------------------- the real morning it missed
def test_walking_around_morning_ends_the_session():
    """2026-08-27: induction/maintenance ran to 11:21 with a median HR of 102-124 bpm by hour.

    The movement index read a flat 0.022 through all of it -- LOWER than during sleep -- so
    heart rate is the only channel that could have caught this.
    """
    cfg, det = AppConfig(), BedExitDetector()
    _asleep(det, 68.0)
    fired = _run(det, cfg, [(110.0, 0.022)] * 40)
    assert fired is not None
    assert "hr_above_lying_ceiling" in fired.reasons
    assert "hr_orthostatic" in fired.reasons
    assert fired.hr_excess is not None and fired.hr_excess > 30


def test_a_quiet_night_never_fires():
    """2026-08-23: a clean 8.7-hour night ending at 06:00. Zero fires is the requirement --
    a false bed exit drops the thermal command mid-night, which is the disturbance this
    controller exists to prevent."""
    cfg, det = AppConfig(), BedExitDetector()
    _asleep(det, 70.0)
    assert _run(det, cfg, [(70.0, 0.025)] * 120) is None


def test_restless_turning_alone_is_not_a_bed_exit():
    """Motion without a heart-rate rise is someone turning over, not someone standing up."""
    cfg, det = AppConfig(), BedExitDetector()
    _asleep(det, 68.0)
    assert _run(det, cfg, [(69.0, 0.8)] * 40) is None


def test_two_channels_act_faster_than_heart_rate_alone():
    """Motion corroborating an elevated heart rate takes the fast path (5 min, not 15)."""
    cfg = AppConfig()
    slow, fast = BedExitDetector(), BedExitDetector()
    _asleep(slow, 68.0)
    _asleep(fast, 68.0)
    hr_only = _run(slow, cfg, [(110.0, 0.02)] * 40)
    both = _run(fast, cfg, [(110.0, 0.8)] * 40)
    assert hr_only is not None and both is not None
    assert both.n_ticks <= hr_only.n_ticks
    assert "sustained_motion" in both.reasons


def test_a_brief_spike_does_not_qualify():
    """Persistence is the whole point: sitting up for two minutes is not getting up."""
    cfg, det = AppConfig(), BedExitDetector()
    _asleep(det, 68.0)
    assert _run(det, cfg, [(110.0, 0.9)] * 3 + [(68.0, 0.02)] * 30) is None


# ---------------------------------------------------------------- the poisoned baseline
def test_the_lying_baseline_cannot_be_poisoned_by_the_thing_it_judges():
    """Replayed unfiltered on 2026-08-27 the baseline climbed to 108.5 bpm, because the
    controller sat in MAINTENANCE through a walking-around morning and every one of those ticks
    was offered as a 'sleeping' heart rate. An orthostatic rule measured against 108.5 can never
    fire again."""
    det = BedExitDetector()
    _asleep(det, 68.0, n=30)
    for _ in range(60):
        det.observe_sleeping(115.0)
    assert det.lying_baseline is not None
    assert det.lying_baseline < 80.0


def test_the_baseline_abstains_until_there_is_enough_of_the_night():
    det = BedExitDetector()
    det.observe_sleeping(68.0)
    assert det.lying_baseline is None


def test_heart_rate_alone_still_works_with_no_baseline_at_all():
    """2026-08-25 and 2026-08-26 never produced a usable baseline. A sustained rate above the
    lying ceiling is still not someone asleep."""
    cfg, det = AppConfig(), BedExitDetector()
    fired = _run(det, cfg, [(110.0, None)] * 40)
    assert fired is not None
    assert fired.lying_baseline is None


# ---------------------------------------------------------------- the entry side
def test_entry_is_blocked_while_still_moving():
    cfg, det = AppConfig(), BedExitDetector()
    t0 = datetime(2026, 8, 28, 7, 0)
    frames = [_frame(t0 + timedelta(minutes=i), 88.0, 0.9) for i in range(12)]
    assert det.blocks_entry(frames[-1], frames[:-1], cfg) is not None


def test_entry_is_blocked_above_the_lying_ceiling():
    cfg, det = AppConfig(), BedExitDetector()
    t0 = datetime(2026, 8, 28, 7, 0)
    frames = [_frame(t0 + timedelta(minutes=i), 110.0, 0.02) for i in range(12)]
    assert det.blocks_entry(frames[-1], frames[:-1], cfg) is not None


def test_lying_down_with_a_walked_up_heart_rate_is_still_bed_entry():
    """Someone who has just got into bed has an elevated heart rate and IS lying still.
    Blocking that would stop every genuine night from starting."""
    cfg, det = AppConfig(), BedExitDetector()
    t0 = datetime(2026, 8, 27, 22, 30)
    frames = [_frame(t0 + timedelta(minutes=i), 84.0, 0.03) for i in range(12)]
    assert det.blocks_entry(frames[-1], frames[:-1], cfg) is None


def test_missing_actigraphy_does_not_block_entry():
    """No movement feed must mean "no evidence", never "evidence of moving"."""
    cfg, det = AppConfig(), BedExitDetector()
    t0 = datetime(2026, 8, 27, 22, 30)
    frames = [_frame(t0 + timedelta(minutes=i), 72.0, None) for i in range(12)]
    assert det.blocks_entry(frames[-1], frames[:-1], cfg) is None


def test_the_detector_abstains_on_too_short_a_window():
    cfg, det = AppConfig(), BedExitDetector()
    t0 = datetime(2026, 8, 28, 7, 0)
    f = _frame(t0, 130.0, 0.9)
    assert det.assess(f, [], cfg, t0).out_of_bed is False
