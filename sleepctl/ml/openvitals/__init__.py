"""OpenVitals heart-rate sleep stager, vendored as a second, independent morning rater.

Source: https://github.com/Aditya-galaxy/openvitals (MIT, see LICENSE and NOTICE here). The model
file ``stager_hr_4class.onnx`` is OpenVitals 0.6's bundled ensemble: 5 TCNs + 5 bidirectional
GRUs over six heart-rate features per 30 s epoch, trained on BIDSleep (253 Apple Watch nights,
Dreem EEG labels) + Walch sleep-accel (31 Apple Watch nights, lab PSG), ODC-By 1.0. OpenVitals
reports four-class kappa 0.51 on held-out Walch subjects (0.46 with once-a-minute heart rate).

Why it is here and not in the live loop: its networks read the WHOLE night (the TCN sees about an
hour either side of each epoch, the GRUs run both ways), so it can only stage a night after the
fact. That makes it a good morning rater for ``learning.wearable_consensus`` -- PSG-trained,
heart-rate only, and independent of Apple's algorithm -- not a replacement for the causal live
stager.

The feature code below is a faithful port of ``openvitals.features`` (HR inputs only) and the
main-sleep-window trimming of ``openvitals.infer``; numpy + onnxruntime, nothing else.
"""

from __future__ import annotations

import json
import os
from typing import List, Optional, Sequence, Tuple

import numpy as np

EPOCH_SECONDS = 30
MODEL_PATH = os.path.join(os.path.dirname(__file__), "stager_hr_4class.onnx")
TRIM_PAD = 20 * 60             # seconds kept before sleep onset / after final wake
MAX_WAKE_GAP = 30 * 60         # wake runs shorter than this stay inside the main sleep period
MAX_COVER_GAP = 10 * 60        # an epoch farther than this from any HR sample has no data
#: OpenVitals' class names -> the controller's
CLASS_MAP = {"wake": "awake", "light": "light", "deep": "deep", "rem": "rem"}

_session = None


def available() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except Exception:
        return False
    return os.path.exists(MODEL_PATH)


def _load():
    global _session
    if _session is None:
        import onnxruntime as ort
        _session = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
    return _session


# ------------------------------------------------------------------ features (openvitals port)
def _interp_missing(a: np.ndarray) -> np.ndarray:
    a = a.astype(float).copy()
    ok = np.isfinite(a)
    if ok.sum() == 0:
        return a
    if ok.sum() < len(a):
        a[~ok] = np.interp(np.flatnonzero(~ok), np.flatnonzero(ok), a[ok])
    return a


def _centered_mean(a: np.ndarray, w: int) -> np.ndarray:
    if len(a) == 0:
        return a
    pad = w // 2
    return np.convolve(np.pad(a, pad, mode="edge"), np.ones(w) / w, mode="valid")


def _centered_std(a: np.ndarray, w: int) -> np.ndarray:
    if len(a) == 0:
        return a
    m = _centered_mean(a, w)
    return np.sqrt(np.maximum(_centered_mean(a * a, w) - m * m, 0.0))


def features(hr_rel_t: np.ndarray, hr: np.ndarray, n: int) -> np.ndarray:
    """(n, 6): hr_z, hr_z_w5, hr_std_w5, hr_std_w11, hr_diff, hours_since_start."""
    idx = np.floor(hr_rel_t / EPOCH_SECONDS).astype(np.int64)
    keep = (idx >= 0) & (idx < n)
    s = np.bincount(idx[keep], weights=hr[keep], minlength=n)
    c = np.bincount(idx[keep], minlength=n)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(c > 0, s / np.maximum(c, 1), np.nan)
    h = _interp_missing(mean)
    med = np.nanmedian(h) if np.isfinite(h).any() else 0.0
    scale = np.nanstd(h) if np.isfinite(h).any() else 1.0
    z = np.nan_to_num((h - med) / (scale if scale > 1e-6 else 1.0))
    cols = [z, _centered_mean(z, 5), _centered_std(z, 5), _centered_std(z, 11),
            np.abs(np.diff(z, prepend=z[:1])), np.arange(n) * EPOCH_SECONDS / 3600.0]
    return np.column_stack(cols).astype(np.float32)


