"""Breathing on an arm is a slow tilt on one axis; pick the most concentrated axis, and yield
nothing while the window holds gross movement."""
import math
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verity_forwarder as vf  # noqa: E402

FS = 52.0


def _bufs(breath_axis: int, brpm: float = 14.0, seconds: float = 120.0):
    n = int(seconds * FS)
    mag = deque(maxlen=n)
    axes = [deque(maxlen=n) for _ in range(3)]
    for i in range(n):
        t = i / FS
        tilt = 0.02 * math.sin(2 * math.pi * (brpm / 60.0) * t)
        base = [0.0, 0.0, 1.0]
        base[breath_axis] += tilt
        for k in range(3):
            axes[k].append(base[k])
        mag.append(math.sqrt(sum(v * v for v in base)))
    return mag, axes


def test_the_breathing_axis_is_found_when_the_magnitude_cannot_see_it():
    mag, axes = _bufs(breath_axis=0)          # tilt on x, orthogonal to gravity: |a| barely moves
    est, axis = vf._best_respiration(mag, axes, deque([0.2, 0.3]), FS)
    assert est is not None and axis == "x"
    assert abs(est.breaths_per_min - 14.0) <= 1.0


def test_gross_movement_in_the_window_yields_no_rate():
    mag, axes = _bufs(breath_axis=0)
    est, axis = vf._best_respiration(mag, axes, deque([0.2, 6.0, 0.3]), FS)
    assert est is None and axis is None


def test_a_slow_roll_across_the_window_yields_no_rate():
    mag, axes = _bufs(breath_axis=0)
    rolling = deque([(0.0, 0.0, 1.0), (0.0, 0.2, 0.98), (0.0, 0.5, 0.87)])   # ~30 degrees
    est, axis = vf._best_respiration(mag, axes, deque([0.2, 0.3]), FS, rolling)
    assert est is None
    steady = deque([(0.0, 0.0, 1.0), (0.01, 0.0, 1.0)])
    est, axis = vf._best_respiration(mag, axes, deque([0.2, 0.3]), FS, steady)
    assert est is not None


def test_gravity_is_the_batch_mean_in_g():
    assert vf._gravity([(0, 0, 1000), (0, 0, 980)]) == (0.0, 0.0, 0.99)
    assert vf._gravity([]) is None
    assert abs(vf._angle_deg((0, 0, 1), (0, 1, 0)) - 90.0) < 1e-6
