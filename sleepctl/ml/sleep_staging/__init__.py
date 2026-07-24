"""Wearable sleep-staging models trained on the PhysioNet sleep-accel dataset.

At inference time a Polar Verity Sense (HR) — optionally plus iPhone motion — feeds
trailing-window HR (and activity) samples into :class:`SleepStager`, which returns a
:class:`StageEstimate` (wake detection + 4-class sleep stage).

The runtime path (:mod:`features` + :mod:`infer`) is PURE standard library (json/math/
statistics) — no numpy/pandas. Training (:mod:`dataset` + :mod:`train`) uses numpy.
"""

from .infer import SleepStager, StageEstimate

__all__ = ["SleepStager", "StageEstimate"]
