"""Tests for the RSA-derived respiratory-rate estimator (``sleepctl.controller.respiration``).

Pure and offline: synthetic tachograms with a known breathing frequency, plus the two failure
modes that made the first attempt at this produce a confident WRONG answer on real data.
"""

from __future__ import annotations

import math
import random

from sleepctl.controller import respiration as R


def _tachogram(brpm: float, minutes: float = 2.0, mean_rr_ms: float = 1000.0,
               depth_ms: float = 40.0, noise_ms: float = 0.0, seed: int = 7):
    """RR series whose intervals are sinusoidally modulated at ``brpm`` -- i.e. textbook RSA."""
    rnd = random.Random(seed)
    hz = brpm / 60.0
    out, t = [], 0.0
    while t < minutes * 60.0:
        rr = mean_rr_ms + depth_ms * math.sin(2.0 * math.pi * hz * t)
        if noise_ms:
            rr += rnd.gauss(0.0, noise_ms)
        out.append(rr)
        t += rr / 1000.0
    return out


def test_recovers_a_known_breathing_rate():
    for brpm in (12.0, 15.0, 18.0):
        est = R.estimate(_tachogram(brpm))
        assert est is not None, f"failed to detect {brpm} brpm"
        assert abs(est.breaths_per_min - brpm) <= 1.5, (brpm, est.breaths_per_min)
        assert est.concentration >= R.MIN_CONCENTRATION


def test_survives_realistic_measurement_noise():
    est = R.estimate(_tachogram(14.0, noise_ms=12.0))
    assert est is not None
    assert abs(est.breaths_per_min - 14.0) <= 2.0


def test_returns_none_on_noise_instead_of_inventing_a_rate():
    """A wrong respiratory rate is WORSE than none: it would make the onset detector's
    respiration_slowed / respiration_regular signals fire on noise and manufacture a false
    sleep onset. Refusing to answer is the correct behaviour."""
    rnd = random.Random(3)
    assert R.estimate([1000.0 + rnd.gauss(0, 30) for _ in range(240)]) is None


def test_rejects_the_lf_band_edge_artefact():
    """REGRESSION. Mayer waves (~0.1 Hz) sit just BELOW the respiratory band and carried 85% of
    total power on this user's real data. A naive band-maximum then pins to the 0.15 Hz edge and
    reports ~9 brpm -- below the physiological floor the band itself assumes, and the tell that
    it is leakage. The first version of this analysis shipped exactly that wrong answer (10.5
    brpm) before the interior-local-maximum + concentration gates were added.
    """
    # a strong 0.10 Hz oscillation and NOTHING in the respiratory band
    out, t = [], 0.0
    while t < 180.0:
        rr = 1000.0 + 60.0 * math.sin(2.0 * math.pi * 0.10 * t)
        out.append(rr)
        t += rr / 1000.0
    est = R.estimate(out)
    # must NOT report a confident ~9 brpm pinned to the band edge
    if est is not None:
        assert est.breaths_per_min > 10.0, (
            f"reported {est.breaths_per_min:.1f} brpm -- that is the LF-leakage artefact")


def test_too_few_intervals_returns_none():
    assert R.estimate(_tachogram(14.0)[:10]) is None
    assert R.estimate([]) is None


def test_implausible_intervals_are_filtered_not_trusted():
    series = _tachogram(14.0)
    series[:0] = [50.0, 5000.0, -100.0, float("nan")]     # artefacts
    est = R.estimate(series)
    assert est is not None
    assert abs(est.breaths_per_min - 14.0) <= 2.0


def test_uniform_estimator_recovers_a_known_rate_from_a_motion_signal():
    """The accelerometer path shares the RR path's spectral core and gates rather than growing a
    second, separately-tuned copy. A worn accelerometer carries the breathing rhythm directly
    (the body moves), giving an INDEPENDENT estimate that fails differently from RSA -- RSA dies
    under sympathetic arousal, accelerometry under gross movement -- so agreement between them is
    far stronger evidence than either alone.
    """
    fs = 52.0
    for brpm in (12.0, 15.0):
        hz = brpm / 60.0
        # ~1 g gravity plus a small respiratory modulation, as an armband actually sees
        sig = [1.0 + 0.01 * math.sin(2.0 * math.pi * hz * (i / fs))
               for i in range(int(fs * 120))]
        est = R.estimate_uniform(sig, fs)
        assert est is not None, f"failed to detect {brpm} brpm from motion"
        assert abs(est.breaths_per_min - brpm) <= 1.5


def test_uniform_estimator_refuses_gross_movement():
    """Thrashing must yield None, not a number -- the accelerometer's characteristic failure."""
    rnd = random.Random(11)
    fs = 52.0
    sig = [1.0 + rnd.gauss(0, 0.3) for _ in range(int(fs * 120))]
    assert R.estimate_uniform(sig, fs) is None
