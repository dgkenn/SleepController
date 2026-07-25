"""Tests for the v2 wearable sleep-staging module.

Covers the pure-stdlib runtime path (features + inference + the online HMM forward filter).
Training/dataset code (numpy/sklearn) is not exercised here; the bundled JSON weights are
treated as committed artifacts.
"""

from __future__ import annotations

import math
import random
import subprocess
import sys

import pytest

from sleepctl.ml.sleep_staging.features import (
    FEATURE_NAMES_HR,
    FEATURE_NAMES_HRMOTION,
    compute_features,
    compute_norm_stats,
    feature_vector,
    percentile_rank,
)
from sleepctl.ml.sleep_staging.infer import (
    SleepStager,
    StageEstimate,
    blend_emission,
    forward_filter,
)


# --------------------------------------------------------------------------- features
def _hr_series(base, n=240, step=10.0, jitter=0.0, seed=0):
    rnd = random.Random(seed)
    return [(i * step, base + (rnd.uniform(-jitter, jitter) if jitter else 0.0))
            for i in range(n)]


def test_features_deterministic_and_lengths():
    hr = _hr_series(58.0, jitter=2.0, seed=1)
    act = [(i * 30.0, 0.0) for i in range(80)]
    ns = compute_norm_stats(hr, act)
    kw = dict(norm_stats=ns, minutes_since_start=120.0, minutes_since_onset=90.0)
    f1 = compute_features(hr, act, 2400.0, **kw)
    f2 = compute_features(hr, act, 2400.0, **kw)
    assert f1 == f2  # deterministic

    v_hr = feature_vector(f1, FEATURE_NAMES_HR)
    v_hrm = feature_vector(f1, FEATURE_NAMES_HRMOTION)
    assert len(v_hr) == len(FEATURE_NAMES_HR)
    assert len(v_hrm) == len(FEATURE_NAMES_HRMOTION)
    assert len(FEATURE_NAMES_HRMOTION) > len(FEATURE_NAMES_HR)
    assert FEATURE_NAMES_HRMOTION[:len(FEATURE_NAMES_HR)] == FEATURE_NAMES_HR
    assert all(isinstance(x, float) and math.isfinite(x) for x in v_hrm)


def test_features_hr_only_when_activity_none():
    hr = _hr_series(60.0, seed=2)
    f = compute_features(hr, None, 2400.0, norm_stats=compute_norm_stats(hr),
                         include_activity=True)
    assert f["act_present"] == 0.0
    assert f["act_rank_w5"] == 0.0


def test_features_hr_stats_correct():
    hr = [(i * 10.0, 60.0) for i in range(200)]  # flat 60 bpm
    f = compute_features(hr, None, 2000.0, norm_stats=compute_norm_stats(hr))
    assert f["hr_mean_w5"] == pytest.approx(60.0)
    assert f["hr_std_w5"] == pytest.approx(0.0)
    assert f["hr_range_w10"] == pytest.approx(0.0)
    assert f["hr_slope_w10"] == pytest.approx(0.0, abs=1e-9)
    assert f["hrn_minus_p50"] == pytest.approx(0.0)


def test_multiscale_and_lag_features_track_a_step_change():
    # 40 bpm for the first 20 min, then 70 bpm for the last 5 min
    hr = [(t * 10.0, 40.0 if t * 10.0 < 1200.0 else 70.0) for t in range(180)]
    f = compute_features(hr, None, 1790.0, norm_stats=compute_norm_stats(hr))
    assert f["hr_mean_w2"] > f["hr_mean_w30"]        # short window sees the new level
    assert f["hr_d20"] > 20.0                        # now minus 20 min ago
    assert f["hr_mean5_minus_min30"] > 20.0
    assert f["hrn_rank5"] > 0.5                      # high within its own night


