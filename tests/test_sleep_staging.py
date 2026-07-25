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
    ACT_FEATURES_SCALEFREE,
    FEATURE_NAMES_HR,
    FEATURE_NAMES_HRMOTION,
    FEATURE_NAMES_HRMOTION_SCALEFREE,
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
    for name in ACT_FEATURES_SCALEFREE:
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
def _oscillating_night(period_min, amp, base=54.0, minutes=150, seed=5):
    """HR that swings around a settled-sleep baseline — the sort of noisy stretch that
    makes a memoryless classifier alternate labels epoch to epoch."""
    rnd = random.Random(seed)
    out = []
    n = int(minutes * 60 / 10)
    for i in range(n):
        t = i * 10.0
        phase = (t / 60.0) % period_min < (period_min / 2.0)
        v = base + (amp if phase else -amp) * 0.5 + rnd.uniform(-2.0, 2.0)
        out.append((t, v))
    return out


def _changes(seq):
    return sum(1 for i in range(1, len(seq)) if seq[i] != seq[i - 1])


def _walk_night(stager, hr, smooth, minutes_back=45):
    """Label the last ``minutes_back`` minutes one 30 s epoch at a time, as the controller
    would call us tick by tick."""
    end = hr[-1][0]
    out = []
    for t in range(int(end) - minutes_back * 60, int(end) + 1, 30):
        hist = [s for s in hr if s[0] <= t]
        if len(hist) < 60:
            continue
        est = stager.predict(hist, None, minutes_since_start=t / 60.0,
                             minutes_since_onset=t / 60.0 - 20.0, smooth=smooth)
        if est is not None:
            out.append(est.stage_label)
    return out


def test_smoothing_reduces_label_flipflop():
    """On an input the unsmoothed model is unstable on, the HMM must flap strictly less.

    The precondition assert matters: without it this test would silently pass whenever the
    unsmoothed sequence happened to be constant (0 changes vs 0 changes).
    """
    stager = SleepStager.load()
    rough = hr = None
    for period, amp in ((2, 8), (3, 10), (4, 12), (2, 14), (5, 16), (3, 20), (6, 24)):
        hr = _oscillating_night(period, amp)
        rough = _walk_night(stager, hr, smooth=False)
        if _changes(rough) >= 3:
            break
    assert len(rough) > 20
    assert _changes(rough) >= 3, "no probe input made the unsmoothed model flip-flop"

    smooth = _walk_night(stager, hr, smooth=True)
    assert len(smooth) == len(rough)
    assert _changes(smooth) < _changes(rough)


# ------------------------------------------------------- stale weights must not be scored
def _copy_weights(dest, mutate=None):
    """Copy the bundled weights into ``dest``, optionally mutating one file's JSON."""
    import json
    import os
    import shutil

    from sleepctl.ml.sleep_staging.infer import WEIGHTS_DIR

    for name in os.listdir(WEIGHTS_DIR):
        shutil.copy(os.path.join(WEIGHTS_DIR, name), os.path.join(dest, name))
    if mutate:
        for name, fn in mutate.items():
            path = os.path.join(dest, name)
            with open(path) as fh:
                d = json.load(fh)
            with open(path, "w") as fh:
                json.dump(fn(d), fh)
    return dest


def test_mismatched_feature_names_are_rejected(tmp_path):
    """A weights file naming features we no longer compute must NOT be loaded.

    feature_vector() imputes unknown names with 0.0, so a stale export would otherwise be
    scored as an all-zero row and return confident garbage.
    """
    def stale(d):
        d["feature_names"] = [f"legacy_feature_{i}" for i in range(len(d["feature_names"]))]
        return d

    w = _copy_weights(str(tmp_path), mutate={"stage4_hr.json": stale})
    stager = SleepStager.load(w)
    assert stager.stage4_hr is None
    assert stager._hr_ok is False


def test_structurally_broken_weights_are_rejected(tmp_path):
    def broken(d):
        d["trees"][0]["f"][0] = 10 ** 6  # feature index far out of range
        return d

    w = _copy_weights(str(tmp_path), mutate={"wake_hr.json": broken})
    stager = SleepStager.load(w)
    assert stager.wake_hr is None
    assert stager._hr_ok is False


def test_bundled_weights_match_current_feature_lists():
    import json
    import os

    from sleepctl.ml.sleep_staging.infer import WEIGHTS_DIR

    expected = {
        "wake_hr.json": [FEATURE_NAMES_HR],
        "stage4_hr.json": [FEATURE_NAMES_HR],
        "wake_hr_sparse.json": [FEATURE_NAMES_HR],
        "stage4_hr_sparse.json": [FEATURE_NAMES_HR],
        # the motion variant ships whichever of the two vocabularies wins grouped CV
        "wake_hrmotion.json": [FEATURE_NAMES_HRMOTION, FEATURE_NAMES_HRMOTION_SCALEFREE],
        "stage4_hrmotion.json": [FEATURE_NAMES_HRMOTION, FEATURE_NAMES_HRMOTION_SCALEFREE],
    }
    for name, allowed in expected.items():
        path = os.path.join(WEIGHTS_DIR, name)
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            got = json.load(fh)["feature_names"]
        assert any(got == list(a) for a in allowed), name


def test_unavailable_stager_returns_none(tmp_path):
    stager = SleepStager.load(str(tmp_path))  # empty dir: nothing to load
    assert stager.available is False
    hr = [(i * 10.0, 55.0) for i in range(100)]
    assert stager.predict(hr) is None


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
