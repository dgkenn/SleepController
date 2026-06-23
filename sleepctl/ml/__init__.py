"""Machine-learning module: feature export, per-outcome response models, and a
constrained optimizer that tailors the learnable SetpointProfile from logged data.

Pure-Python by default (no hard numpy/pandas dependency) so it runs anywhere; it will
use numpy/pandas if installed (the ``ml`` extra). The model is a ridge-regularized linear
response model per outcome; the recommender optimizes the setpoint to maximize a
priority-weighted objective, bounded and small-step, and is gated on data sufficiency.
"""