def test_percentile_rank_monotone():
    grid = [float(i) for i in range(41)]
    assert percentile_rank(grid, -5.0) == 0.0
    assert percentile_rank(grid, 100.0) == 1.0
    assert percentile_rank(grid, 20.0) == pytest.approx(0.5, abs=0.02)


def test_activity_features_are_scale_free():
    """Rescaling the movement signal must not change the shipped motion features."""
    rnd = random.Random(11)
    act = [(i * 30.0, rnd.choice([0.0, 0.0, 0.0, 3.0, 25.0])) for i in range(120)]
    act_scaled = [(t, v * 0.004) for t, v in act]  # counts -> a 0..0.1 movement index
    hr = _hr_series(56.0, jitter=1.0, seed=3)
    f_a = compute_features(hr, act, 3000.0, norm_stats=compute_norm_stats(hr, act))
    f_b = compute_features(hr, act_scaled, 3000.0,
                           norm_stats=compute_norm_stats(hr, act_scaled))
    scale_free = [n for n in FEATURE_NAMES_HRMOTION if n.startswith(("act_", "actn_"))]
    for name in scale_free:
        assert f_a[name] == pytest.approx(f_b[name], rel=1e-6, abs=1e-9), name


# --------------------------------------------------------------------------- HMM math
def test_blend_emission_normalized():
    e = blend_emission([0.1, 0.5, 0.2, 0.2], 0.9)
    assert sum(e) == pytest.approx(1.0)
    assert e[0] > 0.4  # the wake head dominates the wake mass


def test_forward_filter_is_stateless_and_normalized():
    trans = [[0.9, 0.05, 0.03, 0.02],
             [0.05, 0.85, 0.05, 0.05],
             [0.03, 0.07, 0.90, 0.00],
             [0.03, 0.07, 0.00, 0.90]]
    start = prior = [0.25] * 4
    em = [[0.1, 0.6, 0.2, 0.1]] * 5
    a = forward_filter(em, trans, start, prior, 0.5)
    b = forward_filter(em, trans, start, prior, 0.5)
    assert a == b                       # no hidden state
    assert sum(a) == pytest.approx(1.0)
    assert a.index(max(a)) == 1


# --------------------------------------------------------------------------- inference
def test_stager_loads_and_available():
    stager = SleepStager.load()
    assert stager.available is True
    assert stager.hmm is not None


def _awake_pattern(minutes=40):
    rnd = random.Random(7)
    n = int(minutes * 60 / 10)
    hr = [(i * 10.0, 72.0 + rnd.uniform(-8, 8)) for i in range(n)]
    act = [(i * 30.0, 30.0 + rnd.uniform(0, 20)) for i in range(int(minutes * 2))]
    return hr, act


def _asleep_pattern(minutes=40):
    rnd = random.Random(9)
    n = int(minutes * 60 / 10)
    # settled: low, flat HR after an elevated first few minutes (so the night-so-far
    # distribution has some spread, as it does in a real recording)
    hr = [(i * 10.0, (70.0 if i < 30 else 52.0) + rnd.uniform(-1, 1)) for i in range(n)]
    act = [(i * 30.0, 20.0 if i < 10 else 0.0) for i in range(int(minutes * 2))]
    return hr, act


def test_predict_wake_for_awake_pattern():
    stager = SleepStager.load()
    hr, act = _awake_pattern()
    est = stager.predict(hr, act, minutes_since_start=5.0, minutes_since_onset=0.0)
    assert isinstance(est, StageEstimate)
    assert est.stage_label == "wake"
    assert est.p_wake >= 0.5
    assert est.source == "model"


def test_predict_sleep_for_asleep_pattern():
    stager = SleepStager.load()
    hr, act = _asleep_pattern()
    est = stager.predict(hr, act, minutes_since_start=180.0, minutes_since_onset=150.0)
    assert isinstance(est, StageEstimate)
    assert est.stage_label in ("light", "deep", "rem")
    assert est.p_wake < 0.5


