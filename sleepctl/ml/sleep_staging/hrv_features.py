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
def nonlinear(ibis: Sequence[float]) -> Dict[str, float]:
    """Poincaré geometry + sample entropy.

    SD1/SD2 describe the shape of the successive-interval scatter: SD1 is short-term (vagal)
    variability, SD2 long-term. Their ratio shifts systematically across sleep stages, and it is
    computable from intervals but NOT from a smoothed HR series.
    """
    v = list(ibis)
    if len(v) < 4:
        return {}
    d = [b - a for a, b in zip(v, v[1:])]
    sd1 = _safe(lambda: statistics.pstdev(d) / math.sqrt(2.0))
    sdnn = _safe(lambda: statistics.pstdev(v))
    sd2 = _safe(lambda: math.sqrt(max(0.0, 2.0 * sdnn * sdnn - sd1 * sd1)))
    return {
        "ibi_sd1": sd1,
        "ibi_sd2": sd2,
        "ibi_sd1_sd2": (sd1 / sd2) if sd2 else 0.0,
        "ibi_ellipse_area": math.pi * sd1 * sd2,
        "ibi_sampen": _safe(lambda: sample_entropy(v)),
    }


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
    """Goertzel band power — O(n) per bin, no numpy (matches respiration.py's approach)."""
    n = len(sig)
    if n < 8:
        return 0.0
    mean = statistics.fmean(sig)
    x = [v - mean for v in sig]
    # Hann window: without it, spectral leakage smears the LF/HF split we care about
    x = [v * (0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1))) for i, v in enumerate(x)]
    total = 0.0
    step = fs / n
    k = max(1, int(lo / step))
    kmax = min(n // 2, int(hi / step) + 1)
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
        total += real * real + imag * imag
        k += 1
    return total


def frequency_domain(times_s: Sequence[float], ibis: Sequence[float]) -> Dict[str, float]:
    """LF/HF split. HF (0.15-0.40 Hz) is respiratory/parasympathetic and rises in deep sleep;
    LF/HF is a standard sympathovagal index that separates REM from NREM."""
    sig, fs = _tachogram(times_s, ibis)
    if not sig:
        return {}
    vlf = _band_power(sig, fs, *VLF_BAND)
    lf = _band_power(sig, fs, *LF_BAND)
    hf = _band_power(sig, fs, *HF_BAND)
    total = vlf + lf + hf
    if total <= 0:
        return {}
    return {
        "ibi_vlf": vlf, "ibi_lf": lf, "ibi_hf": hf, "ibi_total_power": total,
        "ibi_lf_hf": (lf / hf) if hf else 0.0,
        "ibi_lf_nu": lf / (lf + hf) if (lf + hf) else 0.0,
        "ibi_hf_nu": hf / (lf + hf) if (lf + hf) else 0.0,
        "ibi_vlf_frac": vlf / total,
    }


# ------------------------------------------------------------------ public entry point
def hrv_features(times_s: Sequence[float], ibis: Sequence[float],
                 clean: bool = True) -> Dict[str, float]:
    """All HRV features for one window of inter-beat intervals.

    ``times_s`` are the beat timestamps in seconds (same length as ``ibis``). Returns ``{}`` when
    the window is too short or too contaminated to characterise, so callers can treat "no
    features" as missing rather than as zeros.
    """
    if clean:
        times_s, ibis = _filter_ibis(times_s, ibis)
    if len(ibis) < 8:
        return {}
    feats: Dict[str, float] = {}
    feats.update(time_domain(ibis))
    feats.update(nonlinear(ibis))
    feats.update(frequency_domain(times_s, ibis))
    feats["ibi_n"] = float(len(ibis))
    return feats
