"""Two breathing estimators alternating is not irregular breathing.

2026-09-19 21:06-21:31: the fused rate alternated 9.7 (RSA-only ticks) and 17.0 (ACC-only
ticks); the onset run that had reached 12 ticks was broken and pinned at zero for 25 minutes on
a sleeping user, until the stage-persistence fallback confirmed at 21:36.
"""
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.sleep_onset import SleepOnsetDetector
from sleepctl.models import SensorFrame, SleepStage


def _frame(ts, rr, src, hr=66.0):
    f = SensorFrame(timestamp=ts, stage=SleepStage.LIGHT, stage_confidence=0.6, heart_rate=hr,
                    hrv=45.0, respiratory_rate=rr, movement=0.02, data_age_seconds=5.0)
    f.respiratory_rate_source = src
    return f


def _feed(det, cfg, frames):
    recent = []
    t0 = frames[0].timestamp
    for f in frames:
        det.evaluate(f, recent, f.timestamp, bed_entry_time=t0 - timedelta(minutes=20))
        recent.append(f)
        if len(recent) > 60:
            recent.pop(0)
    return det


def _hr(i):
    """A heart rate settling from 74 to 62 across the window, so the transition signals the
    run needs (hr_drop / hr_trend_down) are present in every scenario; only the breathing
    pattern differs between tests."""
    return max(62.0, 74.0 - 0.3 * i)


def _alternating(t0, n, a=("rsa", 9.7), b=("acc", 17.0)):
    out = []
    for i in range(n):
        src, rr = (a if i % 2 == 0 else b)
        out.append(_frame(t0 + timedelta(seconds=30 * i), rr, src, hr=_hr(i)))
    return out


def test_alternating_estimators_do_not_break_the_run():
    cfg = AppConfig()
    det = SleepOnsetDetector(cfg)
    t0 = datetime(2026, 9, 19, 21, 0)
    _feed(det, cfg, _alternating(t0, 40))
    assert det._run_len > 0, "the run was broken by estimator alternation"


def test_genuinely_irregular_breathing_from_one_estimator_still_breaks_the_run():
    cfg = AppConfig()
    det = SleepOnsetDetector(cfg)
    t0 = datetime(2026, 9, 19, 21, 0)
    frames = [_frame(t0 + timedelta(seconds=30 * i), (9.5 if i % 2 == 0 else 17.0), "rsa", hr=_hr(i))
              for i in range(40)]
    _feed(det, cfg, frames)
    assert det._run_len == 0


def test_a_fused_rate_is_compatible_with_either_estimator():
    cfg = AppConfig()
    det = SleepOnsetDetector(cfg)
    t0 = datetime(2026, 9, 19, 21, 0)
    frames = []
    for i in range(40):
        src = ("rsa+acc", 12.4) if i % 3 == 0 else (("rsa", 12.1) if i % 3 == 1 else ("acc", 12.8))
        frames.append(_frame(t0 + timedelta(seconds=30 * i), src[1], src[0], hr=_hr(i)))
    _feed(det, cfg, frames)
    assert det._run_len > 0


def test_the_status_reports_the_tick_it_saw_even_when_the_run_is_broken():
    """The trace showed the same three signals frozen for 25 minutes: the early return never
    updated them, so the reset looked causeless."""
    cfg = AppConfig()
    det = SleepOnsetDetector(cfg)
    t0 = datetime(2026, 9, 19, 21, 0)
    frames = [_frame(t0 + timedelta(seconds=30 * i), (9.5 if i % 2 == 0 else 17.0), "rsa")
              for i in range(30)]
    _feed(det, cfg, frames)
    assert isinstance(det.status().get("signals"), list)
