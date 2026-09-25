#!/usr/bin/env python3
"""Reduce NSRR MESA Sleep polysomnography (EDF + XML) to the staging trainer's inputs.

MESA Sleep (https://sleepdata.org/datasets/mesa) is 2,056 in-home overnight PSGs (Compumedics
Somte) from the Multi-Ethnic Study of Atherosclerosis, scored by the Brigham sleep reading
centre, with a finger pulse-oximeter plethysmogram (``Pleth``, 256 Hz) in every EDF and a
wrist Actiwatch worn the same week. It is the standard corpus for PPG-based staging. The
NSRR layout this reads (paths relative to the dataset root):

    polysomnography/edfs/mesa-sleep-NNNN.edf                         signals (~190 MB each)
    polysomnography/annotations-events-nsrr/mesa-sleep-NNNN-nsrr.xml  staging, NSRR XML
    polysomnography/annotations-events-profusion/mesa-sleep-NNNN-profusion.xml   (fallback)
    polysomnography/annotations-rpoints/mesa-sleep-NNNN-rpoint.csv   ECG R-points (optional)
    actigraphy/mesa-sleep-NNNN.csv                                   30 s Actiwatch counts
    overlap/mesa-actigraphy-psg-overlap.csv                          actigraphy line at PSG start

Per record it writes, in the SAME text formats ``scripts/dreamt_reduce.py`` writes (so the
trainer and ``sleepctl.ml.sleep_staging.dataset`` read them unchanged):

    <ID>_heartrate.txt          t_seconds,bpm            1 Hz, from the clean beat intervals
    <ID>_labeled_sleep.txt      t_seconds stage          30 s epochs; -1 unscored, 0 wake,
                                                         1 N1, 2 N2, 3 N3 (and N4), 5 REM
    <ID>_ibi.txt                t_seconds,ibi_ms         pleth beat time and interval
    activity/<ID>_activity.txt  epoch_start_s,pim,zcm,mad,std,pmax,n
                                                         Actiwatch counts in pim (and pmax),
                                                         n = 1; only the trainer's SCALE-FREE
                                                         motion features use them

Beats come from the plethysmogram: an Elgendi-style systolic-peak detector (two moving
averages over the clipped, squared band-passed wave) run in 5-minute chunks with overlap, so
a night never sits in memory whole; then artifact rejection -- flat, railed or wildly
out-of-scale 2 s windows, beats whose shape does not match the segment's median pulse
(template correlation < 0.8), beats far off the local amplitude, intervals outside
300-2000 ms, and intervals more than 25 % off the median of their neighbours (missed and
ectopic beats).
An interval is only emitted between two ACCEPTED neighbouring beats, never across a gap.

Pure Python + numpy: a minimal EDF reader is included (no MNE / pyedflib).

Usage (manual; the automatic route is scripts/mesa_pipeline.py):
    python3 scripts/mesa_reduce.py --data-dir D:/nsrr/mesa --out D:/mesa/reduced
    python3 scripts/mesa_reduce.py --out D:/mesa/reduced --verify

MESA is released under an NSRR data use agreement: the raw files and everything reduced from
them stay OUT of the repository. Only trained weight files may ever be committed.
"""
from __future__ import annotations

import argparse
import csv
import glob
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

EPOCH_S = 30.0
#: EDF signal labels tried for the finger plethysmogram, in order (case-insensitive).
PLETH_LABELS = ("Pleth", "PLETH", "Plethysmogram", "PPG", "Pulse", "PlethWV")
#: NSRR XML EventConcept -> the reduced stage code (N4 folds into N3, R&K -> AASM).
NSRR_STAGES = {"wake|0": 0, "stage 1 sleep|1": 1, "stage 2 sleep|2": 2, "stage 3 sleep|3": 3,
               "stage 4 sleep|4": 3, "rem sleep|5": 5, "unscored|9": -1, "movement|6": -1}
#: Profusion XML <SleepStage> integers -> the reduced stage code.
PROFUSION_STAGES = {0: 0, 1: 1, 2: 2, 3: 3, 4: 3, 5: 5}

