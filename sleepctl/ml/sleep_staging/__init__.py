"""Wearable sleep-staging models (v2) trained on the PhysioNet sleep-accel dataset.

At inference time a Polar Verity Sense (HR) — optionally plus a movement signal — feeds
trailing sample histories into :class:`SleepStager`, which returns a :class:`StageEstimate`
(wake detection + 4-class sleep stage, temporally smoothed).

v2 over v1: multi-scale trailing windows (2/5/10/30 min) with lag and delta features,
per-recording normalization (z-score and percentile rank against the night's own HR
distribution) so the model personalizes instead of relying on absolute bpm, real actigraphy
instead of step counts (expressed scale-free so a different sensor's units still work), tree
ensembles instead of logistic regression, and an online HMM forward filter so the stage the
thermal controller sees does not flap tick to tick.

The runtime path (:mod:`features` + :mod:`infer`) is PURE standard library (json/math/
statistics/bisect) — no numpy/pandas. Training (:mod:`dataset` + :mod:`train`) uses
numpy/sklearn.
"""

from .infer import SleepStager, StageEstimate

__all__ = ["SleepStager", "StageEstimate"]