def _near_hr(hr_rel_t: np.ndarray, n: int) -> np.ndarray:
    if len(hr_rel_t) == 0:
        return np.zeros(n, dtype=bool)
    step = float(np.median(np.diff(hr_rel_t))) if len(hr_rel_t) > 1 else EPOCH_SECONDS
    tol = min(MAX_COVER_GAP, max(EPOCH_SECONDS, 2.5 * step))
    centres = (np.arange(n) + 0.5) * EPOCH_SECONDS
    i = np.clip(np.searchsorted(hr_rel_t, centres), 1, max(len(hr_rel_t) - 1, 1))
    nearest = np.minimum(np.abs(centres - hr_rel_t[i - 1]),
                         np.abs(hr_rel_t[np.minimum(i, len(hr_rel_t) - 1)] - centres))
    return nearest <= tol


def _main_sleep_window(pred: np.ndarray, has_data: np.ndarray) -> Optional[Tuple[int, int]]:
    idx = np.flatnonzero((pred > 0) & has_data)
    if idx.size == 0:
        return None
    max_gap = MAX_WAKE_GAP // EPOCH_SECONDS
    best, cur, prev = (idx[0], idx[0]), idx[0], idx[0]
    for i in idx[1:]:
        if i - prev > max_gap:
            if prev - cur > best[1] - best[0]:
                best = (cur, prev)
            cur = i
        prev = i
    if prev - cur > best[1] - best[0]:
        best = (cur, prev)
    return int(best[0]), int(best[1])


def _probs(t: np.ndarray, v: np.ndarray, start: float, n: int):
    rel = t - start
    keep = (rel >= 0) & (rel < n * EPOCH_SECONDS)
    X = features(rel[keep], v[keep], n)
    out = _load().run(["probs"], {"features": X[None]})[0][0]
    return out, _near_hr(rel[keep], n)


def stage(hr: Sequence[Tuple[float, float]], start: Optional[float] = None,
          end: Optional[float] = None) -> Optional[dict]:
    """Stage a night from (unix, bpm) samples. Returns {"start": unix of epoch 0, "labels":
    [awake|light|deep|rem|None per 30 s epoch], "probs": [[...]]} or None."""
    if not hr:
        return None
    a = np.asarray(hr, dtype=float)
    ok = np.isfinite(a[:, 0]) & np.isfinite(a[:, 1]) & (a[:, 1] > 25) & (a[:, 1] < 230)
    a = a[ok]
    a = a[np.argsort(a[:, 0])]
    if len(a) < 10:
        return None
    t, v = a[:, 0], a[:, 1]
    start = float(np.floor(t.min())) if start is None else float(start)
    end = float(t.max()) if end is None else float(end)
    n = int(np.ceil((end - start) / EPOCH_SECONDS))
    if n <= 0:
        return None
    probs, has = _probs(t, v, start, n)
    # the model was trained with epoch 0 near lights-off: re-run on the main sleep period +/- pad
    win = _main_sleep_window(probs.argmax(1), has)
    if win is not None:
        lo = max(0, win[0] - TRIM_PAD // EPOCH_SECONDS)
        hi = min(n, win[1] + 1 + TRIM_PAD // EPOCH_SECONDS)
        if (lo, hi) != (0, n):
            start += lo * EPOCH_SECONDS
            n = hi - lo
            probs, has = _probs(t, v, start, n)
    classes = json.loads(_load().get_modelmeta().custom_metadata_map["classes"])
    labels: List[Optional[str]] = [CLASS_MAP.get(classes[int(k)]) if h else None
                                   for k, h in zip(probs.argmax(1), has)]
    return {"start": start, "labels": labels, "probs": probs.tolist()}


__all__ = ["available", "stage", "features", "EPOCH_SECONDS", "MODEL_PATH"]