IBI_MIN_MS, IBI_MAX_MS = 300.0, 2000.0
#: An interval further than this fraction from its neighbours' median is a missed/extra beat.
IBI_LOCAL_TOL = 0.25
IBI_NEIGHBOURS = 5
#: Beat amplitude vs the local median: below LOW it is not a beat (dicrotic wave, noise) and is
#: dropped; above HIGH it is a movement artifact and both its intervals are discarded.
AMP_LOW, AMP_HIGH = 0.35, 3.5
QUALITY_WIN_S = 2.0
#: A 2 s window whose band amplitude exceeds this multiple of the segment's median is movement.
WINDOW_PTP_HIGH = 2.5
#: A beat whose shape correlates less than this with the segment's median pulse is rejected.
TEMPLATE_MIN_CORR = 0.8
CHUNK_S, PAD_S = 300.0, 8.0
#: A record with fewer clean intervals than this is not worth training on.
MIN_IBIS = 300
MIN_EPOCHS = 60


class LayoutError(ValueError):
    """The file is not what the documented MESA layout says (permanent: retrying won't help)."""


# ---------------------------------------------------------------------------- EDF reader
_EDF_FIELDS = (("label", 16), ("transducer", 80), ("dimension", 8), ("phys_min", 8),
               ("phys_max", 8), ("dig_min", 8), ("dig_max", 8), ("prefilter", 80),
               ("spr", 8), ("reserved", 32))


