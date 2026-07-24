"""Tests for the wearable sleep-staging module.

Covers the pure-stdlib runtime path (features + inference). Training/dataset code
(numpy) is not exercised here; the bundled JSON weights are treated as committed
artifacts.
"""

from __future__ import annotations

import math
import random

import pytest

from sleepctl.ml.sleep_staging.features import (
    FEATURE_NAMES_HR,
    FEATURE_NAMES_HRMOTION,
    compute_features,
    feature_vector,
)
from sleepctl.ml.sleep_staging.infer import SleepStager, StageEstimate


# --------------------------------------------------------------------------- features
def _hr_series(base, n=60, step=10.0, jitter=0.0, seed=0):
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        t = i * step
        v = base + (rnd.uniform(-jitter, jitter) if jitter else 0.0)
        out.append((t, v))
    return out


def test_features_deterministic_and_lengths():
    hr = _hr_series(58.0, jitter=2.0, seed=1)
    act = [(i * 30.0, 0.0) for i in range(20)]
    f1 = compute_features(hr, act, epoch_end_s=600.0, hr_baseline=55.0,
                          minutes_since_start=120.0, minutes_since_onset=90.0)
    f2 = compute_features(hr, act, epoch_end_s=600.0, hr_baseline=55.0,
                          minutes_since_start=120.0, minutes_since_onset=90.0)
    assert f1 == f2  # deterministic

    v_hr = feature_vector(f1, FEATURE_NAMES_HR)
    v_hrm = feature_vector(f1, FEATURE_NAMES_HRMOTION)
    assert len(v_hr) == len(FEATURE_NAMES_HR)
    assert len(v_hrm) == len(FEATURE_NAMES_HRMOTION)
    assert len(FEATURE_NAMES_HRMOTION) > len(FEATURE_NAMES_HR)
    # every value is a finite float
    assert all(isinstance(x, float) and math.isfinite(x) for x in v_hrm)


def test_features_hr_only_when_activity_none_flag_zero():
    hr = _hr_series(60.0, seed=2)
    f = compute_features(hr, None, epoch_end_s=600.0, hr_baseline=55.0,
                         include_activity=True)
    assert f["activity_present"] == 0.0
    assert f["activity_window_sum"] == 0.0


def test_features_hr_stats_correct():
    # flat HR of 60 over the window -> mean 60, std 0, range 0
    hr = [(i * 10.0, 60.0) for i in range(30)]
    f = compute_features(hr, None, epoch_end_s=300.0, hr_baseline=55.0)
    assert f["hr_mean"] == pytest.approx(60.0)
    assert f["hr_std"] == pytest.approx(0.0)
    assert f["hr_range"] == pytest.approx(0.0)
    assert f["hr_minus_baseline"] == pytest.approx(5.0)


# --------------------------------------------------------------------------- inference
def test_stager_loads_and_available():
    stager = SleepStager.load()
    assert stager.available is True


def _awake_pattern():
    # elevated, variable HR + high activity
    rnd = random.Random(7)
    hr = [(i * 10.0, 72.0 + rnd.uniform(-8, 8)) for i in range(60)]
    act = [(i * 30.0, 30.0 + rnd.uniform(0, 20)) for i in range(20)]
    return hr, act


def _asleep_pattern():
    # low, flat HR + no activity
    hr = [(i * 10.0, 52.0) for i in range(60)]
    act = [(i * 30.0, 0.0) for i in range(20)]
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


def test_predict_none_for_empty_hr():
    stager = SleepStager.load()
    assert stager.predict(None) is None
    assert stager.predict([]) is None
