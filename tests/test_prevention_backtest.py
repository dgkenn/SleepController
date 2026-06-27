"""Backtest: does the predictive layer prevent awakenings on 1-MINUTE-resolution data?

This is the proof that awakening prevention is workable WITHOUT rooting — i.e. on the
~60s cloud cadence that is the hard floor (the Pod firmware itself only derives HR/HRV
per minute, so even rooting wouldn't give faster vitals).

It builds nights at 1-frame-per-minute and checks three things, grounded in the evidence:
  1. A realistic slow PRE-AROUSAL DRIFT (HR creep, HRV decay, building restlessness over a
     few minutes — the minute-resolution-visible signal) is detected and pre-empted with
     real lead time BEFORE the awakening. (Togo 2006 doi:10.1016/j.clinph.2006.07.314;
     Calandra-Buonaura 2012 doi:10.1016/j.sleep.2011.11.007 — autonomic precedes arousal.)
  2. Stable sleep does NOT trigger a pre-empt (no false alarms / no needless thermal swings,
     since instability itself promotes arousal — Mahapatra 2005 doi:10.1016/j.physbeh.2004.12.003).
  3. An ABRUPT exogenous awakening (no slow precursor — noise/bladder/dream) is honestly
     NOT predicted early. Minute data cannot catch the seconds-scale precursor, so a fraction
     of awakenings is intrinsically unpreventable (BuSha 2001 doi:10.1093/sleep/24.5.499).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.precursor import PrecursorDetector
from sleepctl.controller.wake_risk import WakeRiskAssessor, WakeProfile
from sleepctl.models import SensorFrame, SleepStage

CFG = AppConfig.default()
START = datetime(2026, 6, 27, 23, 0, 0)
HR_BASE, HRV_BASE = 56.0, 48.0


def _frame(minute, hr, hrv, move, stage=SleepStage.LIGHT, rr=14.0, bed=70.0):
    return SensorFrame(
        timestamp=START + timedelta(minutes=minute),
        stage=stage, stage_confidence=0.85,
        heart_rate=hr, hrv=hrv, respiratory_rate=rr, movement=move,
        presence=True, bed_temp_f=bed, room_temp_f=68.0,
        commanded_level=-40, data_age_seconds=30.0,
    )


def _run_detector(frames):
    """Return the minute index at which the precursor detector first wants to pre-empt."""
    det = PrecursorDetector(CFG)
    for i, f in enumerate(frames):
        a = det.detect(f, frames[:i], f.timestamp, HR_BASE, HRV_BASE)
        if a.should_preempt:
            return i
    return None


def test_slow_drift_is_preempted_with_lead_time_at_1min_cadence():
    # 10 min stable, then a 6-min pre-arousal drift, then the awakening at minute 16.
    frames = [_frame(m, HR_BASE, HRV_BASE, 0.08) for m in range(10)]
    awaken_min = 16
    for k, m in enumerate(range(10, awaken_min)):     # drift minutes 10..15
        frames.append(_frame(m, HR_BASE + 1.0 * (k + 1), HRV_BASE - 1.6 * (k + 1),
                             0.08 + 0.05 * (k + 1)))
    frames.append(_frame(awaken_min, HR_BASE + 9, HRV_BASE - 12, 0.7, stage=SleepStage.AWAKE))

    fired = _run_detector(frames)
    assert fired is not None, "predictive layer never pre-empted the drift"
    lead_min = awaken_min - fired
    # On 1-min data the detector needs a few samples of slope; it must still act BEFORE the
    # awakening (positive lead), which is the whole point of prevention.
    assert lead_min >= 1, f"pre-empt fired too late (lead {lead_min} min)"


def test_stable_sleep_does_not_false_alarm():
    frames = [_frame(m, HR_BASE, HRV_BASE, 0.08, stage=SleepStage.DEEP if m % 3 else SleepStage.LIGHT)
              for m in range(20)]
    assert _run_detector(frames) is None, "stable sleep should not trigger a pre-empt (no swings)"


def test_abrupt_exogenous_awakening_is_honestly_unpredictable():
    # No slow drift: stable, then an instantaneous awakening (noise/bladder). Minute-resolution
    # data carries no precursor here — we must NOT pretend to predict it.
    frames = [_frame(m, HR_BASE, HRV_BASE, 0.08) for m in range(12)]
    frames.append(_frame(12, HR_BASE + 9, HRV_BASE - 14, 0.7, stage=SleepStage.AWAKE))
    fired = _run_detector(frames)
    # It may flag only AT/after the awakening minute (12), never several minutes early.
    assert fired is None or fired >= 11, "claimed to predict an unpredictable abrupt awakening"


def test_structural_window_gives_prophylactic_risk_without_a_trigger():
    # Early-morning circadian-nadir window (the highest-yield prophylaxis target, Raymann 2008
    # doi:10.1093/brain/awm315): risk should be elevated from the standing prior even when the
    # instantaneous physiology is calm — enabling stable, anticipatory protection.
    profile = WakeProfile.evidence_default()
    assr = WakeRiskAssessor(CFG, profile=profile)
    now = START.replace(hour=4, minute=30)            # ~270 min past midnight = nadir zone
    calm = _frame(300, HR_BASE, HRV_BASE, 0.08, stage=SleepStage.LIGHT)
    risk = assr.assess(calm, [calm], now, target_temp_f=70.0,
                       sleep_hr_baseline=HR_BASE, minutes_since_onset=300.0)
    assert "circadian_nadir" in risk.reasons
    assert risk.score > 0.0
