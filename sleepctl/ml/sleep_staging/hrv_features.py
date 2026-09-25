"""HRV features computed from RAW INTER-BEAT INTERVALS — pure standard library.

Why this exists. The deployed stager's features are summary statistics of a HEART-RATE time
series (``hr_mean_w2``, ``hr_std_w2``, ``hr_rmssd_w2``, …), inherited from the PhysioNet
sleep-accel corpus, which only ever had wrist HR. Its measured cross-validated performance is
4-class kappa 0.436 with **wake recall 0.413**.

Topalidis et al. (Sensors 2023;23(22):9077) reached kappa **0.75** on the *same* Polar Verity
Sense hardware, in the home, on a cohort that was 84.8% self-reported poor sleepers — using
inter-beat intervals ALONE, no accelerometer. The feature lineage traces to Radha et al.
(Sci Rep 2019), 132 HRV features over an LSTM.

The Verity streams PPI (true beat-to-beat intervals) and we already persist them in
``rr_intervals`` — 28,960 intervals on a single night, with 0.0% outside the physiological
300–2000 ms band and 0.1% beat-to-beat jumps over 20%, i.e. genuinely clean. But that signal is
currently consumed ONLY for RMSSD and RSA respiration; it never reaches the sleep stager. This
module is the missing extractor.

An IBI series carries information an HR series structurally cannot: HR as reported by the device
is already a smoothed, resampled summary, so beat-to-beat dispersion, the LF/HF split and the
non-linear geometry of successive intervals are all destroyed before we ever see it. Those are
exactly the features the autonomic sleep-staging literature relies on.

Deliberately dependency-free (json/math/statistics only), matching the constraint the rest of
``sleep_staging`` already honours so it can run inside the daemon.
"""

from __future__ import annotations

import math
import statistics
from typing import Dict, List, Optional, Sequence, Tuple

#: Physiological plausibility band for a single inter-beat interval, in milliseconds
#: (2000 ms = 30 bpm, 300 ms = 200 bpm). Anything outside is an artifact, not a heartbeat.
IBI_MIN_MS = 300.0
IBI_MAX_MS = 2000.0

#: Maximum plausible beat-to-beat change, as a fraction of the previous interval. Real sinus
#: rhythm does not jump 20% between consecutive beats outside of ectopy; a PPG that briefly loses
#: the pulse produces exactly such jumps by merging or splitting beats.
IBI_MAX_JUMP_FRAC = 0.20

#: Frequency bands (Hz), ESC/NASPE Task Force (1996).
VLF_BAND = (0.003, 0.04)
LF_BAND = (0.04, 0.15)
HF_BAND = (0.15, 0.40)

#: Resampling rate for the interpolated tachogram used by the spectral features.
_GRID_HZ = 4.0


#: How many recent kept intervals the median anchor is computed over. ~11 beats is ~10 s, over
#: which genuine physiological HR change is small -- so the anchor tracks the sleeper but not an
#: artifact run.
_ANCHOR_N = 11


def _filter_ibis(times_s, ibis):
    """Shared artifact filter. Returns ``(kept_times, kept_ibis)``.

    Two independent checks, because either alone is insufficient:

      * ADJACENT: reject a beat differing more than ``IBI_MAX_JUMP_FRAC`` from the last kept one.
        Catches the isolated merged/split beat a PPG produces when it loses the pulse.
      * MEDIAN ANCHOR: reject a beat differing more than ``IBI_MAX_JUMP_FRAC`` from the median of
        recent kept beats. Without this the adjacent check alone is walkable -- a run of steps
        each individually inside tolerance drags the reference anywhere. Caught by test:
        six successive +15% steps carried the filter from 880 ms to 1770 ms (68 -> 34 bpm) with
        every single step "valid".
    """
    kept_t, kept = [], []
    for t, x in zip(times_s, ibis):
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        if not (IBI_MIN_MS <= v <= IBI_MAX_MS):
            continue
        if kept:
            if abs(v - kept[-1]) / kept[-1] > IBI_MAX_JUMP_FRAC:
                continue
            anchor = statistics.median(kept[-_ANCHOR_N:])
            if abs(v - anchor) / anchor > IBI_MAX_JUMP_FRAC:
                continue
        kept.append(v)
        kept_t.append(float(t))
    return kept_t, kept


