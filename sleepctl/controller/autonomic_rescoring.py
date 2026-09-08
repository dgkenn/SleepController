"""Beat-interval HRV per epoch, used to rescore REM versus deep sleep.

The bundled stager never saw beat intervals: its "HRV" feature is the variability of 2-second
heart-rate samples. Real beat-to-beat intervals carry the strongest heart-based separator of
REM from deep sleep in the published literature (Fonseca et al. 2017; Beattie et al. 2017;
Radha et al. 2019): deep sleep sits at HIGH vagal tone -- large RMSSD and high-frequency
power, low LF/HF -- while REM sits at high sympathetic tone -- small RMSSD, high LF/HF.

Standard definitions, self-normalised: each feature is a robust z-score against the night's
own running distribution (median / MAD), so absolute HRV level -- which varies several-fold
between people and with age -- never matters, only where tonight's epoch sits in tonight's
range. The rescoring is bounded: it may only move a LOW-confidence sleep label, and only when
the autonomic evidence is one robust standard deviation out on both features.
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from typing import List, Optional, Sequence, Tuple

WINDOW_S = 300.0
LF_BAND = (0.04, 0.15)
HF_BAND = (0.15, 0.40)
_GRID_HZ = 4.0
MIN_INTERVALS = 120       # ~2 min of beats inside the 5-min window
RR_MIN_MS, RR_MAX_MS = 300.0, 2000.0
MAX_SUCCESSIVE_RATIO = 0.3   # an interval this far from its neighbour is an artefact
MIN_HISTORY = 20             # epochs before z-scores mean anything
Z_STRONG = 1.0


def _clean(rr: Sequence[float]) -> List[float]:
    out: List[float] = []
    prev = None
    for v in rr:
        try:
            v = float(v)
        except Exception:
            continue
        if not (RR_MIN_MS <= v <= RR_MAX_MS):
            continue
        if prev is not None and abs(v - prev) / prev > MAX_SUCCESSIVE_RATIO:
            prev = v
            continue
        out.append(v)
        prev = v
    return out


def _resample(rr_ms: Sequence[float], grid_hz: float) -> List[float]:
    t, ts = 0.0, []
    for v in rr_ms:
        t += v / 1000.0
        ts.append(t)
    if len(ts) < 4:
        return []
    out, k, step = [], 0, 1.0 / grid_hz
    g = ts[0]
    while g <= ts[-1]:
        while k + 1 < len(ts) and ts[k + 1] < g:
            k += 1
        if k + 1 < len(ts):
            t0, t1 = ts[k], ts[k + 1]
            w = (g - t0) / (t1 - t0) if t1 > t0 else 0.0
            out.append(rr_ms[k] * (1 - w) + rr_ms[k + 1] * w)
        g += step
    return out


def _band_power(x: Sequence[float], fs: float, band: Tuple[float, float]) -> float:
    n = len(x)
    if n < 16:
        return 0.0
    mean = sum(x) / n
    win = [(v - mean) * (0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1))) for i, v in enumerate(x)]
    df = fs / n
    lo, hi = max(1, int(band[0] / df)), int(band[1] / df)
    total = 0.0
    for b in range(lo, hi + 1):
        f = b * df
        w = 2 * math.pi * f / fs
        c = 2 * math.cos(w)
        s0 = s1 = s2 = 0.0
        for v in win:
            s0 = v + c * s1 - s2
            s2, s1 = s1, s0
        total += s1 * s1 + s2 * s2 - c * s1 * s2
    return total


def epoch_hrv(rr_pairs: Sequence, t_end: float, window_s: float = WINDOW_S) -> Optional[dict]:
    """RMSSD (ms) and LF/HF over the beat intervals in ``(t_end - window_s, t_end]``.
    ``rr_pairs`` are ``(epoch_seconds, rr_ms)``. None when too sparse."""
    rr = [float(v) for t, v in rr_pairs if t_end - window_s < float(t) <= t_end]
    rr = _clean(rr)
    if len(rr) < MIN_INTERVALS:
        return None
    diffs = [b - a for a, b in zip(rr, rr[1:])]
    rmssd = math.sqrt(sum(d * d for d in diffs) / len(diffs))
    x = _resample(rr, _GRID_HZ)
    lf, hf = _band_power(x, _GRID_HZ, LF_BAND), _band_power(x, _GRID_HZ, HF_BAND)
    lfhf = (lf / hf) if hf > 1e-9 else None
    return {"rmssd": rmssd, "lf_hf": lfhf, "n": len(rr),
            "mean_hr": 60000.0 / (sum(rr) / len(rr))}


def _robust_z(v: float, hist: Sequence[float]) -> Optional[float]:
    if v is None or len(hist) < MIN_HISTORY:
        return None
    med = statistics.median(hist)
    mad = statistics.median([abs(h - med) for h in hist]) * 1.4826
    if mad <= 1e-9:
        return None
    return (v - med) / mad


class AutonomicRescorer:
    """Keeps the night's own HRV distribution and judges each epoch against it."""

    def __init__(self) -> None:
        self._rmssd: deque = deque(maxlen=900)
        self._lfhf: deque = deque(maxlen=900)
        self.last: Optional[dict] = None

    def reset(self) -> None:
        self._rmssd.clear()
        self._lfhf.clear()
        self.last = None

    def assess(self, rr_pairs: Sequence, t_end: float) -> Optional[dict]:
        f = epoch_hrv(rr_pairs, t_end)
        if f is None:
            self.last = None
            return None
        z_r = _robust_z(f["rmssd"], self._rmssd)
        z_l = _robust_z(math.log(f["lf_hf"]), self._lfhf) if f["lf_hf"] else None
        self._rmssd.append(f["rmssd"])
        if f["lf_hf"]:
            self._lfhf.append(math.log(f["lf_hf"]))
        suggest = None
        if z_r is not None and z_l is not None:
            if z_r >= Z_STRONG and z_l <= -Z_STRONG / 2:
                suggest = "deep"
            elif z_l >= Z_STRONG and z_r <= 0.0:
                suggest = "rem"
        self.last = {"rmssd": round(f["rmssd"], 1), "lf_hf": (round(f["lf_hf"], 2) if f["lf_hf"] else None),
                     "z_rmssd": (round(z_r, 2) if z_r is not None else None),
                     "z_lf_hf": (round(z_l, 2) if z_l is not None else None),
                     "n_beats": f["n"], "history": len(self._rmssd), "suggest": suggest}
        return self.last
