"""Sleep onset on a heart-rate-only night (no movement, HRV or respiration channels).

Measured 2026-09-07: the stager said LIGHT from 22:00 and the heart rate sat 3-5 bpm under its
bed-entry level, yet onset confirmed at 23:54. The awake reference was a rolling mean of the
very frames whose decline it was meant to detect."""
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.sleep_onset import SleepOnsetDetector
from sleepctl.models import SensorFrame, SleepStage

T0 = datetime(2026, 9, 7, 21, 41)


def _hr_only(ts, stage, hr, conf=0.6):
    return SensorFrame(timestamp=ts, stage=stage, stage_confidence=conf, heart_rate=hr,
                       hrv=None, respiratory_rate=None, movement=None, presence=None)


def _run(det, frames):
    recent, first = [], None
    for f in frames:
        r = det.evaluate(f, recent, f.timestamp, bed_entry_time=T0)
        if r is not None and first is None:
            first = (f.timestamp, r)
        recent.append(f)
        recent = recent[-15:]
    return first


def test_a_sustained_drop_below_the_bed_entry_rate_confirms_within_minutes():
    det = SleepOnsetDetector(AppConfig.default())
    frames = []
    # first five minutes in bed: ~70 bpm, no stage yet
    for i in range(10):
        frames.append(_hr_only(T0 + timedelta(seconds=30 * i), SleepStage.UNKNOWN, 70 + (i % 2)))
    # then LIGHT at ~66 bpm with ordinary beat-to-beat wobble, for 40 minutes
    for i in range(80):
        hr = 66 + (1 if i % 3 == 0 else 0) - (1 if i % 5 == 0 else 0)
        frames.append(_hr_only(T0 + timedelta(minutes=5) + timedelta(seconds=30 * i),
                               SleepStage.LIGHT, hr))
    first = _run(det, frames)
    assert first is not None, "onset never confirmed on a clear, sustained drop"
    confirmed_at, ev = first
    assert confirmed_at <= T0 + timedelta(minutes=25), f"took until {confirmed_at}"
    assert ev.timestamp <= T0 + timedelta(minutes=12), "onset should be back-dated to the drop"


def test_an_awake_hour_at_a_flat_rate_still_does_not_confirm():
    """2026-08-06: awake in bed for an hour at 75-77 bpm, stager saying LIGHT -- no decline, no
    onset. The bed-entry reference must not weaken that."""
    det = SleepOnsetDetector(AppConfig.default())
    frames = []
    seq = [77, 76, 75, 76, 76, 75]
    for i in range(120):
        frames.append(_hr_only(T0 + timedelta(seconds=30 * i), SleepStage.LIGHT, seq[i % 6]))
    assert _run(det, frames) is None


def test_awake_labelled_frames_still_take_precedence_over_the_entry_reference():
    det = SleepOnsetDetector(AppConfig.default())
    frames = []
    for i in range(10):
        frames.append(_hr_only(T0 + timedelta(seconds=30 * i), SleepStage.AWAKE, 60))
    # entry reference would say 60; AWAKE frames say 60 too -- a later 66 is NOT a drop
    for i in range(60):
        frames.append(_hr_only(T0 + timedelta(minutes=5) + timedelta(seconds=30 * i),
                               SleepStage.LIGHT, 66))
    assert _run(det, frames) is None


def test_mark_confirmed_reports_like_a_native_confirmation():
    det = SleepOnsetDetector(AppConfig.default())
    det.mark_confirmed(T0 + timedelta(minutes=20), latency_min=20.0)
    assert det.onset_time == T0 + timedelta(minutes=20)
    f = _hr_only(T0 + timedelta(hours=2), SleepStage.LIGHT, 66)
    assert det.evaluate(f, [], f.timestamp, bed_entry_time=T0).signals == ["restored"]
