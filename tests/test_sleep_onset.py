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


# ---------------------------------------------------------------------------------------
# Lapse tolerance. A single non-qualifying sample used to reset the whole persistence run,
# which made "10 minutes of sustained sleep" mean "the 2-of-N test fires on EVERY sample
# without exception". The HR-derived signals are per-sample noisy, so on a real HR-only night
# (2026-08-27) the run was reset 6 times across 22 UNBROKEN minutes of LIGHT staging with HR
# drifting 70->63, reaching at most 4.0 min. Onset never confirmed, the controller never left
# INDUCTION, and wake detection/prevention -- which live in MAINTENANCE -- never armed.
# ---------------------------------------------------------------------------------------

def _noisy_hr_night(t0, minutes=25, samples_per_min=2):
    """Sustained LIGHT sleep on an HR-ONLY feed (no accelerometer, no PPI -> no movement, no
    respiration, no HRV), with the beat-to-beat wobble a real armband actually delivers. This is
    exactly the feed the forwarder falls back to when the band's PMD streams are refused."""
    hr_wobble = [69, 68, 67, 67, 72, 67, 69, 66, 70, 69, 70, 75, 68, 70, 71, 68, 67, 71, 70, 72,
                 69, 68, 67, 68, 63, 65, 64, 66, 63, 64]
    frames = []
    n = minutes * samples_per_min
    for i in range(n):
        ts = t0 + timedelta(seconds=int(i * 60 / samples_per_min))
        frames.append(SensorFrame(timestamp=ts, stage=SleepStage.LIGHT, stage_confidence=0.55,
                                  heart_rate=float(hr_wobble[i % len(hr_wobble)]),
                                  hrv=None, respiratory_rate=None, movement=None, presence=None))
    return frames


def test_hr_noise_alone_no_longer_destroys_the_persistence_run():
    cfg = AppConfig.default()
    det = SleepOnsetDetector(cfg)
    t0 = datetime(2026, 8, 27, 22, 10)
    # 12 min of awake-in-bed first, so there is a baseline for hr_drop to work against.
    warmup = [_frame(t0 + timedelta(minutes=i - 12), SleepStage.AWAKE, hr=76, move=None,
                     rr=None, hrv=None, presence=None, conf=0.6) for i in range(12)]
    ev = _run(det, warmup + _noisy_hr_night(t0), t0 - timedelta(minutes=12))
    assert ev is not None, "22 unbroken minutes of light sleep still failed to confirm onset"
    assert ev.timestamp >= t0


def test_a_lapse_longer_than_the_tolerance_still_breaks_the_run():
    """Tolerance forgives the ABSENCE of evidence briefly, not indefinitely. A long stretch with
    nothing qualifying means the descent really did break."""
    cfg = AppConfig.default()
    det = SleepOnsetDetector(cfg)
    t0 = datetime(2026, 8, 27, 22, 10)
    frames = _noisy_hr_night(t0, minutes=3)
    # A flat, unmoving HR: stage still says LIGHT, but nothing evidences a transition any more.
    frames += [SensorFrame(timestamp=t0 + timedelta(minutes=3 + i), stage=SleepStage.LIGHT,
                           stage_confidence=0.55, heart_rate=70.0, hrv=None,
                           respiratory_rate=None, movement=None, presence=None)
               for i in range(10)]
    _run(det, frames, t0)
    assert det._run_start is None, "a 10-minute lapse should have broken the run"


def test_an_awake_label_breaks_the_run_outright_rather_than_being_tolerated():
    """The tolerance is for missing evidence, never for contradicting evidence."""
    cfg = AppConfig.default()
    det = SleepOnsetDetector(cfg)
    t0 = datetime(2026, 8, 27, 22, 10)
    frames = _noisy_hr_night(t0, minutes=4)
    frames.append(SensorFrame(timestamp=t0 + timedelta(minutes=4), stage=SleepStage.AWAKE,
                              stage_confidence=0.8, heart_rate=78.0, hrv=None,
                              respiratory_rate=None, movement=None, presence=None))
    _run(det, frames, t0)
    assert det._run_start is None
    assert det._transition_hits == 0


def test_gross_movement_still_breaks_the_run_through_a_tolerated_lapse():
    cfg = AppConfig.default()
    det = SleepOnsetDetector(cfg)
    t0 = datetime(2026, 6, 23, 23, 0)
    frames = [_frame(t0 + timedelta(minutes=i), SleepStage.LIGHT, hr=58 - i * 0.2, move=0.05)
              for i in range(5)]
    frames.append(_frame(t0 + timedelta(minutes=5), SleepStage.LIGHT, hr=58, move=0.9))
    _run(det, frames, t0)
    assert det._run_start is None


def test_a_run_that_survives_only_on_tolerance_never_confirms():
    """Confirmation needs evidence of an actual DESCENT spread across the run, not merely a run
    that managed to stay open. Without the transition-hit floor, lapse tolerance would let a
    person lying awake in bed under a LIGHT label confirm onset by doing nothing at all."""
    cfg = AppConfig.default()
    det = SleepOnsetDetector(cfg)
    det.min_transition_hits = 99            # nothing can meet it
    det.break_tolerance_min = 10_000.0      # nothing can break the run either
    t0 = datetime(2026, 8, 27, 22, 10)
    assert _run(det, _noisy_hr_night(t0, minutes=40), t0) is None


def test_tolerance_does_not_resurrect_the_awake_in_bed_false_positive():
    """The 2026-08-06 failure: an hour awake in bed, still, HR 77,76,75,76,76,75 -- no decline at
    all -- was confirmed as onset on `asleep_stage` + `stillness`. It must stay rejected."""
    cfg = AppConfig.default()
    det = SleepOnsetDetector(cfg)
    t0 = datetime(2026, 8, 6, 21, 0)
    flat = [77, 76, 75, 76, 76, 75]
    frames = [_frame(t0 + timedelta(minutes=i), SleepStage.LIGHT, hr=float(flat[i % len(flat)]),
                     move=0.05, rr=None, hrv=None, presence=True, conf=0.6)
              for i in range(60)]
    assert _run(det, frames, t0) is None
