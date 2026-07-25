"""Runtime sleep-stage inference — PURE standard library (json/math/bisect), NO numpy.

Loads the compact JSON tree-ensemble weights bundled under ``weights/`` and scores trailing
HR (optionally + movement) samples streamed from a Polar Verity Sense. Feature computation
is shared verbatim with training via :mod:`features`, guaranteeing train/inference parity.

Model variants under ``weights/``:
  * HR-only       ``wake_hr.json``,        ``stage4_hr.json``        -- Verity Sense alone
  * HR + motion   ``wake_hrmotion.json``,  ``stage4_hrmotion.json``  -- + a movement signal
  * sparse HR     ``wake_hr_sparse.json``, ``stage4_hr_sparse.json`` -- trained on 1 sample/min
  * ``hmm.json``  4x4 transition matrix + class order + start/prior distributions

Because this feeds a *thermal controller*, the stage must not flap tick-to-tick, so the
posterior is temporally smoothed with an **online HMM forward filter**: emissions are
recomputed at each of the last ``smoothing_epochs`` 30 s epoch ends from the sample history
and the forward recursion is run over them, returning the final posterior. The filter is
**stateless** — no hidden mutable state, no ``reset()``; identical input gives identical
output — which keeps it safe for a controller that may restart at any time.

Usage::

    stager = SleepStager.load()
    if stager.available:
        est = stager.predict(hr_samples, activity_samples, minutes_since_start=120)
"""

from __future__ import annotations

import bisect
import json
import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .features import (
    FEATURE_NAMES_HR,
    FEATURE_NAMES_HRMOTION,
    MAX_LOOKBACK_S,
    compute_features,
    feature_vector,
    stats_from_sorted,
)

WEIGHTS_DIR = os.path.join(os.path.dirname(__file__), "weights")

STAGE4_LABELS = ["wake", "light", "deep", "rem"]
EPOCH_S = 30.0
DEFAULT_SMOOTHING_EPOCHS = 20  # 10 minutes of causal context

Sample = Tuple[float, float]


@dataclass
class StageEstimate:
    stage_label: str          # "wake" / "light" / "deep" / "rem"
    p_wake: float             # probability of wake, 0..1
    confidence: float         # winning class probability, 0..1
    probs: Dict[str, float]   # label -> probability (posterior, after smoothing)
    source: str = "model"
    smoothed: bool = False    # True when the HMM forward filter was applied