def clean_ibis(ibis):
    """Drop physiologically impossible intervals and artifact jumps (see :func:`_filter_ibis`)."""
    return _filter_ibis(range(len(ibis)), ibis)[1]


def _safe(fn, default=0.0):
    try:
        v = fn()
        return default if v is None or math.isnan(v) or math.isinf(v) else float(v)
    except Exception:
        return default


# ------------------------------------------------------------------ time domain
def time_domain(ibis: Sequence[float]) -> Dict[str, float]:
    """Classic time-domain HRV. ``pnn50``/``pnn20`` are the fraction of successive differences
    exceeding 50/20 ms — the parasympathetic markers that rise with sleep depth."""
    v = list(ibis)
    if len(v) < 3:
        return {}
    d = [b - a for a, b in zip(v, v[1:])]
    ad = [abs(x) for x in d]
    mean = statistics.fmean(v)
    out = {
        "ibi_mean": mean,
        "ibi_median": statistics.median(v),
        "ibi_sdnn": _safe(lambda: statistics.pstdev(v)),
        "ibi_rmssd": _safe(lambda: math.sqrt(statistics.fmean([x * x for x in d]))),
        "ibi_sdsd": _safe(lambda: statistics.pstdev(d)) if len(d) > 1 else 0.0,
        "ibi_min": min(v),
        "ibi_max": max(v),
        "ibi_range": max(v) - min(v),
        "ibi_iqr": _safe(lambda: _pct(sorted(v), 0.75) - _pct(sorted(v), 0.25)),
        "ibi_pnn50": sum(1 for x in ad if x > 50.0) / len(ad),
        "ibi_pnn20": sum(1 for x in ad if x > 20.0) / len(ad),
        "hr_from_ibi": 60000.0 / mean if mean else 0.0,
    }
    out["ibi_cvnn"] = out["ibi_sdnn"] / mean if mean else 0.0
    out["ibi_skew"] = _safe(lambda: _skew(v))
    out["ibi_kurtosis"] = _safe(lambda: _kurtosis(v))
    return out


def _pct(sorted_v: Sequence[float], p: float) -> float:
    if not sorted_v:
        return 0.0
    return sorted_v[min(len(sorted_v) - 1, max(0, int(len(sorted_v) * p)))]


def _skew(v: Sequence[float]) -> float:
    m = statistics.fmean(v)
    sd = statistics.pstdev(v)
    if sd == 0:
        return 0.0
    return statistics.fmean([((x - m) / sd) ** 3 for x in v])


def _kurtosis(v: Sequence[float]) -> float:
    m = statistics.fmean(v)
    sd = statistics.pstdev(v)
    if sd == 0:
        return 0.0
    return statistics.fmean([((x - m) / sd) ** 4 for x in v]) - 3.0


# ------------------------------------------------------------------ non-linear
def nonlinear(ibis: Sequence[float], sampen: bool = True) -> Dict[str, float]:
    """Poincaré geometry + sample entropy.

    SD1/SD2 describe the shape of the successive-interval scatter: SD1 is short-term (vagal)
    variability, SD2 long-term. Their ratio shifts systematically across sleep stages, and it is
    computable from intervals but NOT from a smoothed HR series.

    ``sampen=False`` skips :func:`sample_entropy`, which is O(n^2) and dominates the cost of a
    window longer than a couple of minutes (the stager's multi-scale block only asks for it on
    its shortest window).
    """
    v = list(ibis)
    if len(v) < 4:
        return {}
    d = [b - a for a, b in zip(v, v[1:])]
    sd1 = _safe(lambda: statistics.pstdev(d) / math.sqrt(2.0))
    sdnn = _safe(lambda: statistics.pstdev(v))
    sd2 = _safe(lambda: math.sqrt(max(0.0, 2.0 * sdnn * sdnn - sd1 * sd1)))
    out = {
        "ibi_sd1": sd1,
        "ibi_sd2": sd2,
        "ibi_sd1_sd2": (sd1 / sd2) if sd2 else 0.0,
        "ibi_ellipse_area": math.pi * sd1 * sd2,
    }
    if sampen:
        out["ibi_sampen"] = _safe(lambda: sample_entropy(v))
    return out


