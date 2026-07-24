"""Runtime sleep-stage inference — PURE standard library (json + math), NO numpy.

Loads the compact JSON logistic-regression weights bundled under ``weights/`` and scores
trailing-window HR (optionally + activity) samples streamed from a Polar Verity Sense
(and optionally iPhone motion). The feature computation is shared verbatim with training
via :mod:`features`, guaranteeing train/inference parity.

Two model variants exist:
  * HR-only        (wake_hr.json,        stage4_hr.json)        -- Verity Sense alone
  * HR + motion    (wake_hrmotion.json,  stage4_hrmotion.json)  -- + iPhone motion

Usage::

    stager = SleepStager.load()
    if stager.available:
        est = stager.predict(hr_samples, activity_samples, minutes_since_start=120)
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .features import (
    FEATURE_NAMES_HR,
    FEATURE_NAMES_HRMOTION,
    compute_features,
    feature_vector,
)

WEIGHTS_DIR = os.path.join(os.path.dirname(__file__), "weights")

STAGE4_LABELS = ["wake", "light", "deep", "rem"]

Sample = Tuple[float, float]


@dataclass
class StageEstimate:
    stage_label: str          # "wake" / "light" / "deep" / "rem"
    p_wake: float             # probability of wake, 0..1
    confidence: float         # winning class probability, 0..1
    probs: Dict[str, float]   # label -> probability
    source: str = "model"


@dataclass
class _LinearModel:
    """A standardized (multinomial or binary) logistic model loaded from JSON."""

    feature_names: List[str]
    mean: List[float]
    std: List[float]
    classes: List[int]
    coef: List[List[float]]   # [n_classes][n_features]
    intercept: List[float]    # [n_classes]

    @classmethod
    def from_dict(cls, d: dict) -> "_LinearModel":
        return cls(
            feature_names=list(d["feature_names"]),
            mean=[float(x) for x in d["mean"]],
            std=[float(x) for x in d["std"]],
            classes=[int(x) for x in d["classes"]],
            coef=[[float(x) for x in row] for row in d["coef"]],
            intercept=[float(x) for x in d["intercept"]],
        )

    def _standardize(self, x: Sequence[float]) -> List[float]:
        out = []
        for v, m, s in zip(x, self.mean, self.std):
            out.append((v - m) / s if s > 0 else 0.0)
        return out

    def predict_proba(self, feats: Dict[str, float]) -> List[float]:
        x = feature_vector(feats, self.feature_names)
        z = self._standardize(x)
        logits = []
        for k in range(len(self.coef)):
            acc = self.intercept[k]
            row = self.coef[k]
            for j in range(len(row)):
                acc += row[j] * z[j]
            logits.append(acc)
        if len(logits) == 1:
            # binary stored as a single logit -> [P(class0), P(class1)]
            p1 = 1.0 / (1.0 + math.exp(-logits[0]))
            return [1.0 - p1, p1]
        m = max(logits)
        exps = [math.exp(l - m) for l in logits]
        tot = sum(exps)
        return [e / tot for e in exps]


def _load_model(path: str) -> Optional[_LinearModel]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as fh:
            return _LinearModel.from_dict(json.load(fh))
    except Exception:  # noqa: BLE001
        return None


class SleepStager:
    def __init__(
        self,
        wake_hr: Optional[_LinearModel] = None,
        stage4_hr: Optional[_LinearModel] = None,
        wake_hrmotion: Optional[_LinearModel] = None,
        stage4_hrmotion: Optional[_LinearModel] = None,
    ) -> None:
        self.wake_hr = wake_hr
        self.stage4_hr = stage4_hr
        self.wake_hrmotion = wake_hrmotion
        self.stage4_hrmotion = stage4_hrmotion
        # available iff at least one complete (wake + stage4) variant loaded
        self._hr_ok = wake_hr is not None and stage4_hr is not None
        self._hrmotion_ok = wake_hrmotion is not None and stage4_hrmotion is not None
        self.available = self._hr_ok or self._hrmotion_ok

    @classmethod
    def load(cls, weights_dir: str = WEIGHTS_DIR) -> "SleepStager":
        return cls(
            wake_hr=_load_model(os.path.join(weights_dir, "wake_hr.json")),
            stage4_hr=_load_model(os.path.join(weights_dir, "stage4_hr.json")),
            wake_hrmotion=_load_model(os.path.join(weights_dir, "wake_hrmotion.json")),
            stage4_hrmotion=_load_model(os.path.join(weights_dir, "stage4_hrmotion.json")),
        )

    def predict(
        self,
        hr_samples: Optional[Sequence[Sample]],
        activity_samples: Optional[Sequence[Sample]] = None,
        minutes_since_start: Optional[float] = None,
        minutes_since_onset: Optional[float] = None,
    ) -> Optional[StageEstimate]:
        if not hr_samples:
            return None
        if not self.available:
            return None

        use_motion = activity_samples is not None and self._hrmotion_ok
        if use_motion:
            wake_model = self.wake_hrmotion
            stage_model = self.stage4_hrmotion
        else:
            wake_model = self.wake_hr
            stage_model = self.stage4_hr
            if wake_model is None or stage_model is None:
                # requested HR-only but only motion models exist -> fall back to motion
                wake_model = self.wake_hrmotion
                stage_model = self.stage4_hrmotion
                use_motion = True

        # trailing window ends at the latest HR sample
        epoch_end = max(t for t, _ in hr_samples)
        feats = compute_features(
            hr_samples,
            activity_samples if use_motion else None,
            epoch_end,
            hr_baseline=self._infer_baseline(hr_samples),
            minutes_since_start=minutes_since_start,
            minutes_since_onset=minutes_since_onset,
            include_activity=use_motion,
        )

        wake_probs = wake_model.predict_proba(feats)
        # wake model classes order corresponds to model.classes; find index of sleep(1)/wake(0)
        p_wake = _prob_of_class(wake_probs, wake_model.classes, 0)

        stage_probs_raw = stage_model.predict_proba(feats)
        probs: Dict[str, float] = {}
        for cls_code, p in zip(stage_model.classes, stage_probs_raw):
            probs[STAGE4_LABELS[cls_code]] = p
        for lbl in STAGE4_LABELS:
            probs.setdefault(lbl, 0.0)

        # argmax stage, but force wake when the dedicated wake model says wake
        stage_label = max(STAGE4_LABELS, key=lambda l: probs[l])
        if p_wake >= 0.5:
            stage_label = "wake"

        confidence = probs[stage_label] if stage_label != "wake" else max(p_wake, probs["wake"])
        confidence = max(0.0, min(1.0, confidence))

        return StageEstimate(
            stage_label=stage_label,
            p_wake=float(p_wake),
            confidence=float(confidence),
            probs=probs,
            source="model",
        )

    @staticmethod
    def _infer_baseline(hr_samples: Sequence[Sample]) -> float:
        """20th-percentile baseline from whatever HR history we were handed."""
        vals = sorted(v for _, v in hr_samples)
        if not vals:
            return 0.0
        if len(vals) == 1:
            return vals[0]
        rank = 0.20 * (len(vals) - 1)
        lo = int(rank)
        hi = min(lo + 1, len(vals) - 1)
        frac = rank - lo
        return vals[lo] * (1 - frac) + vals[hi] * frac


def _prob_of_class(probs: List[float], classes: List[int], target: int) -> float:
    for p, c in zip(probs, classes):
        if c == target:
            return p
    return 0.0


__all__ = ["SleepStager", "StageEstimate", "FEATURE_NAMES_HR", "FEATURE_NAMES_HRMOTION"]
