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


# ---------------------------------------------------- 2026-09-20: a reset with no recorded cause
def test_every_reset_says_what_broke_the_run():
    """The run reached 9-12 ticks four times between 21:22 and 22:33 and collapsed each time;
    the published trace recorded the signals and nothing about the cause, so 96 minutes of
    INDUCTION on a sleeping user could be described and not explained."""
    from datetime import datetime, timedelta
    from sleepctl.config import AppConfig
    from sleepctl.controller.sleep_onset import SleepOnsetDetector
    from sleepctl.models import SensorFrame, SleepStage

    t = AppConfig().tunables
    det = SleepOnsetDetector(AppConfig())
    assert det.status()["last_break"] is None

    t0 = datetime(2026, 9, 20, 21, 20)

    def _f(i, stage=SleepStage.LIGHT, movement=0.02, rr=14.0, hr=64.0):
        return SensorFrame(timestamp=t0 + timedelta(minutes=i), stage=stage,
                           stage_confidence=0.7, heart_rate=hr, movement=movement,
                           respiratory_rate=rr, data_age_seconds=5.0)

    recent = []
    for i in range(6):                       # build a run
        f = _f(i)
        det.evaluate(f, recent, t0 + timedelta(minutes=i), bed_entry_time=t0)
        recent = (recent + [f])[-40:]
    assert det.status()["run_len"] > 0

    det.evaluate(_f(7, stage=SleepStage.AWAKE), recent, t0 + timedelta(minutes=7), bed_entry_time=t0)
    brk = det.status()["last_break"]
    assert brk and "AWAKE" in brk["why"] and brk["run_len"] > 0

    # only a run that had PROGRESSED is worth recording, so rebuild one before the next break
    for i in range(8, 14):
        f = _f(i)
        det.evaluate(f, recent, t0 + timedelta(minutes=i), bed_entry_time=t0)
        recent = (recent + [f])[-40:]
    assert det.status()["run_len"] > 0
    det.evaluate(_f(15, movement=0.9), recent, t0 + timedelta(minutes=15), bed_entry_time=t0)
    assert "movement" in det.status()["last_break"]["why"]


def test_the_breathing_veto_reports_its_reading():
    from datetime import datetime, timedelta
    from sleepctl.config import AppConfig
    from sleepctl.controller.sleep_onset import SleepOnsetDetector
    from sleepctl.models import SensorFrame, SleepStage

    t = AppConfig().tunables
    t0 = datetime(2026, 9, 20, 21, 20)

    def _hist(rr_of):
        return [SensorFrame(timestamp=t0 + timedelta(minutes=i), stage=SleepStage.LIGHT,
                            stage_confidence=0.7, heart_rate=64.0, movement=0.02,
                            respiratory_rate=rr_of(i), data_age_seconds=5.0)
                for i in range(t.onset_resp_cv_window + 5)]

    # wildly irregular breathing from ONE estimator: the veto fires and says what it read
    hist = _hist(lambda i: 10.0 if i % 2 else 18.0)
    det = SleepOnsetDetector(AppConfig())
    det.evaluate(hist[-1], hist[:-1], t0 + timedelta(minutes=40), bed_entry_time=t0)
    st = det.status()
    assert st["resp_cv"] is not None and st["resp_cv"] >= t.onset_resp_irregular_cv
    assert st["confirmed"] is False

    # steady breathing: measured, reported, and no veto
    steady = _hist(lambda i: 14.0 + (0.1 if i % 2 else -0.1))
    det2 = SleepOnsetDetector(AppConfig())
    det2.evaluate(steady[-1], steady[:-1], t0 + timedelta(minutes=40), bed_entry_time=t0)
    st2 = det2.status()
    assert st2["resp_cv"] is not None and st2["resp_cv"] < t.onset_resp_irregular_cv
    assert st2["last_break"] is None