def test_predict_hr_only_path_valid():
    stager = SleepStager.load()
    hr, _ = _asleep_pattern()
    est = stager.predict(hr, activity_samples=None, minutes_since_start=180.0,
                         minutes_since_onset=150.0)
    assert isinstance(est, StageEstimate)
    assert est.stage_label in ("wake", "light", "deep", "rem")
    assert 0.0 <= est.p_wake <= 1.0


def test_probs_sum_to_one_and_confidence_range():
    stager = SleepStager.load()
    hr, act = _asleep_pattern()
    est = stager.predict(hr, act, minutes_since_start=180.0, minutes_since_onset=150.0)
    assert abs(sum(est.probs.values()) - 1.0) < 1e-6
    assert set(est.probs.keys()) == {"wake", "light", "deep", "rem"}
    assert 0.0 <= est.confidence <= 1.0
    assert est.confidence == pytest.approx(est.probs[est.stage_label])
    assert est.smoothed is True


def test_predict_none_for_empty_hr():
    stager = SleepStager.load()
    assert stager.predict(None) is None
    assert stager.predict([]) is None


def test_predict_is_deterministic():
    stager = SleepStager.load()
    hr, act = _asleep_pattern()
    a = stager.predict(hr, act, minutes_since_start=120.0)
    b = stager.predict(hr, act, minutes_since_start=120.0)
    assert (a.stage_label, a.probs) == (b.stage_label, b.probs)


# ------------------------------------------------------------------ smoothing reduces flap
def _alternating_noise_night(minutes=90, seed=5):
    """HR that flips between sleep-like and wake-like every couple of minutes."""
    rnd = random.Random(seed)
    hr = []
    for i in range(int(minutes * 60 / 10)):
        t = i * 10.0
        hot = int(t // 120) % 2 == 0
        base = 68.0 if hot else 54.0
        hr.append((t, base + rnd.uniform(-4, 4)))
    return hr


def test_smoothing_reduces_label_flipflop():
    stager = SleepStager.load()
    hr = _alternating_noise_night()
    end = hr[-1][0]

    def labels(smooth):
        out = []
        # walk the night one 30 s epoch at a time, as the controller would
        for t in range(int(end) - 40 * 60, int(end) + 1, 30):
            hist = [s for s in hr if s[0] <= t]
            if len(hist) < 30:
                continue
            est = stager.predict(hist, None, minutes_since_start=t / 60.0, smooth=smooth)
            if est is not None:
                out.append(est.stage_label)
        return out

    rough = labels(False)
    smooth = labels(True)
    assert len(rough) == len(smooth) and len(smooth) > 20

    def changes(seq):
        return sum(1 for i in range(1, len(seq)) if seq[i] != seq[i - 1])

    assert changes(smooth) < changes(rough)


# --------------------------------------------------------------- pure-stdlib guarantee
def test_runtime_imports_without_numpy():
    """features.py + infer.py must run in the controller with numpy unavailable."""
    code = (
        "import sys\n"
        "class Block:\n"
        "    def find_module(self, name, path=None):\n"
        "        return self if name.split('.')[0] in ('numpy', 'pandas', 'sklearn') else None\n"
        "    def load_module(self, name):\n"
        "        raise ImportError('blocked: ' + name)\n"
        "sys.meta_path.insert(0, Block())\n"
        "for m in list(sys.modules):\n"
        "    if m.split('.')[0] in ('numpy', 'pandas', 'sklearn'):\n"
        "        del sys.modules[m]\n"
        "from sleepctl.ml.sleep_staging.infer import SleepStager\n"
        "s = SleepStager.load()\n"
        "assert s.available\n"
        "hr = [(i * 10.0, 54.0 + (i % 5)) for i in range(300)]\n"
        "est = s.predict(hr, None, minutes_since_start=100.0)\n"
        "assert est is not None and abs(sum(est.probs.values()) - 1.0) < 1e-6\n"
        "assert 'numpy' not in sys.modules\n"
        "print('OK')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
