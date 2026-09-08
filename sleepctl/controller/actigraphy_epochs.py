"""Epoch-level use of the armband's accelerometer counts.

The per-tick ``movement`` index answers "is the body moving right now". Two questions matter
more for staying asleep and need the dense counts over minutes:

* **Movement clusters** -- a run of small movements over a few epochs is a micro-arousal or a
  turn, distinct from one big shift (the single-burst wake rule stays; it was validated 6/6 on
  message-timestamp ground truth and a sustained-motion requirement dropped it to 3/6).
* **Restlessness ramp** -- awakenings are preceded by minutes of RISING movement density.
  Measured as bursts per window against the night's own baseline density, it leads the heart
  rate creep and is the signal the pre-empt has been missing.

Scale-free by construction: every count is expressed as a multiple of the same burst
threshold the wake rule uses (``est_stage_actigraphy_wake_pim``), so the units of the count
never matter as long as they are counts. Pure functions over ``(t_seconds, count)`` samples.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

EPOCH_S = 30.0
#: Windowed cluster score weights, newest epoch first (the last two minutes).
_CLUSTER_WEIGHTS = (0.45, 0.25, 0.18, 0.12)
#: A cluster score at/above this is a movement cluster (micro-arousal grade).
CLUSTER_AROUSAL = 0.4


def _val(s) -> Optional[float]:
    try:
        return float(s[1])
    except Exception:
        return None


def epoch_counts(history: Sequence, now_t: float, epochs: int = 8,
                 epoch_s: float = EPOCH_S) -> List[float]:
    """Max count per epoch over the last ``epochs`` epochs ending at ``now_t``, newest first.
    Missing epochs read 0 (no data is no movement for these purposes)."""
    out = [0.0] * epochs
    if not history:
        return out
    start = now_t - epochs * epoch_s
    for s in history:
        try:
            t = float(s[0])
        except Exception:
            continue
        if t <= start or t > now_t:
            continue
        v = _val(s)
        if v is None:
            continue
        k = int((now_t - t) // epoch_s)      # 0 = newest epoch
        if 0 <= k < epochs and v > out[k]:
            out[k] = v
    return out


def cluster_score(history: Sequence, now_t: float, burst_thresh: float) -> float:
    """Weighted recent movement in burst units, 0..~3. One isolated burst scores ~0.45;
    movement across several epochs scores higher. ``burst_thresh`` > 0."""
    if burst_thresh <= 0:
        return 0.0
    ep = epoch_counts(history, now_t, epochs=len(_CLUSTER_WEIGHTS))
    return sum(w * min(3.0, e / burst_thresh) for w, e in zip(_CLUSTER_WEIGHTS, ep))


def restlessness(history: Sequence, now_t: float, burst_thresh: float,
                 window_min: float = 5.0, baseline_min: float = 45.0,
                 burst_frac: float = 0.5) -> dict:
    """Movement density now vs the night's own baseline.

    ``density``: epochs with a burst (count >= burst_frac * burst_thresh) per ``window_min``
    over the most recent window. ``baseline``: the same rate over the ``baseline_min`` before
    that window. ``ratio``: density / baseline (baseline floored at 0.5 bursts per window so a
    perfectly still night does not make the first turn infinite)."""
    if burst_thresh <= 0 or not history:
        return {"density": 0.0, "baseline": 0.0, "ratio": 0.0, "n_epochs": 0}
    thr = burst_frac * burst_thresh
    w_epochs = max(1, int(window_min * 60.0 / EPOCH_S))
    b_epochs = max(1, int(baseline_min * 60.0 / EPOCH_S))
    ep = epoch_counts(history, now_t, epochs=w_epochs + b_epochs)
    recent, base = ep[:w_epochs], ep[w_epochs:]
    n_recent = sum(1 for e in recent if e >= thr)
    n_base = sum(1 for e in base if e >= thr)
    base_rate = n_base / (len(base) / w_epochs) if base else 0.0   # bursts per window
    ratio = n_recent / max(0.5, base_rate)
    have = sum(1 for s in history if _val(s) is not None)
    return {"density": float(n_recent), "baseline": round(base_rate, 2),
            "ratio": round(ratio, 2), "n_epochs": have}


def counts_history(frame) -> Optional[Sequence]:
    """The frame's dense activity history when it is in counts, else None."""
    if getattr(frame, "activity_units", None) != "counts":
        return None
    hist = getattr(frame, "activity_history", None)
    return hist or None
