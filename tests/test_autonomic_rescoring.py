"""Beat-interval HRV per epoch rescoring REM vs deep: self-normalised, bounded."""
import math
import random

from sleepctl.controller.autonomic_rescoring import AutonomicRescorer, epoch_hrv


def _rr_series(t0, seconds, mean_ms, rsa_ms, lf_ms, seed=1):
    """Beat intervals with a 0.25 Hz respiratory modulation (HF) and a 0.1 Hz LF modulation."""
    rnd = random.Random(seed)
    out, t = [], t0
    while t < t0 + seconds:
        rr = mean_ms + rsa_ms * math.sin(2 * math.pi * 0.25 * (t - t0)) \
            + lf_ms * math.sin(2 * math.pi * 0.1 * (t - t0)) + rnd.gauss(0, 3)
        out.append((t, rr))
        t += rr / 1000.0
    return out


def test_epoch_hrv_measures_rmssd_and_lf_hf():
    deep = _rr_series(0.0, 320, 1000, rsa_ms=60, lf_ms=10)
    rem = _rr_series(0.0, 320, 900, rsa_ms=10, lf_ms=60)
    fd, fr = epoch_hrv(deep, 320.0), epoch_hrv(rem, 320.0)
    assert fd and fr
    assert fd["rmssd"] > fr["rmssd"]
    assert fd["lf_hf"] < fr["lf_hf"]


def test_the_rescorer_needs_a_night_before_it_judges_then_flags_strong_epochs():
    r = AutonomicRescorer()
    t = 0.0
    # a baseline of ordinary light-sleep epochs
    for i in range(25):
        series = _rr_series(t, 320, 950, rsa_ms=30, lf_ms=30, seed=i)
        out = r.assess(series, t + 320)
        t += 30
    assert out is not None and out["suggest"] is None
    deep = _rr_series(t, 320, 1000, rsa_ms=90, lf_ms=5, seed=99)
    assert r.assess(deep, t + 320)["suggest"] == "deep"
    rem = _rr_series(t + 30, 320, 880, rsa_ms=3, lf_ms=25, seed=98)
    assert r.assess(rem, t + 350)["suggest"] == "rem"


def test_too_few_beats_is_no_reading():
    r = AutonomicRescorer()
    assert r.assess(_rr_series(0.0, 60, 1000, 30, 30), 60.0) is None