def sample_entropy(v: Sequence[float], m: int = 2, r_frac: float = 0.2) -> Optional[float]:
    """Sample entropy — regularity of the interval series. Lower = more regular.

    O(n^2), so callers should pass an epoch-sized window, not a whole night.
    """
    n = len(v)
    if n < m + 2:
        return None
    r = r_frac * statistics.pstdev(v)
    if r <= 0:
        return None

    def _count(mm: int) -> int:
        tmpl = [v[i:i + mm] for i in range(n - mm)]
        c = 0
        for i in range(len(tmpl)):
            for j in range(i + 1, len(tmpl)):
                if max(abs(a - b) for a, b in zip(tmpl[i], tmpl[j])) <= r:
                    c += 1
        return c

    a, b = _count(m + 1), _count(m)
    if a == 0 or b == 0:
        return None
    return -math.log(a / b)


# ------------------------------------------------------------------ frequency domain
def _tachogram(times_s: Sequence[float], ibis: Sequence[float]) -> Tuple[List[float], float]:
    """Uniformly resampled tachogram (linear interpolation) for spectral estimation."""
    if len(ibis) < 4:
        return [], _GRID_HZ
    t = list(times_s)
    span = t[-1] - t[0]
    if span <= 0:
        return [], _GRID_HZ
    n = int(span * _GRID_HZ)
    if n < 8:
        return [], _GRID_HZ
    out, j = [], 0
    for k in range(n):
        x = t[0] + k / _GRID_HZ
        while j + 1 < len(t) - 1 and t[j + 1] < x:
            j += 1
        t0, t1 = t[j], t[j + 1]
        y0, y1 = ibis[j], ibis[j + 1]
        out.append(y0 if t1 == t0 else y0 + (y1 - y0) * (x - t0) / (t1 - t0))
    return out, _GRID_HZ


def _band_power(sig: Sequence[float], fs: float, lo: float, hi: float) -> float:
    """Goertzel band power in ms^2 over the half-open band [lo, hi) -- O(n) per bin, no numpy.

    Normalised as a one-sided Hann-windowed periodogram (2*sum|X_k|^2 / (n * sum w^2)), so the
    value is physical power and does not grow with window length. The raw sum of |X_k|^2 it
    used to return scaled with n^2 (the same 450 ms^2 oscillation read 1.3e7 over 2 min and
    8.0e7 over 5 min), which only ratios survived. Half-open bands keep the 0.15 Hz bin out of
    LF and HF at once."""
    return _band_spectrum(sig, fs, lo, hi)[0]


