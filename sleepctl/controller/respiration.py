"""Respiratory rate derived from beat-to-beat intervals (RSA), pure-stdlib.

The Pod's own respiratory rate is paywalled on a membership-less account (0 of 17k samples
ever carried one), and the Polar Verity Sense does not report respiration directly. But
breathing modulates heart rate -- respiratory sinus arrhythmia -- so the respiratory rhythm is
present in the RR-interval series the armband DOES give us, and which this project already
persists in full.

Why this matters: ``SleepOnsetDetector`` scores seven signals and two of them
(``respiration_slowed``, ``respiration_regular``) need a respiratory rate. Without it they can
NEVER fire -- and ``sleep_onset.py`` itself calls breathing regularity "one of the strongest
discriminators of true sleep from quiet wakefulness". Losing them forced the onset bar down to
2 signals. ``WakeDetector`` loses ``resp_variability`` the same way.

METHOD (and why each choice was made, measured on a real night of this user's data):

  * Interpolate the RR tachogram onto an even 4 Hz grid. Uneven beat spacing otherwise smears
    the spectrum.
  * Remove the mean and apply a Hann window, then evaluate ONLY the respiratory band with a
    Goertzel filter bank (O(n) per bin, ~20 bins) instead of a full FFT -- this keeps the
    module pure-stdlib, matching the rest of the runtime (no numpy import on the control path).
  * Search 0.15-0.40 Hz = 9-24 breaths/min, the HF band standardised by the ESC/NASPE Task
    Force (1996) as the RSA/respiratory band.

TWO TRAPS, both hit for real while developing this:

  1. LF LEAKAGE. Below 0.15 Hz sits the LF band, dominated by ~0.1 Hz Mayer waves, and on this
     user's data it carried 85% of total power. A naive "peak of the HF band" then lands
     *pinned to the 0.15 Hz edge* and reports ~9 brpm -- below the physiological floor the band
     itself assumes, which is the tell that it is leakage and not breathing. :func:`estimate`
     therefore requires the peak to be a genuine interior local maximum and to carry a minimum
     share of band power, and returns None otherwise.
  2. WINDOW TOO SHORT. A 10-minute window with crude detrending gave 10.5 brpm; the same data
     with proper segment averaging gives a stable 14-15 brpm. Measured window sweep on the
     user's own uninterrupted sleep: 2 min -> median 14.0 brpm, IQR 2.0, peak concentration
     0.56 (the most stable of 2/3/5/10/15/30 min). Hence :data:`DEFAULT_WINDOW_S` = 120 s.

Returning None is a normal, frequent outcome -- during movement, a BLE dropout, or genuinely
weak RSA (sympathetic arousal suppresses it). Callers must treat respiration as OPTIONAL
evidence, exactly as they already treat a missing Pod reading.
"""

from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "estimate", "RespirationEstimate",
    "RESP_BAND_LO_HZ", "RESP_BAND_HI_HZ", "DEFAULT_WINDOW_S",
    "MIN_INTERVALS", "MIN_CONCENTRATION", "MIN_PEAK_PROMINENCE",
]

# ESC/NASPE Task Force (1996) HF band == the RSA/respiratory band.
RESP_BAND_LO_HZ = 0.15   # 9 breaths/min
RESP_BAND_HI_HZ = 0.40   # 24 breaths/min
_GRID_HZ = 4.0           # tachogram resample rate; >= 2x the top of the band, standard for HRV
DEFAULT_WINDOW_S = 120.0  # see the window sweep in the module docstring

MIN_INTERVALS = 60        # below this a 2-min window is too sparse to trust
# Share of in-band power that must sit within +/-0.03 Hz of the peak. On real data a true
# respiratory peak scored 0.39-0.62; a leakage "peak" pinned to the band edge scored ~0.24.
# Chosen from a MATCHED-window comparison of this user's real sleep against white-noise
# tachograms of identical duration (an earlier comparison was invalid -- the noise windows had
# 63 band bins vs 32 for real data, so the statistics were not comparable):
#   concentration >= 0.50  keeps 68% of real, admits 10% of noise
#   concentration >= 0.55  keeps 58% of real, admits  5% of noise   <- chosen
# Specificity is the right trade here: None is harmless (callers already degrade), whereas a
# fabricated rate makes respiration_slowed/respiration_regular fire on noise and manufacture a
# false sleep onset. 58% yield is ample -- respiration is recomputed on every ingest (~2 s).
MIN_CONCENTRATION = 0.55
# Peak prominence (in-band peak / median in-band power) is kept only as a SANITY FLOOR against a
# degenerate flat spectrum. It was evaluated as the primary gate and REJECTED on measurement:
# real p10=6.9/med=16.3 vs noise p10=4.5/med=13.2 -- the distributions overlap so heavily that no
# threshold separated them usefully (at 30 it kept just 33% of real while still admitting 14% of
# noise). Concentration is the discriminator; this only rejects the pathological case.
MIN_PEAK_PROMINENCE = 3.0

_PLAUSIBLE_RR_MS = (250.0, 2000.0)   # 240..30 bpm; drops obvious artefacts before analysis