class EdfReader:
    """A minimal EDF / EDF+C reader: header, then any signal's samples by range.

    Samples are read record by record straight from the file, so a caller can walk a 10-hour
    256 Hz channel in chunks without the night (or the other channels) ever in memory."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = open(path, "rb")
        try:
            self._parse_header()
        except Exception:
            self._fh.close()
            raise

    def _parse_header(self) -> None:
        head = self._fh.read(256)
        if len(head) < 256:
            raise LayoutError("file is shorter than an EDF header")
        if head[:1] == b"\xff":
            raise LayoutError("BDF (24-bit) files are not supported")
        try:
            ascii_head = head.decode("ascii")
            self.header_bytes = int(ascii_head[184:192])
            declared = int(ascii_head[236:244])
            self.record_duration = float(ascii_head[244:252])
            ns = int(ascii_head[252:256])
        except (UnicodeDecodeError, ValueError) as exc:
            raise LayoutError(f"not an EDF header ({exc.__class__.__name__})") from None
        self.edf_plus = ascii_head[192:236].strip()        # "", "EDF+C" or "EDF+D"
        if ns <= 0 or self.header_bytes != 256 * (ns + 1):
            raise LayoutError("EDF header size does not match its signal count")
        if self.record_duration <= 0:
            raise LayoutError("EDF record duration is not positive")
        raw = self._fh.read(256 * ns)
        if len(raw) < 256 * ns:
            raise LayoutError("EDF signal header is truncated")
        text = raw.decode("latin-1")
        cols: Dict[str, List[str]] = {}
        pos = 0
        for name, width in _EDF_FIELDS:
            cols[name] = [text[pos + i * width: pos + (i + 1) * width].strip() for i in range(ns)]
            pos += width * ns
        self.labels = cols["label"]
        self.dimensions = cols["dimension"]
        try:
            self.spr = [int(v) for v in cols["spr"]]
            self.phys_min = [float(v) for v in cols["phys_min"]]
            self.phys_max = [float(v) for v in cols["phys_max"]]
            self.dig_min = [int(float(v)) for v in cols["dig_min"]]
            self.dig_max = [int(float(v)) for v in cols["dig_max"]]
        except ValueError as exc:
            raise LayoutError(f"unreadable EDF signal header ({exc})") from None
        self.record_samples = sum(self.spr)
        self.record_bytes = 2 * self.record_samples
        self.offsets = [sum(self.spr[:i]) for i in range(ns)]
        size = os.fstat(self._fh.fileno()).st_size
        present = max(0, (size - self.header_bytes) // self.record_bytes)
        # -1 means "unknown" (recording still open); a truncated file is read as far as it goes
        self.n_records = present if declared < 0 else min(declared, present)
        self.truncated = declared >= 0 and present < declared
        self.start: Optional[datetime] = None
        try:
            d, t = ascii_head[168:176], ascii_head[176:184]
            dd, mm, yy = (int(p) for p in d.split("."))
            hh, mi, ss = (int(p) for p in t.split("."))
            self.start = datetime(1900 + yy if yy >= 85 else 2000 + yy, mm, dd, hh, mi, ss)
        except Exception:
            pass                                    # anonymised start dates are allowed

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "EdfReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def duration_s(self) -> float:
        return self.n_records * self.record_duration

    def fs(self, idx: int) -> float:
        return self.spr[idx] / self.record_duration

    def n_samples(self, idx: int) -> int:
        return self.n_records * self.spr[idx]

    def find_signal(self, names: Sequence[str]) -> Optional[int]:
        """Index of the first label matching ``names`` exactly (case-insensitive), else the
        first label starting with one of them; None if absent."""
        low = [lb.lower() for lb in self.labels]
        for n in names:
            if n.lower() in low:
                return low.index(n.lower())
        for n in names:
            for i, lb in enumerate(low):
                if lb.startswith(n.lower()):
                    return i
        return None

    def _gain(self, idx: int) -> Tuple[float, float]:
        dspan = self.dig_max[idx] - self.dig_min[idx]
        pspan = self.phys_max[idx] - self.phys_min[idx]
        gain = pspan / dspan if dspan else 1.0
        return gain, self.phys_min[idx] - self.dig_min[idx] * gain

    def read(self, idx: int, start: int = 0, stop: Optional[int] = None) -> np.ndarray:
        """Physical samples ``[start, stop)`` of signal ``idx`` as float64."""
        n = self.n_samples(idx)
        stop = n if stop is None else min(int(stop), n)
        start = max(0, int(start))
        if stop <= start:
            return np.zeros(0)
        spr = self.spr[idx]
        r0, r1 = start // spr, -(-stop // spr)
        self._fh.seek(self.header_bytes + r0 * self.record_bytes)
        buf = self._fh.read((r1 - r0) * self.record_bytes)
        nrec = len(buf) // self.record_bytes
        if nrec <= 0:
            return np.zeros(0)
        rec = np.frombuffer(buf[: nrec * self.record_bytes], dtype="<i2")
        rec = rec.reshape(nrec, self.record_samples)
        off = self.offsets[idx]
        dig = rec[:, off: off + spr].reshape(-1)[start - r0 * spr: stop - r0 * spr]
        gain, offset = self._gain(idx)
        return dig.astype(np.float64) * gain + offset

    def rails(self, idx: int) -> Tuple[float, float]:
        """The physical values of the digital minimum and maximum (a railed sensor sits there)."""
        gain, offset = self._gain(idx)
        return self.dig_min[idx] * gain + offset, self.dig_max[idx] * gain + offset


def write_edf(path: str, signals: Sequence[Tuple[str, float, np.ndarray]], record_s: float = 1.0,
              start: Optional[datetime] = None, phys: Optional[Sequence[Tuple[float, float]]] = None
              ) -> None:
    """Write a small plain EDF (used by the tests' synthetic fixtures). ``signals`` are
    (label, fs, samples); each is scaled to 16-bit over its own range (or ``phys``)."""
    start = start or datetime(2010, 1, 1, 22, 0, 0)
    ns = len(signals)
    sprs = [int(round(fs * record_s)) for _l, fs, _x in signals]
    n_rec = min(len(x) // s for (_l, _f, x), s in zip(signals, sprs))
    ranges = []
    for k, (_l, _f, x) in enumerate(signals):
        lo, hi = (phys[k] if phys else (float(np.min(x)), float(np.max(x))))
        ranges.append((lo, hi if hi > lo else lo + 1.0))

    def f(v, w):
        s = str(v)
        if len(s) > w:
            s = (f"{v:.{max(0, w - 6)}g}" if isinstance(v, float) else s)[:w]
        return s.ljust(w).encode("ascii")

    hdr = b"".join([f("0", 8), f("X X X X", 80), f("Startdate X X X X", 80),
                    f(start.strftime("%d.%m.%y"), 8), f(start.strftime("%H.%M.%S"), 8),
                    f(256 * (ns + 1), 8), f("", 44), f(n_rec, 8), f(record_s, 8), f(ns, 4)])
    cols = {"label": [f(lb, 16) for lb, _f, _x in signals],
            "transducer": [f("", 80)] * ns, "dimension": [f("", 8)] * ns,
            "phys_min": [f(r[0], 8) for r in ranges], "phys_max": [f(r[1], 8) for r in ranges],
            "dig_min": [f(-32768, 8)] * ns, "dig_max": [f(32767, 8)] * ns,
            "prefilter": [f("", 80)] * ns, "spr": [f(s, 8) for s in sprs],
            "reserved": [f("", 32)] * ns}
    for name, _w in _EDF_FIELDS:
        hdr += b"".join(cols[name])
    digs = []
    for (lo, hi), (_l, _f, x), s in zip(ranges, signals, sprs):
        d = np.round((np.asarray(x[: n_rec * s], dtype=float) - lo) / (hi - lo) * 65535 - 32768)
        digs.append(np.clip(d, -32768, 32767).astype("<i2").reshape(n_rec, s))
    with open(path, "wb") as fh:
        fh.write(hdr)
        fh.write(np.concatenate(digs, axis=1).tobytes())


# ------------------------------------------------------------------------- beat detection
def _movmean(x: np.ndarray, w: int) -> np.ndarray:
    """Centred moving average of width ``w`` samples (edges padded with the end values)."""
    w = max(1, int(w))
    if w == 1 or len(x) == 0:
        return np.array(x, dtype=float)
    left, right = w // 2, w - 1 - w // 2
    xp = np.concatenate([np.full(left, x[0]), x, np.full(right, x[-1])])
    c = np.concatenate([[0.0], np.cumsum(xp, dtype=float)])
    return (c[w:] - c[:-w]) / w


def bandpass(x: np.ndarray, fs: float) -> np.ndarray:
    """~0.5-8 Hz band as a difference of moving averages (numpy only, no phase shift)."""
    x = np.asarray(x, dtype=float)
    x = x - float(np.mean(x)) if len(x) else x
    return _movmean(x, round(0.10 * fs)) - _movmean(x, round(1.2 * fs))


def detect_peaks(x: np.ndarray, fs: float, band: Optional[np.ndarray] = None
                 ) -> Tuple[np.ndarray, np.ndarray]:
    """Systolic peaks of one PPG segment (Elgendi et al. 2013, two event-related moving
    averages). Returns sub-sample peak positions and their band-passed amplitudes."""
    if len(x) < int(2 * fs):
        return np.zeros(0), np.zeros(0)
    band = bandpass(x, fs) if band is None else band
    y = np.clip(band, 0.0, None) ** 2
    w1 = max(1, int(round(0.111 * fs)))
    ma_peak = _movmean(y, w1)
    ma_beat = _movmean(y, round(0.667 * fs))
    above = ma_peak > ma_beat + 0.02 * float(np.mean(y))
    d = np.diff(above.astype(np.int8), prepend=np.int8(0), append=np.int8(0))
    starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    keep = (ends - starts) >= w1
    pos: List[float] = []
    amp: List[float] = []
    refractory = 0.27 * fs
    for s, e in zip(starts[keep], ends[keep]):
        p = int(s + np.argmax(band[s:e]))
        a = float(band[p])
        frac = 0.0
        if 0 < p < len(band) - 1:                  # parabolic sub-sample refinement
            y0, y1, y2 = band[p - 1], band[p], band[p + 1]
            den = y0 - 2 * y1 + y2
            if den < 0:
                frac = float(np.clip(0.5 * (y0 - y2) / den, -0.5, 0.5))
        if pos and (p + frac) - pos[-1] < refractory:
            if a > amp[-1]:
                pos[-1], amp[-1] = p + frac, a
            continue
        pos.append(p + frac)
        amp.append(a)
    return np.asarray(pos, dtype=float), np.asarray(amp, dtype=float)


def bad_windows(x: np.ndarray, fs: float, rails: Optional[Tuple[float, float]] = None,
                win_s: float = QUALITY_WIN_S, band: Optional[np.ndarray] = None) -> np.ndarray:
    """One flag per ``win_s`` window: flat, railed / clipped, or band amplitude far outside the
    segment's typical scale (movement, probe off). Beats in a flagged window are rejected."""
    n = max(1, int(round(win_s * fs)))
    m = -(-len(x) // n)
    flags = np.zeros(m, dtype=bool)
    if m == 0:
        return flags
    band = bandpass(x, fs) if band is None else band
    ptp = np.zeros(m)
    for k in range(m):
        w = x[k * n:(k + 1) * n]
        if len(w) < 2 or float(np.ptp(w)) <= 1e-9:
            flags[k] = True
            continue
        at_ext = float(np.mean(w == w.max())) + float(np.mean(w == w.min()))
        if at_ext > 0.20:
            flags[k] = True
        if rails is not None:
            lo, hi = min(rails), max(rails)
            if float(np.mean((w <= lo) | (w >= hi))) > 0.05:
                flags[k] = True
        ptp[k] = float(np.ptp(band[k * n:(k + 1) * n]))
    ok = ~flags & (ptp > 0)
    if ok.any():
        med = float(np.median(ptp[ok]))
        flags |= (ptp > WINDOW_PTP_HIGH * med) | (ptp < 0.15 * med)
    return flags


def template_corr(band: np.ndarray, pos: np.ndarray, fs: float) -> np.ndarray:
    """Each beat's correlation with the segment's median beat shape (a template-matching
    signal-quality index): a movement artifact the detector took for a beat looks nothing
    like the pulses around it. Beats too near the edge score 1 (judged by their neighbours)."""
    pre, post = int(0.25 * fs), int(0.45 * fs)
    idx = np.round(pos).astype(int)
    inside = (idx - pre >= 0) & (idx + post < len(band))
    out = np.ones(len(pos))
    if inside.sum() < 5:
        return out
    seg = np.stack([band[i - pre: i + post] for i in idx[inside]])
    seg = seg - seg.mean(axis=1, keepdims=True)
    tpl = np.median(seg, axis=0)
    tpl = tpl - tpl.mean()
    den = np.linalg.norm(seg, axis=1) * (np.linalg.norm(tpl) or 1.0)
    out[inside] = np.where(den > 0, seg @ tpl / np.where(den > 0, den, 1.0), 0.0)
    return out


def beats_from_signal(read, n: int, fs: float, rails: Optional[Tuple[float, float]] = None,
                      chunk_s: float = CHUNK_S, pad_s: float = PAD_S
                      ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Walk a signal of ``n`` samples in overlapping chunks (``read(start, stop)`` returns
    samples). Returns beat times (s), amplitudes and a per-beat signal-quality flag."""
    L, P = max(1, int(chunk_s * fs)), int(pad_s * fs)
    qn = max(1, int(round(QUALITY_WIN_S * fs)))
    times: List[np.ndarray] = []
    amps: List[np.ndarray] = []
    oks: List[np.ndarray] = []
    for a in range(0, n, L):
        b = min(n, a + L)
        lo, hi = max(0, a - P), min(n, b + P)
        x = read(lo, hi)
        if len(x) < int(2 * fs):
            continue
        band = bandpass(x, fs)
        pos, amp = detect_peaks(x, fs, band)
        corr = template_corr(band, pos, fs)
        sel = (pos + lo >= a) & (pos + lo < b)
        pos, amp, corr = pos[sel], amp[sel], corr[sel]
        flags = bad_windows(x, fs, rails, band=band)
        wi = np.minimum((pos // qn).astype(int), len(flags) - 1)
        times.append((pos + lo) / fs)
        amps.append(amp)
        oks.append(~flags[wi] & (corr >= TEMPLATE_MIN_CORR))
    if not times:
        return np.zeros(0), np.zeros(0), np.zeros(0, dtype=bool)
    return np.concatenate(times), np.concatenate(amps), np.concatenate(oks)


def _rolling_median(v: np.ndarray, half: int) -> np.ndarray:
    out = np.empty(len(v))
    for i in range(len(v)):
        out[i] = np.median(v[max(0, i - half): i + half + 1])
    return out


def clean_ibis(times: np.ndarray, amps: Optional[np.ndarray] = None,
               ok: Optional[np.ndarray] = None) -> List[Tuple[float, float]]:
    """Beat times -> [(t_beat_s, ibi_ms)] with artifact rejection (see the module doc)."""
    t = np.asarray(times, dtype=float)
    if len(t) < 3:
        return []
    order = np.argsort(t)
    t = t[order]
    ok = np.ones(len(t), dtype=bool) if ok is None else np.asarray(ok, dtype=bool)[order]
    if amps is not None:
        a = np.abs(np.asarray(amps, dtype=float)[order])
        ref = a.copy()
        ref[~ok] = np.nan
        good = np.flatnonzero(ok)
        if len(good):
            med = np.full(len(a), np.nan)
            med[good] = _rolling_median(a[good], 10)
            # beats in bad windows borrow the nearest good beat's reference
            idx = np.clip(np.searchsorted(good, np.arange(len(a))), 0, len(good) - 1)
            med = np.where(np.isnan(med), med[good][idx], med)
            keep = a >= AMP_LOW * med                  # below: not a beat at all
            ok = ok & (a <= AMP_HIGH * med)           # above: a movement artifact
            t, ok = t[keep], ok[keep]
    ibi = np.diff(t) * 1000.0
    valid = ok[1:] & ok[:-1] & (ibi >= IBI_MIN_MS) & (ibi <= IBI_MAX_MS)
    cand = np.flatnonzero(valid)
    if len(cand) == 0:
        return []
    vals = ibi[cand]
    out: List[Tuple[float, float]] = []
    k = IBI_NEIGHBOURS
    for j, i in enumerate(cand):
        lo, hi = max(0, j - k), min(len(cand), j + k + 1)
        near = np.concatenate([vals[lo:j], vals[j + 1:hi]])
        # neighbours only count if they are close in time (not across a long gap)
        tn = np.concatenate([t[cand[lo:j] + 1], t[cand[j + 1:hi] + 1]])
        near = near[np.abs(tn - t[i + 1]) <= 30.0]
        if len(near) < 3:
            continue
        med = float(np.median(near))
        if abs(vals[j] - med) <= IBI_LOCAL_TOL * med:
            out.append((float(t[i + 1]), float(vals[j])))
    return out


def hr_from_ibis(ibis: Sequence[Tuple[float, float]], window_s: float = 5.0
                 ) -> List[Tuple[float, float]]:
    """1 Hz heart rate: 60000 / mean interval of the beats in the trailing ``window_s``."""
    if len(ibis) < 2:
        return []
    t = np.asarray([p[0] for p in ibis])
    v = np.asarray([p[1] for p in ibis])
    c = np.concatenate([[0.0], np.cumsum(v)])
    secs = np.arange(math.ceil(t[0]), math.floor(t[-1]) + 1, dtype=float)
    hi = np.searchsorted(t, secs, side="right")
    lo = np.searchsorted(t, secs - window_s, side="right")
    cnt = hi - lo
    out = []
    for s, a, b, nb in zip(secs, lo, hi, cnt):
        if nb >= 2:
            hr = 60000.0 * nb / (c[b] - c[a])
            if 25.0 <= hr <= 220.0:
                out.append((float(s), float(hr)))
    return out


def pleth_ibis(edf: EdfReader, idx: Optional[int] = None) -> Tuple[List[Tuple[float, float]], dict]:
    """Clean beat intervals from an EDF's plethysmogram, read chunk by chunk."""
    idx = edf.find_signal(PLETH_LABELS) if idx is None else idx
    if idx is None:
        raise LayoutError(f"no pleth channel among {len(edf.labels)} EDF signals")
    fs = edf.fs(idx)
    if fs < 32:
        raise LayoutError(f"pleth sampled at {fs:g} Hz (need >= 32 Hz)")
    times, amps, ok = beats_from_signal(lambda a, b: edf.read(idx, a, b), edf.n_samples(idx),
                                        fs, rails=edf.rails(idx))
    ibis = clean_ibis(times, amps, ok)
    return ibis, {"fs": fs, "beats": int(len(times)), "beats_ok": int(np.sum(ok)),
                  "ibis": len(ibis)}


def rpoint_ibis(path: str) -> List[Tuple[float, float]]:
    """Clean intervals from an NSRR ``-rpoint.csv`` (ECG R-points; ``seconds`` column)."""
    with open(path, newline="") as fh:
        rd = csv.reader(fh)
        head = [h.strip().lower() for h in next(rd, [])]
        col = head.index("seconds") if "seconds" in head else None
        if col is None:
            raise LayoutError("no 'seconds' column in the R-point file")
        ts = []
        for row in rd:
            try:
                ts.append(float(row[col]))
            except (IndexError, ValueError):
                continue
    return clean_ibis(np.asarray(ts))


# ---------------------------------------------------------------------------- annotations
def parse_stages(path: str) -> Dict[int, int]:
    """30 s epoch index -> stage code from an NSRR XML (``annotations-events-nsrr``) or a
    Compumedics Profusion XML (``annotations-events-profusion``)."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise LayoutError(f"annotation XML does not parse ({exc})") from None
    try:
        epoch = float(root.findtext("EpochLength") or EPOCH_S)
    except ValueError:
        epoch = EPOCH_S
    labels: Dict[int, int] = {}
    events = root.find("ScoredEvents")
    stages = root.find("SleepStages")
    if events is not None and root.find("ScoredEvents/ScoredEvent/EventConcept") is not None:
        for ev in events.iter("ScoredEvent"):
            etype = (ev.findtext("EventType") or "").strip().lower()
            if not etype.startswith("stages"):
                continue
            concept = (ev.findtext("EventConcept") or "").strip().lower()
            code = NSRR_STAGES.get(concept)
            if code is None:
                try:
                    code = PROFUSION_STAGES.get(int(concept.rsplit("|", 1)[-1]), -1)
                except ValueError:
                    code = -1
            try:
                start = float(ev.findtext("Start") or "nan")
                dur = float(ev.findtext("Duration") or "nan")
            except ValueError:
                continue
            if not (math.isfinite(start) and math.isfinite(dur)) or dur <= 0:
                continue
            k0 = int(round(start / EPOCH_S))
            for k in range(k0, k0 + max(1, int(round(dur / EPOCH_S)))):
                labels[k] = code
    elif stages is not None:
        for i, el in enumerate(stages.findall("SleepStage")):
            try:
                v = int(float((el.text or "").strip()))
            except ValueError:
                v = -1
            labels[int(round(i * epoch / EPOCH_S))] = PROFUSION_STAGES.get(v, -1)
    else:
        raise LayoutError("annotation XML has neither ScoredEvents nor SleepStages")
    if not any(c >= 0 for c in labels.values()):
        raise LayoutError("annotation XML holds no scored sleep stages")
    return labels


# ------------------------------------------------------------------------------ actigraphy
def mesa_id(record: str) -> Optional[int]:
    m = re.search(r"(\d+)$", record.split("-nsrr")[0].split("-profusion")[0])
    return int(m.group(1)) if m else None


def read_overlap(path: str) -> Dict[int, Dict[str, str]]:
    """``overlap/mesa-actigraphy-psg-overlap.csv``: mesaid -> its row (line at PSG start)."""
    out: Dict[int, Dict[str, str]] = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            try:
                out[int(float(row["mesaid"]))] = row
            except (KeyError, ValueError):
                continue
    return out


def _clock_s(s: str) -> Optional[int]:
    try:
        parts = [int(p) for p in s.replace(".", ":").split(":")]
        return parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) > 2 else 0)
    except (ValueError, IndexError):
        return None


def activity_lines(act_csv: str, overlap_row: Dict[str, str], duration_s: float) -> List[str]:
    """Actiwatch 30 s counts on the PSG clock (epoch 0 = the overlap file's ``line``)."""
    try:
        start_line = int(float(overlap_row["line"]))
    except (KeyError, ValueError):
        raise LayoutError("overlap row has no 'line'") from None
    # the actigraphy epoch boundary may sit up to 30 s off the PSG start
    shift = 0.0
    a, b = _clock_s(overlap_row.get("linetime", "")), _clock_s(overlap_row.get("starttime_psg", ""))
    if a is not None and b is not None:
        d = (a - b) % 86400
        shift = float(d - 86400 if d > 43200 else d)
        if abs(shift) > 60:
            shift = 0.0
    out = []
    with open(act_csv, newline="") as fh:
        for row in csv.DictReader(fh):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            try:
                line = int(float(row["line"]))
            except (KeyError, ValueError):
                continue
            if line < start_line:
                continue
            t = (line - start_line) * EPOCH_S + shift
            if t > duration_s:
                break
            if row.get("offwrist") not in (None, "", "0", "0.0"):
                continue
            try:
                act = float(row.get("activity", ""))
            except ValueError:
                continue
            if not math.isfinite(act) or act < 0:
                continue
            out.append(f"{t:.0f},{act:g},0,0,0,{act:g},1")
    return out


# --------------------------------------------------------------------------------- output
def write_reduced(out_dir: str, rid: str, hr_rows, labels: Dict[int, int], ibi_rows,
                  act_lines: Sequence[str], act_note: str) -> None:
    """The four files, byte-compatible with dreamt_reduce's. ``<ID>_ibi.txt`` is written
    LAST: the pipeline treats it as "this record is done"."""
    os.makedirs(os.path.join(out_dir, "activity"), exist_ok=True)
    with open(os.path.join(out_dir, f"{rid}_heartrate.txt"), "w") as fh:
        fh.write("".join(f"{t:.0f},{v:.2f}\n" for t, v in hr_rows))
    with open(os.path.join(out_dir, f"{rid}_labeled_sleep.txt"), "w") as fh:
        fh.write("".join(f"{k * EPOCH_S:.0f} {labels[k]}\n" for k in sorted(labels)))
    with open(os.path.join(out_dir, "activity", f"{rid}_activity.txt"), "w") as fh:
        fh.write(f"# epoch_start_s,pim,zcm,mad,std,pmax,n  ({act_note})\n")
        fh.write("\n".join(act_lines) + ("\n" if act_lines else ""))
    with open(os.path.join(out_dir, f"{rid}_ibi.txt"), "w") as fh:
        fh.write("".join(f"{t:.3f},{v:.1f}\n" for t, v in ibi_rows))


def reduce_record(rid: str, xml_path: str, out_dir: str, edf_path: Optional[str] = None,
                  rpoint_path: Optional[str] = None, act_path: Optional[str] = None,
                  overlap_row: Optional[Dict[str, str]] = None, verbose: bool = False
                  ) -> Dict[str, object]:
    """Reduce one MESA record. Beats come from the EDF's pleth (or, if no EDF is given, an
    R-point file). Raises LayoutError when the record cannot be used at all."""
    t0 = time.time()
    labels = parse_stages(xml_path)
    info: Dict[str, object] = {}
    duration = (max(labels) + 1) * EPOCH_S
    if edf_path:
        with EdfReader(edf_path) as edf:
            ibis, info = pleth_ibis(edf)
            duration = min(duration, edf.duration_s) if edf.duration_s > 0 else duration
            labels = {k: c for k, c in labels.items() if k * EPOCH_S < edf.duration_s}
    elif rpoint_path:
        ibis = rpoint_ibis(rpoint_path)
        info = {"ibis": len(ibis), "source": "rpoints"}
    else:
        raise ValueError("need an EDF or an R-point file")
    scored = sum(1 for c in labels.values() if c >= 0)
    if scored < MIN_EPOCHS:
        raise LayoutError(f"only {scored} scored epochs")
    if len(ibis) < MIN_IBIS:
        raise LayoutError(f"only {len(ibis)} clean beat intervals")
    hr = hr_from_ibis(ibis)
    act: List[str] = []
    if act_path and overlap_row:
        try:
            act = activity_lines(act_path, overlap_row, duration)
        except (LayoutError, OSError):
            act = []
    write_reduced(out_dir, rid, hr, labels, ibis, act,
                  "MESA Actiwatch counts in pim/pmax; scale-free features only")
    dist: Dict[int, int] = {}
    for c in labels.values():
        dist[c] = dist.get(c, 0) + 1
    summ = {"id": rid, "epochs": len(labels), "hr": len(hr), "ibi": len(ibis),
            "activity_epochs": len(act), "labels": dist, "seconds": round(time.time() - t0, 1),
            **{k: v for k, v in info.items() if k in ("beats", "beats_ok", "fs")}}
    if verbose:
        print(f"  {rid}: {len(labels)} epochs, {len(hr)} HR, {len(ibis)} IBI "
              f"({info.get('beats', '?')} beats), {len(act)} activity epochs, labels {dist} "
              f"[{summ['seconds']}s]", flush=True)
    return summ


# ------------------------------------------------------------------------------------ CLI
def discover(data_dir: str) -> List[Tuple[str, str, Optional[str]]]:
    """(record, xml, edf-or-None) for a local copy in the NSRR layout (``nsrr download``)."""
    psg = os.path.join(data_dir, "polysomnography")
    if not os.path.isdir(psg):
        psg = data_dir
    xmls: Dict[str, str] = {}
    for sub, suffix in (("annotations-events-profusion", "-profusion.xml"),
                        ("annotations-events-nsrr", "-nsrr.xml")):      # nsrr wins
        for p in glob.glob(os.path.join(psg, sub, f"*{suffix}")):
            xmls[os.path.basename(p)[: -len(suffix)]] = p
    edfs = {os.path.basename(p)[:-4]: p for p in glob.glob(os.path.join(psg, "edfs", "*.edf"))}
    return [(r, xmls[r], edfs.get(r)) for r in sorted(xmls)]


def verify(out_dir: str) -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import dreamt_reduce
    return dreamt_reduce.verify(out_dir)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", help="a local MESA copy in the NSRR layout (holds polysomnography/)")
    ap.add_argument("--out", required=True, help="reduced output folder (keep OUT of the repo)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--delete-edf", action="store_true", help="delete each EDF once reduced")
    ap.add_argument("--verify", action="store_true", help="only print what --out holds")
    args = ap.parse_args(argv)
    if args.verify:
        return verify(args.out)
    if not args.data_dir:
        ap.error("--data-dir is required unless --verify")
    recs = [r for r in discover(args.data_dir) if r[2]]
    if args.limit:
        recs = recs[: args.limit]
    if not recs:
        print(f"no EDF + XML pairs under {args.data_dir}")
        return 1
    root = args.data_dir
    overlap: Dict[int, Dict[str, str]] = {}
    ov = glob.glob(os.path.join(root, "overlap", "*overlap*.csv"))
    if ov:
        overlap = read_overlap(ov[0])
    done = 0
    for rid, xml, edf in recs:
        if args.skip_existing and os.path.exists(os.path.join(args.out, f"{rid}_ibi.txt")):
            continue
        act = os.path.join(root, "actigraphy", f"{rid}.csv")
        try:
            reduce_record(rid, xml, args.out, edf_path=edf,
                          act_path=act if os.path.exists(act) else None,
                          overlap_row=overlap.get(mesa_id(rid) or -1), verbose=True)
            done += 1
            if args.delete_edf:
                os.remove(edf)
        except Exception as exc:
            print(f"  {rid}: FAILED {type(exc).__name__}: {exc}", flush=True)
    print(f"done: {done} reduced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