def _band_spectrum(sig: Sequence[float], fs: float, lo: float,
                   hi: float) -> Tuple[float, List[float], List[float]]:
    """:func:`_band_power` plus the per-bin frequencies and raw powers it summed, so the
    respiratory peak can be read off the HF band at no extra Goertzel cost."""
    n = len(sig)
    if n < 8:
        return 0.0, [], []
    mean = statistics.fmean(sig)
    x = [v - mean for v in sig]
    # Hann window: without it, spectral leakage smears the LF/HF split we care about
    w_hann = [0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) for i in range(n)]
    x = [v * w for v, w in zip(x, w_hann)]
    norm = n * sum(w * w for w in w_hann)
    if norm <= 0:
        return 0.0, [], []
    total = 0.0
    freqs: List[float] = []
    powers: List[float] = []
    step = fs / n
    k = max(1, int(math.ceil(lo / step - 1e-9)))
    kmax = min(n // 2, int(math.ceil(hi / step - 1e-9)) - 1)
    while k <= kmax:
        w = 2.0 * math.pi * k / n
        cw, sw = math.cos(w), math.sin(w)
        coeff = 2.0 * cw
        s0 = s1 = s2 = 0.0
        for v in x:
            s0 = v + coeff * s1 - s2
            s2, s1 = s1, s0
        real = s1 - s2 * cw
        imag = s2 * sw
        p = real * real + imag * imag
        total += p
        freqs.append(k * step)
        powers.append(p)
        k += 1
    return 2.0 * total / norm, freqs, powers


def frequency_domain(times_s: Sequence[float], ibis: Sequence[float]) -> Dict[str, float]:
    """LF/HF split. HF (0.15-0.40 Hz) is respiratory/parasympathetic and rises in deep sleep;
    LF/HF is a standard sympathovagal index that separates REM from NREM."""
    sig, fs = _tachogram(times_s, ibis)
    if not sig:
        return {}
    vlf = _band_power(sig, fs, *VLF_BAND)
    lf = _band_power(sig, fs, *LF_BAND)
    hf, hf_freqs, hf_powers = _band_spectrum(sig, fs, *HF_BAND)
    total = vlf + lf + hf
    if total <= 0:
        return {}
    out = {
        "ibi_vlf": vlf, "ibi_lf": lf, "ibi_hf": hf, "ibi_total_power": total,
        "ibi_lf_hf": (lf / hf) if hf else 0.0,
        "ibi_lf_nu": lf / (lf + hf) if (lf + hf) else 0.0,
        "ibi_hf_nu": hf / (lf + hf) if (lf + hf) else 0.0,
        "ibi_vlf_frac": vlf / total,
    }
    peak = rsa_peak(hf_freqs, hf_powers)
    if peak is not None:
        out["ibi_resp_rate"] = 60.0 * peak[0]
        out["ibi_resp_conc"] = peak[1]
    return out


# ------------------------------------------------------------------ respiration (RSA)
# Breathing modulates the heart period (respiratory sinus arrhythmia), so the respiratory rhythm
# rides on the tachogram: the IBI lengthens on expiration and shortens on inspiration. Deep (N3)
# sleep breathes slowly and very regularly, REM irregularly (Douglas et al., Thorax 1982;
# Redmond & Heneghan, IEEE TBME 2006), which is what makes the breathing pattern a deep-vs-REM
# discriminator HR summaries do not carry. Two complementary estimators:
#
#   * SPECTRAL: the RSA peak of the HF band (the band's own Goertzel bins, so free) -> the
#     dominant breathing rate, plus how concentrated the band power is around it (a narrow peak
#     is steady breathing, a smeared one is not). Same concentration definition and interior-peak
#     rule as ``sleepctl.controller.respiration``, which was validated on this user's nights.
#   * CYCLE COUNTING: the "advanced counting" method of Schaefer & Kratky (Ann Biomed Eng 2008;
#     36:476) -- band-pass the tachogram, take its extrema, discard swings smaller than 0.3x the
#     upper quartile -- yields individual breaths, hence breath-to-breath variability (CV of
#     cycle durations) and the peak-to-trough RSA amplitude per breath (the band-passed analogue
#     of Grossman's peak-valley RSA, Psychophysiology 1990), neither of which a single spectrum
#     can give.

#: half-width of the "near the peak" band the concentration is measured over (Hz)
RESP_CONC_HALFWIDTH_HZ = 0.03
#: tachogram rate for cycle counting: 5+ samples per cycle at the 0.40 Hz band top is ample
#: once extrema are refined parabolically, and half the 4 Hz grid's filtering cost
_RESP_GRID_HZ = 2.0
#: band-pass corners (2nd-order Butterworth high- and low-pass, run forward-backward). The
#: pair passes 0.79-0.86 of the amplitude over 0.2-0.3 Hz (12-18 breaths/min) and 0.21 of a
#: 0.1 Hz Mayer wave. A 0.12 Hz corner let a 60 ms Mayer wave push a regular breath's cycle
#: CV from 0.04 to 0.18 -- as "irregular" as a 15%-jittered rate -- on synthetic tachograms.
_RESP_HP_HZ = 0.14
_RESP_LP_HZ = 0.50
#: a beat-time gap longer than this is not interpolated across; the tachogram is split there
#: and no cycle may straddle it. An ectopic beat and its compensatory pause, both rejected by
#: :func:`_filter_ibis`, leave ~3 s at 60 bpm -- most of a breath, and bridging it doubled the
#: cycle CV of a regular breather at 1-3% ectopy (synthetic; 3.0 s let it through, 2.5 s not).
RESP_GAP_S = 2.5
#: shortest contiguous stretch worth filtering (a few breaths)
_RESP_MIN_SEG_S = 20.0
#: odd-reflection padding at each segment end, so the filter's start-up transient falls outside
_RESP_PAD_S = 15.0
#: extremum pairs closer than this fraction of the upper-quartile swing are noise, not breaths
RESP_SWING_FRAC = 0.3
#: plausible breath-cycle durations (s): 5-30 breaths/min
RESP_CYCLE_S = (2.0, 12.0)
#: fewest cycles for a window's breathing summary
RESP_MIN_CYCLES = 4


def rsa_peak(freqs: Sequence[float], powers: Sequence[float]
             ) -> Optional[Tuple[float, float]]:
    """``(peak_hz, concentration)`` of the respiratory peak in an HF-band spectrum, or None.

    The peak must be the largest INTERIOR local maximum: a maximum pinned to the band edge is
    LF (Mayer-wave) leakage, not breathing. Its frequency is refined by a parabola through the
    log-power of the three bins around it (a Hann main lobe is near-Gaussian, so this is
    accurate to a small fraction of a bin). Concentration is the share of band power within
    :data:`RESP_CONC_HALFWIDTH_HZ` of the peak.
    """
    n = len(powers)
    if n < 3:
        return None
    total = 0.0
    for p in powers:
        total += p
    if total <= 0:
        return None
    best, best_p = -1, 0.0
    for i in range(1, n - 1):
        p = powers[i]
        if p > powers[i - 1] and p >= powers[i + 1] and p > best_p:
            best, best_p = i, p
    if best < 0:
        return None
    f = freqs[best]
    a, b, c = powers[best - 1], powers[best], powers[best + 1]
    if a > 0 and c > 0:
        la, lb, lc = math.log(a), math.log(b), math.log(c)
        den = la - 2.0 * lb + lc
        if den < 0:
            f += 0.5 * (la - lc) / den * (freqs[best + 1] - freqs[best])
    near = 0.0
    for fk, p in zip(freqs, powers):
        if abs(fk - f) <= RESP_CONC_HALFWIDTH_HZ:
            near += p
    return f, near / total


def _biquad(kind: str, fc: float, fs: float) -> Tuple[float, float, float, float, float]:
    """2nd-order Butterworth (Q = 1/sqrt 2) section via the bilinear transform (RBJ cookbook),
    normalised to a0 = 1: ``(b0, b1, b2, a1, a2)``."""
    w0 = 2.0 * math.pi * fc / fs
    cw = math.cos(w0)
    alpha = math.sin(w0) / math.sqrt(2.0)
    a0 = 1.0 + alpha
    if kind == "hp":
        b0, b1 = (1.0 + cw) / 2.0, -(1.0 + cw)
    else:
        b0, b1 = (1.0 - cw) / 2.0, 1.0 - cw
    return b0 / a0, b1 / a0, b0 / a0, -2.0 * cw / a0, (1.0 - alpha) / a0


_RESP_SECTIONS = (_biquad("hp", _RESP_HP_HZ, _RESP_GRID_HZ),
                  _biquad("lp", _RESP_LP_HZ, _RESP_GRID_HZ))


def _sosfilt(x: List[float]) -> List[float]:
    """Both band-pass sections in one pass (transposed direct form II, zero initial state)."""
    (b0, b1, b2, a1, a2), (c0, c1, c2, d1, d2) = _RESP_SECTIONS
    z1 = z2 = u1 = u2 = 0.0
    out = []
    for v in x:
        y = b0 * v + z1
        z1 = b1 * v - a1 * y + z2
        z2 = b2 * v - a2 * y
        o = c0 * y + u1
        u1 = c1 * y - d1 * o + u2
        u2 = c2 * y - d2 * o
        out.append(o)
    return out


def _segment_extrema(ts: Sequence[float], vs: Sequence[float]
                     ) -> List[Tuple[float, float, bool]]:
    """Alternating raw extrema ``(t, value, is_max)`` of the band-passed tachogram of one
    gap-free run of beats."""
    t0 = ts[0]
    n = int((ts[-1] - t0) * _RESP_GRID_HZ) + 1
    sig, j = [], 0
    for k in range(n):
        x = t0 + k / _RESP_GRID_HZ
        while j + 2 < len(ts) and ts[j + 1] < x:
            j += 1
        ta, tb = ts[j], ts[j + 1]
        sig.append(vs[j] if tb == ta else vs[j] + (vs[j + 1] - vs[j]) * (x - ta) / (tb - ta))
    mean = statistics.fmean(sig)
    sig = [v - mean for v in sig]
    pad = min(n - 1, int(_RESP_PAD_S * _RESP_GRID_HZ))
    # odd reflection about each end keeps level and slope continuous, as scipy's filtfilt does
    head = [2.0 * sig[0] - sig[i] for i in range(pad, 0, -1)]
    tail = [2.0 * sig[-1] - sig[-1 - i] for i in range(1, pad + 1)]
    y = _sosfilt(head + sig + tail)
    y = _sosfilt(y[::-1])[::-1][pad:pad + n]    # forward-backward: zero phase, so no lag
    out: List[Tuple[float, float, bool]] = []
    for k in range(1, n - 1):
        a, b, c = y[k - 1], y[k], y[k + 1]
        is_max = b > a and b >= c
        if not (is_max or (b < a and b <= c)):
            continue
        den = a - 2.0 * b + c
        d = 0.5 * (a - c) / den if den else 0.0
        out.append((t0 + (k + d) / _RESP_GRID_HZ, b - 0.25 * (a - c) * d, is_max))
    return out


def breath_cycles(times_s: Sequence[float], ibis: Sequence[float]
                  ) -> List[Tuple[float, float, float]]:
    """Individual breaths from an (already cleaned) beat-interval series, by advanced counting.

    Returns ``(t_end, duration_s, swing_ms)`` per cycle: every peak-to-peak AND trough-to-trough
    span between confirmed extrema, with the peak-to-trough swing that closes it. The series is
    split at beat gaps over :data:`RESP_GAP_S` (a dropout or rejected ectopic run) and each run
    filtered on its own, so no cycle is ever interpolated across missing data.
    """
    n = len(ibis)
    if n < 8:
        return []
    segs: List[List[Tuple[float, float, bool]]] = []
    i0 = 0
    for i in range(1, n + 1):
        if i == n or times_s[i] - times_s[i - 1] > RESP_GAP_S:
            if times_s[i - 1] - times_s[i0] >= _RESP_MIN_SEG_S and i - i0 >= 8:
                segs.append(_segment_extrema(times_s[i0:i], ibis[i0:i]))
            i0 = i
    swings = sorted(abs(s[k][1] - s[k - 1][1]) for s in segs for k in range(1, len(s)))
    if len(swings) < 4:
        return []
    h = RESP_SWING_FRAC * swings[(3 * len(swings)) // 4]
    lo_s, hi_s = RESP_CYCLE_S
    cycles: List[Tuple[float, float, float]] = []
    for ext in segs:
        # zig-zag: an extremum is confirmed once the signal has reversed from it by >= h; a
        # smaller wiggle is dropped and a further same-direction extremum extends the candidate
        conf: List[Tuple[float, float, bool]] = []
        cand = None
        for e in ext:
            if cand is None or e[2] == cand[2]:
                if cand is None or (e[1] > cand[1] if e[2] else e[1] < cand[1]):
                    cand = e
            elif abs(e[1] - cand[1]) >= h:
                conf.append(cand)
                cand = e
        for k in range(2, len(conf)):
            dur = conf[k][0] - conf[k - 2][0]
            if lo_s <= dur <= hi_s:
                cycles.append((conf[k][0], dur, abs(conf[k][1] - conf[k - 1][1])))
    cycles.sort()
    return cycles


def breath_summary(cycles: Sequence[Tuple[float, float, float]], start: float,
                   stop: float) -> Dict[str, float]:
    """Breathing features over the cycles that lie wholly inside ``[start, stop]``.

    ``ibi_breath_rate`` breaths/min from the median cycle; ``ibi_breath_cv`` the coefficient of
    variation of cycle duration (breath-to-breath irregularity); ``ibi_rsa_amp`` the median
    peak-to-trough swing (ms) and ``ibi_rsa_amp_cv`` its variability; ``ibi_breath_cov`` the
    fraction of the window spanned by counted breaths (each breath appears twice, peak-to-peak
    and trough-to-trough, hence the halving). ``{}`` below :data:`RESP_MIN_CYCLES`.
    """
    durs, amps = [], []
    for t_end, dur, amp in cycles:
        if t_end - dur >= start and t_end <= stop:
            durs.append(dur)
            amps.append(amp)
    if len(durs) < RESP_MIN_CYCLES or stop <= start:
        return {}
    return {
        "ibi_breath_rate": 60.0 / statistics.median(durs),
        "ibi_breath_cv": _cv(durs),
        "ibi_rsa_amp": statistics.median(amps),
        "ibi_rsa_amp_cv": _cv(amps),
        "ibi_breath_cov": min(1.0, 0.5 * sum(durs) / (stop - start)),
    }


def _cv(v: Sequence[float]) -> float:
    """Population coefficient of variation in plain float arithmetic (``statistics.pstdev``
    is exact-rational and ~20x slower, which matters at 20 epochs per daemon tick)."""
    m = math.fsum(v) / len(v)
    if m <= 0:
        return 0.0
    return math.sqrt(max(0.0, math.fsum((x - m) * (x - m) for x in v) / len(v))) / m


def respiration(times_s: Sequence[float], ibis: Sequence[float]) -> Dict[str, float]:
    """Cycle-counting breathing features for one window of cleaned beat intervals."""
    if len(ibis) < 8:
        return {}
    return breath_summary(breath_cycles(times_s, ibis), times_s[0], times_s[-1])


# ------------------------------------------------------------------ public entry point
def hrv_features(times_s: Sequence[float], ibis: Sequence[float],
                 clean: bool = True, *, sampen: bool = True,
                 spectral: bool = True, breathing: bool = True) -> Dict[str, float]:
    """All HRV features for one window of inter-beat intervals.

    ``times_s`` are the beat timestamps in seconds (same length as ``ibis``). Returns ``{}`` when
    the window is too short or too contaminated to characterise, so callers can treat "no
    features" as missing rather than as zeros.

    ``sampen`` / ``spectral`` switch off the two super-linear blocks (sample entropy is O(n^2),
    the Goertzel band powers O(n * bins) with bins growing with n) for callers that score long
    windows many times per tick; ``breathing`` the cycle-counting block (:func:`respiration`),
    for callers that count breaths once over a longer span and slice it themselves. The spectral
    respiratory peak (``ibi_resp_rate`` / ``ibi_resp_conc``) comes with ``spectral``. The
    default computes everything.
    """
    if clean:
        times_s, ibis = _filter_ibis(times_s, ibis)
    if len(ibis) < 8:
        return {}
    feats: Dict[str, float] = {}
    feats.update(time_domain(ibis))
    feats.update(nonlinear(ibis, sampen=sampen))
    if spectral:
        feats.update(frequency_domain(times_s, ibis))
    if breathing:
        feats.update(respiration(times_s, ibis))
    feats["ibi_n"] = float(len(ibis))
    return feats