class RespirationEstimate:
    """A respiratory-rate estimate plus the evidence for trusting it."""

    __slots__ = ("breaths_per_min", "concentration", "n_intervals", "span_s")

    def __init__(self, breaths_per_min: float, concentration: float,
                 n_intervals: int, span_s: float) -> None:
        self.breaths_per_min = breaths_per_min
        self.concentration = concentration
        self.n_intervals = n_intervals
        self.span_s = span_s

    def to_dict(self) -> dict:
        return {"breaths_per_min": round(self.breaths_per_min, 2),
                "concentration": round(self.concentration, 3),
                "n_intervals": self.n_intervals,
                "span_s": round(self.span_s, 1)}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"RespirationEstimate({self.breaths_per_min:.1f} brpm, "
                f"conc={self.concentration:.2f}, n={self.n_intervals})")


def _clean(rr_ms: Iterable[float]) -> List[float]:
    lo, hi = _PLAUSIBLE_RR_MS
    out: List[float] = []
    for v in rr_ms:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f) and lo <= f <= hi:
            out.append(f)
    return out


def _resample(rr: Sequence[float], grid_hz: float) -> List[float]:
    """RR series -> evenly sampled tachogram. Each interval is placed at its own cumulative
    time, then linearly interpolated onto the grid (beats are not evenly spaced in time)."""
    times: List[float] = []
    t = 0.0
    for v in rr:
        t += v / 1000.0
        times.append(t)
    span = times[-1] - times[0]
    if span <= 0:
        return []
    n = int(span * grid_hz)
    if n < 8:
        return []
    out: List[float] = []
    j = 0
    for i in range(n):
        ti = times[0] + i / grid_hz
        while j + 1 < len(times) and times[j + 1] < ti:
            j += 1
        if j + 1 >= len(times):
            out.append(rr[-1])
            continue
        t0, t1 = times[j], times[j + 1]
        w = 0.0 if t1 == t0 else (ti - t0) / (t1 - t0)
        out.append(rr[j] * (1.0 - w) + rr[j + 1] * w)
    return out


def _goertzel_power(x: Sequence[float], freq_hz: float, sample_hz: float) -> float:
    """Power at one frequency, O(n) and allocation-free -- the reason this module needs no FFT
    (and therefore no numpy) on the control path."""
    n = len(x)
    k = freq_hz / sample_hz
    w = 2.0 * math.pi * k
    coeff = 2.0 * math.cos(w)
    s0 = s1 = s2 = 0.0
    for v in x:
        s0 = v + coeff * s1 - s2
        s2 = s1
        s1 = s0
    return s1 * s1 + s2 * s2 - coeff * s1 * s2


def estimate(rr_ms: Iterable[float],
             *,
             min_intervals: int = MIN_INTERVALS,
             min_concentration: float = MIN_CONCENTRATION,
             min_prominence: float = MIN_PEAK_PROMINENCE) -> Optional[RespirationEstimate]:
    """Respiratory rate (breaths/min) from a window of RR intervals, or ``None``.

    ``None`` means "not measurable right now" -- too few beats, too short a span, or no
    convincing peak in the respiratory band. That is a normal outcome during movement or weak
    RSA, and callers must degrade rather than substitute a guess: a wrong respiratory rate fed
    to the onset detector is worse than an absent one, because ``respiration_slowed`` and
    ``respiration_regular`` would then fire on noise and manufacture false sleep onsets.
    """
    rr = _clean(rr_ms)
    if len(rr) < min_intervals:
        return None
    span_s = sum(rr) / 1000.0
    x = _resample(rr, _GRID_HZ)
    if len(x) < 16:
        return None

    mean = sum(x) / len(x)
    n = len(x)
    # Hann window: without it, spectral leakage from the large LF component smears across the
    # respiratory band and produces the band-edge artefact described in the module docstring.
    win = [(v - mean) * (0.5 - 0.5 * math.cos(2.0 * math.pi * i / (n - 1)))
           for i, v in enumerate(x)]

    # Evaluate the band on the natural DFT grid for this window length.
    df = _GRID_HZ / n
    lo_bin = max(1, int(RESP_BAND_LO_HZ / df))
    hi_bin = int(RESP_BAND_HI_HZ / df)
    if hi_bin <= lo_bin + 2:
        return None
    freqs = [b * df for b in range(lo_bin, hi_bin + 1)]
    powers = [_goertzel_power(win, f, _GRID_HZ) for f in freqs]
    total = sum(powers)
    if total <= 0:
        return None

    # Interior local maximum only. A peak at either edge is the leakage signature (LF bleeding
    # up through 0.15 Hz), not a breath rate -- rejecting it is what stopped this returning a
    # confident 9-10 brpm on a night whose real rate was 14.
    best_i = -1
    best_p = 0.0
    for i in range(1, len(powers) - 1):
        p = powers[i]
        if p > powers[i - 1] and p >= powers[i + 1] and p > best_p:
            best_i, best_p = i, p
    if best_i < 0:
        return None

    peak_hz = freqs[best_i]
    conc = sum(p for f, p in zip(freqs, powers) if abs(f - peak_hz) <= 0.03) / total
    if conc < min_concentration:
        return None
    ordered = sorted(powers)
    mid = len(ordered) // 2
    median_p = ordered[mid] if len(ordered) % 2 else 0.5 * (ordered[mid - 1] + ordered[mid])
    if median_p <= 0 or (best_p / median_p) < min_prominence:
        return None
    return RespirationEstimate(peak_hz * 60.0, conc, len(rr), span_s)
