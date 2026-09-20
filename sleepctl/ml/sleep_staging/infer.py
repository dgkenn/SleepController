"""Runtime sleep-stage inference — PURE standard library (json/math/bisect), NO numpy.

Loads the compact JSON tree-ensemble weights bundled under ``weights/`` and scores trailing
HR (optionally + movement) samples streamed from a Polar Verity Sense. Feature computation
is shared verbatim with training via :mod:`features`, guaranteeing train/inference parity.

Model variants under ``weights/``:
  * HR-only       ``wake_hr.json``,        ``stage4_hr.json``        -- Verity Sense alone
  * HR + motion   ``wake_hrmotion.json``,  ``stage4_hrmotion.json``  -- + a movement signal
  * sparse HR     ``wake_hr_sparse.json``, ``stage4_hr_sparse.json`` -- trained on 1 sample/min
  * HR + HRV (+ scale-free motion)
                  ``wake_hrv.json``,       ``stage4_hrv.json``       -- beat intervals streaming
  * HR + HRV      ``wake_hrvonly.json``,   ``stage4_hrvonly.json``   -- beat intervals, no motion
  * ``hmm.json``  4x4 transition matrix + class order + start/prior distributions

The HRV variants are trained on the PhysioNet DREAMT corpus (``scripts/train_dreamt.py``)
and are OPTIONAL: when their files are absent nothing about the HR / HR+motion path changes.
When present, :meth:`SleepStager.predict` prefers them whenever the caller supplies a beat
interval history with at least :data:`MIN_IBI_FOR_HRV` intervals in the last 10 minutes,
and records the variant it used in :attr:`StageEstimate.variant`.

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
    FEATURE_NAMES_ALL,
    FEATURE_NAMES_HR,
    FEATURE_NAMES_HRMOTION,
    FEATURE_NAMES_HRMOTION_ABS,
    FEATURE_NAMES_HRV,
    HRV_NORM_KEYS,
    MAX_LOOKBACK_S,
    compute_features,
    feature_vector,
    hrv_bucket_summaries,
    stats_from_sorted,
)

#: every feature name :func:`features.compute_features` can emit. A weights file naming
#: anything outside this set was exported against a different feature version.
KNOWN_FEATURES = frozenset(FEATURE_NAMES_ALL)

#: fewest beat intervals in the trailing 10 minutes before an HRV variant is preferred over
#: the HR / HR+motion models. 10 min at 40-100 bpm is 400-1000 beats, so 200 means the PPI
#: stream is genuinely flowing (not a stale or one-off batch) and every HRV window from
#: 2 to 10 min is characterised, while a dropout-riddled stretch falls back to HR.
MIN_IBI_FOR_HRV = 200
HRV_RECENT_WINDOW_S = 600.0

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
    #: which weights scored this estimate: "hr", "hrmotion", "hrv" (HR + HRV + scale-free
    #: motion) or "hrvonly" (HR + HRV)
    variant: str = "hr"


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


def validate_forest_dict(d: object) -> bool:
    """Is this JSON a self-consistent forest for the *current* feature vocabulary?

    :func:`features.feature_vector` imputes unknown names with 0.0, so a weights file
    exported against an older feature set would otherwise be scored silently — every column
    zero — and return confident garbage. The controller treats a returned estimate as
    authoritative for onset and all maintenance-time steering, so "unavailable" is far
    safer than "confidently wrong": anything inconsistent is rejected here.
    """
    try:
        if not isinstance(d, dict):
            return False
        names = d.get("feature_names")
        classes = d.get("classes")
        trees = d.get("trees")
        if not isinstance(names, list) or not names:
            return False
        if not isinstance(classes, list) or not classes:
            return False
        if not isinstance(trees, list) or not trees:
            return False
        # every declared feature must exist in the current vocabulary
        if not all(isinstance(n, str) and n in KNOWN_FEATURES for n in names):
            return False
        n_feat = len(names)
        n_cls = len(classes)
        for tr in trees:
            f = tr["f"]
            th = tr["t"]
            left = tr["l"]
            right = tr["r"]
            v = tr["v"]
            n_nodes = len(f)
            if n_nodes == 0 or not (len(th) == len(left) == len(right) == n_nodes):
                return False
            n_leaves = 0
            for i in range(n_nodes):
                fi = f[i]
                if fi < 0:                      # leaf: l[] indexes the flat value array
                    if not (0 <= left[i]):
                        return False
                    n_leaves = max(n_leaves, left[i] + 1)
                else:                           # split: feature index must be in range
                    if fi >= n_feat:
                        return False
                    if not (0 <= left[i] < n_nodes and 0 <= right[i] < n_nodes):
                        return False
            if len(v) != n_leaves * n_cls:
                return False
        return True
    except Exception:  # noqa: BLE001 — malformed structure of any shape
        return False


def _load_model(path: str) -> Optional[_Forest]:
    d = _load_json(path)
    if not validate_forest_dict(d):
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
        wake_hrv: Optional[_Forest] = None,
        stage4_hrv: Optional[_Forest] = None,
        wake_hrvonly: Optional[_Forest] = None,
        stage4_hrvonly: Optional[_Forest] = None,
        min_ibi_for_hrv: int = MIN_IBI_FOR_HRV,
    ) -> None:
        self.wake_hr = wake_hr
        self.stage4_hr = stage4_hr
        self.wake_hrmotion = wake_hrmotion
        self.stage4_hrmotion = stage4_hrmotion
        self.wake_hrv = wake_hrv
        self.stage4_hrv = stage4_hrv
        self.wake_hrvonly = wake_hrvonly
        self.stage4_hrvonly = stage4_hrvonly
        self.hmm = hmm
        self.smoothing_epochs = max(1, int(smoothing_epochs))
        self.min_ibi_for_hrv = max(1, int(min_ibi_for_hrv))
        self._hr_ok = wake_hr is not None and stage4_hr is not None
        self._hrmotion_ok = wake_hrmotion is not None and stage4_hrmotion is not None
        self._hrv_ok = wake_hrv is not None and stage4_hrv is not None
        self._hrvonly_ok = wake_hrvonly is not None and stage4_hrvonly is not None
        self.available = (self._hr_ok or self._hrmotion_ok
                          or self._hrv_ok or self._hrvonly_ok)

    @property
    def hrv_available(self) -> bool:
        """True when at least one beat-interval (HRV) variant is loaded."""
        return self._hrv_ok or self._hrvonly_ok

    def set_wake_bias(self, bias: float) -> None:
        self.wake_bias = max(0.5, min(2.0, float(bias or 1.0)))

    def set_personal_hmm(self, trans, prior=None) -> None:
        """Replace the smoothing model's transitions (and prior) with a personalised blend
        (see learning.hypnogram_priors). No-op when no HMM is bundled."""
        if not self.hmm or not trans:
            return
        hmm = dict(self.hmm)
        hmm["trans"] = [list(map(float, r)) for r in trans]
        if prior:
            hmm["prior"] = list(map(float, prior))
        self.hmm = hmm

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
            # optional DREAMT-trained beat-interval variants (absent until trained locally)
            wake_hrv=_load_model(os.path.join(weights_dir, "wake_hrv.json")),
            stage4_hrv=_load_model(os.path.join(weights_dir, "stage4_hrv.json")),
            wake_hrvonly=_load_model(os.path.join(weights_dir, "wake_hrvonly.json")),
            stage4_hrvonly=_load_model(os.path.join(weights_dir, "stage4_hrvonly.json")),
        )

    def select_variant(self, has_activity: bool, n_recent_ibi: int) -> Optional[str]:
        """Which bundled variant :meth:`predict` will score with.

        Preference order: an HRV variant when enough beat intervals are streaming (the
        +motion one when a movement series is present, else HR+HRV), otherwise HR+motion
        when a movement series is present, otherwise HR-only. Each step falls through to
        the next when its weights are not bundled, so the answer is ``None`` only when
        nothing is loaded at all.
        """
        if n_recent_ibi >= self.min_ibi_for_hrv:
            if has_activity and self._hrv_ok:
                return "hrv"
            if self._hrvonly_ok:
                return "hrvonly"
            if self._hrv_ok:
                return "hrv"  # only the +motion HRV variant bundled: its motion block reads empty
        if has_activity and self._hrmotion_ok:
            return "hrmotion"
        if self._hr_ok:
            return "hr"
        if self._hrmotion_ok:
            return "hrmotion"
        return None

    # ------------------------------------------------------------------ public interface
    def predict(
        self,
        hr_samples: Optional[Sequence[Sample]],
        activity_samples: Optional[Sequence[Sequence[float]]] = None,
        minutes_since_start: Optional[float] = None,
        minutes_since_onset: Optional[float] = None,
        *,
        smooth: bool = True,
        ibi_samples: Optional[Sequence[Sample]] = None,
    ) -> Optional[StageEstimate]:
        """Stage estimate from trailing ``(t_seconds, value)`` sample histories.

        ``activity_samples`` may be ``(t, movement)`` — any monotone movement scale works,
        the motion features are expressed relative to this recording's own distribution —
        or the 6-tuple actigraphy form ``(t, pim, zcm, mad, std, pmax)``.

        ``ibi_samples`` is an optional ``(t_seconds, ibi_ms)`` beat-interval history (the
        Verity's PPI stream, e.g. ``frame.rr_history``). When an HRV variant is bundled and
        at least :data:`MIN_IBI_FOR_HRV` intervals fall in the trailing 10 minutes, that
        variant is scored instead (see :meth:`select_variant`); otherwise the intervals
        are ignored and the estimate is exactly what the HR / HR+motion path returns.
        """
        if not hr_samples or not self.available:
            return None

        hr = sorted(((float(t), float(v)) for t, v in hr_samples), key=lambda s: s[0])
        hr_ts = [t for t, _ in hr]
        last_t = hr_ts[-1]

        ibi: List[Sample] = []
        n_recent_ibi = 0
        if ibi_samples and (self._hrv_ok or self._hrvonly_ok):
            ibi = sorted(((float(t), float(v)) for t, v in ibi_samples), key=lambda s: s[0])
            lo = bisect.bisect_left([t for t, _ in ibi], last_t - HRV_RECENT_WINDOW_S)
            n_recent_ibi = len(ibi) - lo
        variant = self.select_variant(bool(activity_samples), n_recent_ibi)
        if variant is None:
            return None
        use_hrv = variant in ("hrv", "hrvonly")
        if variant == "hrv":
            wake_model, stage_model = self.wake_hrv, self.stage4_hrv
            use_motion = bool(activity_samples)
        elif variant == "hrvonly":
            wake_model, stage_model = self.wake_hrvonly, self.stage4_hrvonly
            use_motion = False
        elif variant == "hrmotion":
            wake_model, stage_model = self.wake_hrmotion, self.stage4_hrmotion
            use_motion = True  # motion block simply reads empty when no movement series
        else:
            wake_model, stage_model = self.wake_hr, self.stage4_hr
            use_motion = False
        if not use_hrv:
            ibi = []

        act: List[Sequence[float]] = []
        if use_motion and activity_samples:
            act = sorted((tuple(float(x) for x in s) for s in activity_samples),
                         key=lambda s: s[0])
        act_ts = [s[0] for s in act]
        ibi_ts = [t for t, _ in ibi]
        # per-night HRV distribution source (2 min buckets, known from their end time)
        buckets = hrv_bucket_summaries(ibi) if ibi else []

        span = last_t - hr_ts[0]
        n_epochs = self.smoothing_epochs if (smooth and self.hmm) else 1
        # only step back over epochs we actually have history for
        n_epochs = max(1, min(n_epochs, int(span // EPOCH_S) + 1))
        epoch_ends = [last_t - EPOCH_S * k for k in range(n_epochs - 1, -1, -1)]

        # causal, incrementally-sorted "night so far" distributions (matches training)
        hr_sorted: List[float] = []
        act_sorted: List[float] = []
        hrv_sorted: Dict[str, List[float]] = {k: [] for k, _src in HRV_NORM_KEYS}
        hi = 0
        ai = 0
        ii = 0
        bi = 0
        emissions: List[List[float]] = []
        p_wake_last = 0.0  # raw binary-head wake probability at the newest epoch

        for end in epoch_ends:
            while hi < len(hr) and hr_ts[hi] <= end:
                bisect.insort(hr_sorted, hr[hi][1])
                hi += 1
            while ai < len(act) and act_ts[ai] <= end:
                bisect.insort(act_sorted, float(act[ai][1]))
                ai += 1
            while ii < len(ibi) and ibi_ts[ii] <= end:
                ii += 1
            while bi < len(buckets) and buckets[bi][0] <= end:
                for k, _src in HRV_NORM_KEYS:
                    bisect.insort(hrv_sorted[k], buckets[bi][1][k])
                bi += 1
            if hi == 0:
                continue
            lo_hr = bisect.bisect_left(hr_ts, end - MAX_LOOKBACK_S)
            lo_act = bisect.bisect_left(act_ts, end - MAX_LOOKBACK_S) if act else 0
            lo_ibi = bisect.bisect_left(ibi_ts, end - MAX_LOOKBACK_S) if ibi else 0
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
                norm_stats=stats_from_sorted(hr_sorted, act_sorted,
                                             hrv_sorted=hrv_sorted if use_hrv else None),
                minutes_since_start=mss,
                minutes_since_onset=mso,
                include_activity=use_motion,
                ibi_samples=ibi[lo_ibi:ii] if use_hrv else None,
                include_hrv=use_hrv,
            )
            stage_raw = _ordered_stage_probs(stage_model, feats)
            p_wake_raw = _prob_of_class(wake_model.predict_proba(feats),
                                        wake_model.classes, 0)
            # Personal wake calibration (learning.wake_truth): scale toward the rate that
            # would have caught this user's declared awakenings. 1.0 until learned.
            p_wake_raw = min(1.0, p_wake_raw * float(getattr(self, "wake_bias", 1.0) or 1.0))
            emissions.append(blend_emission(stage_raw, p_wake_raw))
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
            variant=variant,
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
    "MIN_IBI_FOR_HRV",
    "FEATURE_NAMES_HR",
    "FEATURE_NAMES_HRMOTION",
    "FEATURE_NAMES_HRV",
]
