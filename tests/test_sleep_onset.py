"""Accurate sleep-onset detection: asleep vs lying in bed awake."""

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.sleep_onset import SleepOnsetDetector
from sleepctl.models import SensorFrame, SleepStage


def _frame(ts, stage, hr, move, rr=15.0, hrv=55.0, presence=True, conf=0.8):
    return SensorFrame(timestamp=ts, stage=stage, stage_confidence=conf, heart_rate=hr,
                       hrv=hrv, respiratory_rate=rr, movement=move, presence=presence)


def _run(detector, frames, bed_entry):
    recent = []
    result = None
    for f in frames:
        r = detector.evaluate(f, recent, f.timestamp, bed_entry_time=bed_entry)
        result = result or r
        recent.append(f)
        recent = recent[-15:]
    return result


def test_lying_awake_does_not_trigger_onset():
    cfg = AppConfig.default()
    det = SleepOnsetDetector(cfg)
    t0 = datetime(2026, 6, 23, 23, 0)
    # 30 minutes in bed AWAKE: high-ish HR, fidgeting, no sleep stage.
    frames = [_frame(t0 + timedelta(minutes=i), SleepStage.AWAKE, hr=64, move=0.4)
              for i in range(30)]
    assert _run(det, frames, t0) is None
    assert det.onset_time is None


def test_brief_light_blip_does_not_trigger():
    cfg = AppConfig.default()
    det = SleepOnsetDetector(cfg)
    t0 = datetime(2026, 6, 23, 23, 0)
    frames = [_frame(t0 + timedelta(minutes=i), SleepStage.AWAKE, hr=64, move=0.4)
              for i in range(10)]
    # a 3-minute drowsy dip then back awake — shorter than the persistence window
    for i in range(3):
        frames.append(_frame(t0 + timedelta(minutes=10 + i), SleepStage.LIGHT, hr=58, move=0.1))
    frames += [_frame(t0 + timedelta(minutes=13 + i), SleepStage.AWAKE, hr=64, move=0.4)
               for i in range(5)]
    assert _run(det, frames, t0) is None


def test_sustained_sleep_confirms_and_backdates_onset():
    cfg = AppConfig.default()
    det = SleepOnsetDetector(cfg)
    t0 = datetime(2026, 6, 23, 23, 0)
    # 12 min awake-in-bed, then sustained sleep: HR drops, still, slowed resp, HRV up.
    frames = [_frame(t0 + timedelta(minutes=i), SleepStage.AWAKE, hr=64, move=0.4)
              for i in range(12)]
    sleep_start = t0 + timedelta(minutes=12)
    for i in range(15):
        frames.append(_frame(sleep_start + timedelta(minutes=i), SleepStage.LIGHT,
                             hr=56, move=0.05, rr=13.5, hrv=66))
    ev = _run(det, frames, t0)
    assert ev is not None
    # onset back-dated to the start of the persistent run (~minute 12), not bed entry
    assert ev.timestamp == sleep_start
    assert ev.latency_min is not None and 11 <= ev.latency_min <= 13
    assert "asleep_stage" in ev.signals and len(ev.signals) >= 3


def test_idempotent_after_confirmation():
    cfg = AppConfig.default()
    det = SleepOnsetDetector(cfg)
    t0 = datetime(2026, 6, 23, 23, 0)
    frames = [_frame(t0 + timedelta(minutes=i), SleepStage.AWAKE, hr=64, move=0.4)
              for i in range(5)]
    frames += [_frame(t0 + timedelta(minutes=5 + i), SleepStage.LIGHT, hr=55, move=0.05,
                      rr=13.0, hrv=66) for i in range(15)]
    first = _run(det, frames, t0)
    assert first is not None
    # subsequent evaluate keeps returning the same confirmed onset
    again = det.evaluate(frames[-1], frames, frames[-1].timestamp, bed_entry_time=t0)
    assert again.timestamp == first.timestamp