# --------------------------------------------------------------------------- forest model
@dataclass
class _Forest:
    """A tree ensemble stored as flat per-tree arrays (see train.py for the writer).

    Each tree: ``f`` feature index (<0 marks a leaf), ``t`` threshold, ``l``/``r`` child
    indices (for leaves ``l`` indexes into the flat leaf-probability array ``v``).
    """

    feature_names: List[str]
    classes: List[int]
    n_classes: int
    trees: List[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "_Forest":
        classes = [int(c) for c in d["classes"]]
        return cls(
            feature_names=list(d["feature_names"]),
            classes=classes,
            n_classes=len(classes),
            trees=[
                {
                    "f": [int(x) for x in tr["f"]],
                    "t": [float(x) for x in tr["t"]],
                    "l": [int(x) for x in tr["l"]],
                    "r": [int(x) for x in tr["r"]],
                    "v": [float(x) for x in tr["v"]],
                }
                for tr in d["trees"]
            ],
        )

    def predict_proba_vec(self, x: Sequence[float]) -> List[float]:
        c = self.n_classes
        acc = [0.0] * c
        for tr in self.trees:
            f = tr["f"]
            th = tr["t"]
            left = tr["l"]
            right = tr["r"]
            node = 0
            while f[node] >= 0:
                node = left[node] if x[f[node]] <= th[node] else right[node]
            base = left[node] * c
            v = tr["v"]
            for k in range(c):
                acc[k] += v[base + k]
        n = len(self.trees) or 1
        out = [a / n for a in acc]
        s = sum(out)
        return [o / s for o in out] if s > 0 else [1.0 / c] * c

    def predict_proba(self, feats: Dict[str, float]) -> List[float]:
        return self.predict_proba_vec(feature_vector(feats, self.feature_names))


def _load_json(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def _load_model(path: str) -> Optional[_Forest]:
    d = _load_json(path)
    if not d:
        return None
    try:
        return _Forest.from_dict(d)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------- HMM smoothing math
def blend_emission(stage_probs: Sequence[float], p_wake: float, weight: float = 0.5
                   ) -> List[float]:
    """Mix the 4-class posterior with the dedicated binary wake model into one emission.

    The binary wake head is trained on a balanced wake/sleep split and is the more reliable
    wake detector, so half the emission mass follows it while the sleep-stage *shape* is
    taken from the 4-class head. Result sums to 1. Used identically in training-time
    evaluation and at inference, so reported CV numbers describe the shipped pipeline.
    """
    p4 = [max(0.0, float(p)) for p in stage_probs]
    s = sum(p4)
    p4 = [p / s for p in p4] if s > 0 else [0.25] * 4
    pw = min(1.0, max(0.0, float(p_wake)))
    sleep_mass = max(1e-9, 1.0 - p4[0])
    out = [0.0] * 4
    out[0] = (1.0 - weight) * p4[0] + weight * pw
    for k in range(1, 4):
        out[k] = (1.0 - weight) * p4[k] + weight * (1.0 - pw) * (p4[k] / sleep_mass)
    tot = sum(out)
    return [o / tot for o in out] if tot > 0 else [0.25] * 4


def forward_filter(
    emissions: Sequence[Sequence[float]],
    trans: Sequence[Sequence[float]],
    start: Sequence[float],
    prior: Sequence[float],
    temper: float = 1.0,
) -> List[float]:
    """Causal HMM forward recursion over a run of emissions; returns the final posterior.

    Emissions are class *posteriors*, converted to likelihoods by dividing out the class
    prior the classifier was trained under (uniform for class-balanced training), so the
    transition matrix — not the emission prior — carries the temporal structure.

    ``temper`` raises each likelihood to a power < 1. Consecutive 30 s epochs share almost
    all of their trailing window, so their emissions are far from independent; multiplying
    20 near-duplicate likelihoods would saturate the posterior and make the filter *worse*
    than no smoothing at all. The exponent is fitted by grouped CV in ``train.py`` and
    stored in ``hmm.json``.

    Streaming-safe: only past epochs are used (no Viterbi backtrace, no hidden state).
    """
    n = len(start)
    alpha: Optional[List[float]] = None
    a = float(temper)
    for e in emissions:
        lik = [(max(float(e[k]), 1e-9) / max(float(prior[k]), 1e-9)) ** a for k in range(n)]
        if alpha is None:
            pred = list(start)
        else:
            pred = [sum(alpha[i] * trans[i][k] for i in range(n)) for k in range(n)]
        post = [max(pred[k], 1e-12) * lik[k] for k in range(n)]
        tot = sum(post)
        alpha = [p / tot for p in post] if tot > 0 else [1.0 / n] * n
    return alpha if alpha is not None else [1.0 / n] * n


# --------------------------------------------------------------------------- the stager
class SleepStager:
    def __init__(
        self,
        wake_hr: Optional[_Forest] = None,
        stage4_hr: Optional[_Forest] = None,
        wake_hrmotion: Optional[_Forest] = None,
        stage4_hrmotion: Optional[_Forest] = None,
        hmm: Optional[dict] = None,
        smoothing_epochs: int = DEFAULT_SMOOTHING_EPOCHS,
    ) -> None:
        self.wake_hr = wake_hr
        self.stage4_hr = stage4_hr
        self.wake_hrmotion = wake_hrmotion
        self.stage4_hrmotion = stage4_hrmotion
        self.hmm = hmm
        self.smoothing_epochs = max(1, int(smoothing_epochs))
        self._hr_ok = wake_hr is not None and stage4_hr is not None
        self._hrmotion_ok = wake_hrmotion is not None and stage4_hrmotion is not None
        self.available = self._hr_ok or self._hrmotion_ok

    @classmethod
    def load(cls, weights_dir: str = WEIGHTS_DIR,
             smoothing_epochs: Optional[int] = None) -> "SleepStager":
        hmm = _load_json(os.path.join(weights_dir, "hmm.json"))
        if smoothing_epochs is None:
            smoothing_epochs = int((hmm or {}).get("smoothing_epochs",
                                                   DEFAULT_SMOOTHING_EPOCHS))
        return cls(
            wake_hr=_load_model(os.path.join(weights_dir, "wake_hr.json")),
            stage4_hr=_load_model(os.path.join(weights_dir, "stage4_hr.json")),
            wake_hrmotion=_load_model(os.path.join(weights_dir, "wake_hrmotion.json")),
            stage4_hrmotion=_load_model(os.path.join(weights_dir, "stage4_hrmotion.json")),
            hmm=hmm,
            smoothing_epochs=smoothing_epochs,
        )

    # ------------------------------------------------------------------ public interface
    def predict(
        self,
        hr_samples: Optional[Sequence[Sample]],
        activity_samples: Optional[Sequence[Sequence[float]]] = None,
        minutes_since_start: Optional[float] = None,
        minutes_since_onset: Optional[float] = None,
        *,
        smooth: bool = True,
    ) -> Optional[StageEstimate]:
        """Stage estimate from trailing ``(t_seconds, value)`` sample histories.

        ``activity_samples`` may be ``(t, movement)`` — any monotone movement scale works,
        the motion features are expressed relative to this recording's own distribution —
        or the 6-tuple actigraphy form ``(t, pim, zcm, mad, std, pmax)``.
        """
        if not hr_samples or not self.available:
            return None

        use_motion = bool(activity_samples) and self._hrmotion_ok
        if use_motion:
            wake_model, stage_model = self.wake_hrmotion, self.stage4_hrmotion
        elif self._hr_ok:
            wake_model, stage_model = self.wake_hr, self.stage4_hr
        else:  # only motion models bundled -> use them, motion block simply reads empty
            wake_model, stage_model = self.wake_hrmotion, self.stage4_hrmotion
            use_motion = True

        hr = sorted(((float(t), float(v)) for t, v in hr_samples), key=lambda s: s[0])
        act: List[Sequence[float]] = []
        if use_motion and activity_samples:
            act = sorted((tuple(float(x) for x in s) for s in activity_samples),
                         key=lambda s: s[0])
        hr_ts = [t for t, _ in hr]
        act_ts = [s[0] for s in act]

        last_t = hr_ts[-1]
        span = last_t - hr_ts[0]
        n_epochs = self.smoothing_epochs if (smooth and self.hmm) else 1
        # only step back over epochs we actually have history for
        n_epochs = max(1, min(n_epochs, int(span // EPOCH_S) + 1))
        epoch_ends = [last_t - EPOCH_S * k for k in range(n_epochs - 1, -1, -1)]

        # causal, incrementally-sorted "night so far" distributions (matches training)
        hr_sorted: List[float] = []
        act_sorted: List[float] = []
        hi = 0
        ai = 0
        emissions: List[List[float]] = []
        raw_last: List[float] = []
        p_wake_last = 0.0

        for end in epoch_ends:
            while hi < len(hr) and hr_ts[hi] <= end:
                bisect.insort(hr_sorted, hr[hi][1])
                hi += 1
            while ai < len(act) and act_ts[ai] <= end:
                bisect.insort(act_sorted, float(act[ai][1]))
                ai += 1
            if hi == 0:
                continue
            lo_hr = bisect.bisect_left(hr_ts, end - MAX_LOOKBACK_S)
            lo_act = bisect.bisect_left(act_ts, end - MAX_LOOKBACK_S) if act else 0
            back_min = (last_t - end) / 60.0
            if minutes_since_start is not None:
                mss = float(minutes_since_start) - back_min
            else:
                # no clock context: fall back to how much history we hold. (Deriving it
                # from epoch_end would use wall-clock epoch seconds — a nonsense value.)
                mss = (end - hr_ts[0]) / 60.0
            # may legitimately go negative for epochs before sleep onset, exactly as in
            # the training rows
            mso = (float(minutes_since_onset) - back_min
                   if minutes_since_onset is not None else None)
            feats = compute_features(
                hr[lo_hr:hi],
                act[lo_act:ai] if act else None,
                end,
                norm_stats=stats_from_sorted(hr_sorted, act_sorted),
                minutes_since_start=mss,
                minutes_since_onset=mso,
                include_activity=use_motion,
            )
            stage_raw = _ordered_stage_probs(stage_model, feats)
            p_wake_raw = _prob_of_class(wake_model.predict_proba(feats),
                                        wake_model.classes, 0)
            emissions.append(blend_emission(stage_raw, p_wake_raw))
            raw_last = stage_raw
            p_wake_last = p_wake_raw

        if not emissions:
            return None

        smoothed = False
        if smooth and self.hmm and len(emissions) >= 1:
            try:
                post = forward_filter(
                    emissions,
                    self.hmm["trans"],
                    self.hmm.get("start") or self.hmm["prior"],
                    # the heads are class-balanced, so their effective prior is uniform
                    self.hmm.get("emission_prior") or self.hmm["prior"],
                    float(self.hmm.get("temper", 1.0)),
                )
                smoothed = True
            except Exception:  # noqa: BLE001 — never let smoothing break the controller
                post = emissions[-1]
        else:
            post = emissions[-1]

        probs = {lbl: float(post[i]) for i, lbl in enumerate(STAGE4_LABELS)}
        p_wake = probs["wake"] if smoothed else float(p_wake_last)
        stage_label = max(STAGE4_LABELS, key=lambda l: probs[l])
        if p_wake >= 0.5:
            stage_label = "wake"
        confidence = max(0.0, min(1.0, probs[stage_label]))
        return StageEstimate(
            stage_label=stage_label,
            p_wake=float(max(0.0, min(1.0, p_wake))),
            confidence=float(confidence),
            probs=probs,
            source="model",
            smoothed=smoothed,
        )


def _ordered_stage_probs(model: _Forest, feats: Dict[str, float]) -> List[float]:
    """4-vector in wake/light/deep/rem order, regardless of the model's class ordering."""
    raw = model.predict_proba(feats)
    out = [0.0] * 4
    for cls_code, p in zip(model.classes, raw):
        if 0 <= cls_code < 4:
            out[cls_code] = float(p)
    s = sum(out)
    return [o / s for o in out] if s > 0 else [0.25] * 4


def _prob_of_class(probs: Sequence[float], classes: Sequence[int], target: int) -> float:
    for p, c in zip(probs, classes):
        if c == target:
            return float(p)
    return 0.0


__all__ = [
    "SleepStager",
    "StageEstimate",
    "blend_emission",
    "forward_filter",
    "STAGE4_LABELS",
    "FEATURE_NAMES_HR",
    "FEATURE_NAMES_HRMOTION",
]
