"""Breathing read from respiratory sinus arrhythmia, as HRV-stager features.

Deep sleep breathes slowly and very regularly, REM irregularly, so the breathing pattern carried
by the beat stream is a deep-vs-REM signal the HR summaries lack. Pinned here: the estimate on
synthetic tachograms with KNOWN breathing (rate, regularity, depth, plus noise, dropouts and
ectopy); that older weight files keep loading and scoring exactly as before; and that training
rows and live inference compute the same numbers.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import shutil

import pytest

from sleepctl.ml.sleep_staging import infer
from sleepctl.ml.sleep_staging.features import (
    ACT_FEATURES_SCALEFREE, FEATURE_NAMES_HR, FEATURE_NAMES_HRMOTION, FEATURE_NAMES_HRV,
    HRV_RESP_KEYS, compute_features)
from sleepctl.ml.sleep_staging.hrv_features import (
    RESP_GAP_S, breath_cycles, hrv_features, respiration)


def _beats(minutes=5.0, hr=60.0, brpm=15.0, depth=40.0, noise=10.0, jitter=0.0, lf=0.0,
           gaps=(), ectopy=0.0, seed=0):
    """``(t, ibi_ms)`` beats whose interval is modulated by breathing at ``brpm``.

    The breathing phase is integrated, so ``jitter`` (the per-breath SD of the rate, as a
    fraction) is genuine breath-to-breath irregularity. ``gaps`` are dropouts (s), ``lf`` a
    0.1 Hz Mayer wave (ms), ``ectopy`` the fraction of beats replaced by a premature beat and
    its compensatory pause.
    """
    rnd = random.Random(seed)
    t, ph, out = 0.0, 0.0, []
    f = f0 = brpm / 60.0
    next_breath = 0.0
    while t < minutes * 60.0:
        if jitter and t >= next_breath:
            f = max(0.12, f0 * (1.0 + rnd.gauss(0.0, jitter)))
            next_breath = t + 1.0 / f
        v = (60000.0 / hr + depth * math.sin(ph) + lf * math.sin(2 * math.pi * 0.1 * t)
             + rnd.gauss(0.0, noise))
        if ectopy and rnd.random() < ectopy:
            pre = 0.65 * v
            beats = [(t, pre), (t + pre / 1000.0, 2.0 * v - pre)]
        else:
            beats = [(t, v)]
        for bt, bv in beats:
            if not any(a <= bt < b for a, b in gaps):
                out.append((bt, bv))
        dt = sum(bv for _, bv in beats) / 1000.0
        ph += 2 * math.pi * f * dt
        t += dt
    return out


def _feats(beats):
    return hrv_features([t for t, _ in beats], [v for _, v in beats])


# ------------------------------------------------------------------ the estimate itself
@pytest.mark.parametrize("brpm", [12.0, 15.0, 18.0, 20.0])
def test_both_estimators_recover_a_known_breathing_rate(brpm):
    f = _feats(_beats(brpm=brpm))
    assert abs(f["ibi_resp_rate"] - brpm) < 0.3          # spectral RSA peak
    assert abs(f["ibi_breath_rate"] - brpm) < 0.5        # cycle counting
    assert f["ibi_resp_conc"] > 0.85
    assert f["ibi_breath_cv"] < 0.12
    assert f["ibi_breath_cov"] > 0.9


@pytest.mark.parametrize("hr", [48.0, 75.0])
def test_rate_holds_across_heart_rates(hr):
    """Beats sample the breathing: at 48 bpm an 18/min breath gets under three of them."""
    f = _feats(_beats(hr=hr, brpm=18.0, seed=2))
    assert abs(f["ibi_resp_rate"] - 18.0) < 0.3
    assert abs(f["ibi_breath_rate"] - 18.0) < 0.6


def test_a_mayer_wave_is_not_read_as_breathing():
    """0.1 Hz baroreflex oscillations often carry most of the tachogram's power; the peak must
    stay on the breathing, not slide down to the LF edge (the trap respiration.py documents)."""
    f = _feats(_beats(lf=60.0, seed=4))
    assert abs(f["ibi_resp_rate"] - 15.0) < 0.3
    assert abs(f["ibi_breath_rate"] - 15.0) < 0.5
    assert f["ibi_breath_cv"] < 0.15


def test_dropouts_and_ectopy_leave_the_estimate_intact():
    clean = _feats(_beats(seed=5))
    gappy = _feats(_beats(gaps=((60, 72), (150, 175), (220, 240)), seed=5))
    ectopic = _feats(_beats(ectopy=0.03, seed=5))
    for f in (gappy, ectopic):
        assert abs(f["ibi_resp_rate"] - 15.0) < 0.3
        assert abs(f["ibi_breath_rate"] - 15.0) < 0.5
        assert f["ibi_breath_cv"] < 0.12
        assert abs(f["ibi_rsa_amp"] - clean["ibi_rsa_amp"]) < 0.15 * clean["ibi_rsa_amp"]
    # ~57 s of dropout in a 300 s window: coverage reports it rather than inventing breaths
    assert gappy["ibi_breath_cov"] < clean["ibi_breath_cov"] - 0.1


def test_live_batch_stamped_beats_keep_the_rate():
    """Live beats are stamped per POSTED batch and walked back by their own lengths
    (bridge.recent_rr_intervals), so batch latency jitters the beat clock. The rate must hold;
    the cycle CV floor rises (0.04 -> ~0.08 here) but stays far below irregular breathing."""
    rnd = random.Random(14)
    beats, out = _beats(seed=14), []
    for i in range(0, len(beats), 5):
        chunk = beats[i:i + 5]
        t_post = chunk[-1][0] + chunk[-1][1] / 1000.0 + max(0.0, rnd.gauss(0.4, 0.25))
        back = sum(v for _, v in chunk)
        for _, v in chunk:
            back -= v
            out.append((t_post - back / 1000.0, v))
    f = _feats(sorted(out))
    assert abs(f["ibi_resp_rate"] - 15.0) < 0.3
    assert abs(f["ibi_breath_rate"] - 15.0) < 0.5
    assert f["ibi_breath_cv"] < 0.12


def test_no_breath_is_counted_across_a_gap():
    beats = _beats(gaps=((100, 130),), seed=6)
    cycles = breath_cycles([t for t, _ in beats], [v for _, v in beats])
    assert cycles
    for t_end, dur, _amp in cycles:
        assert not (t_end - dur < 115 < t_end), "a cycle straddles the dropout"
    assert RESP_GAP_S < 30


def test_irregular_breathing_reads_irregular():
    reg = _feats(_beats(seed=3))
    irr = _feats(_beats(jitter=0.25, seed=3))
    assert irr["ibi_breath_cv"] > 2.5 * reg["ibi_breath_cv"]
    assert irr["ibi_resp_conc"] < reg["ibi_resp_conc"] - 0.2


def test_rsa_amplitude_tracks_modulation_depth():
    amps = [_feats(_beats(depth=d, noise=5.0, seed=7))["ibi_rsa_amp"] for d in (15, 30, 60)]
    assert amps[0] < amps[1] < amps[2]
    assert 1.6 < amps[2] / amps[1] < 2.4


def test_noise_without_breathing_has_no_concentrated_peak():
    f = _feats(_beats(depth=0.0, noise=20.0, seed=8))
    assert f.get("ibi_resp_conc", 0.0) < 0.5
    assert f["ibi_breath_cv"] > 0.25


def test_short_or_empty_windows_are_missing_not_zero():
    assert respiration([], []) == {}
    beats = _beats(minutes=0.25)
    assert respiration([t for t, _ in beats], [v for _, v in beats]) == {}


# ------------------------------------------------------------------ stager feature block
def _hr(minutes):
    return [(float(s), 60.0 + math.sin(s / 90.0)) for s in range(0, int(minutes * 60) + 1)]


def test_the_block_emits_every_window_and_its_deltas():
    beats = _beats(minutes=35.0, brpm=14.0, seed=9)
    hr = _hr(35.0)
    f = compute_features(hr, None, hr[-1][0], include_activity=False, ibi_samples=beats)
    assert all(n in f for n in FEATURE_NAMES_HRV)
    for tag in ("2", "5", "10"):
        assert abs(f[f"hrv_resp_rate_w{tag}"] - 14.0) < 0.5
        assert abs(f[f"hrv_breath_rate_w{tag}"] - 14.0) < 0.7
        assert f[f"hrv_rsa_amp_w{tag}"] > 20.0
    assert abs(f["hrv_resp_rate_d2_10"]) < 0.5
    assert all(math.isfinite(v) for v in f.values())


def test_the_block_reads_zero_when_beats_stop():
    beats = [b for b in _beats(minutes=35.0, seed=10) if b[0] < 20 * 60]
    hr = _hr(35.0)
    f = compute_features(hr, None, hr[-1][0], include_activity=False, ibi_samples=beats)
    for k, _src in HRV_RESP_KEYS:
        assert f[f"hrv_{k}_w10"] == 0.0


# ------------------------------------------------------------------ weight-file compatibility
#: the HRV block as it stood before the breathing columns: its length and a fingerprint.
#: Weight files trained then list exactly these, so they must stay computed, unrenamed.
LEGACY_HRV_N = 124
LEGACY_HRV_MD5 = "f9cdc1f78f0729f484ea769bc9ed99d8"


def test_the_legacy_hrv_columns_are_untouched_and_breathing_is_appended():
    legacy = FEATURE_NAMES_HRV[:LEGACY_HRV_N]
    assert hashlib.md5("|".join(legacy).encode()).hexdigest() == LEGACY_HRV_MD5
    added = FEATURE_NAMES_HRV[LEGACY_HRV_N:]
    assert len(added) == 3 * len(HRV_RESP_KEYS) + 3
    # hrv_-prefixed and disjoint from the HR / HR+motion vocabularies, so the bundled
    # BIDSleep / sleep-accel models (no beat data) can never project onto one
    assert all(n.startswith("hrv_") for n in added)
    assert set(added).isdisjoint(FEATURE_NAMES_HRMOTION)
    assert set(added) <= infer.KNOWN_FEATURES


def _forest(names, split_on, thr, classes):
    """One stump on ``split_on``: left leaf certain of classes[0], right of classes[-1]."""
    c = len(classes)
    left = [1.0 if k == 0 else 0.0 for k in range(c)]
    right = [1.0 if k == c - 1 else 0.0 for k in range(c)]
    return {"feature_names": list(names), "classes": list(classes),
            "trees": [{"f": [names.index(split_on), -1, -1], "t": [thr, 0.0, 0.0],
                       "l": [1, 0, 1], "r": [2, 0, 1], "v": left + right}]}


def _weights(tmp_path, names, split_on, thr):
    for name in os.listdir(infer.WEIGHTS_DIR):
        shutil.copy(os.path.join(infer.WEIGHTS_DIR, name), tmp_path / name)
    (tmp_path / "wake_hrvonly.json").write_text(json.dumps(_forest(names, split_on, thr, [0, 1])))
    (tmp_path / "stage4_hrvonly.json").write_text(
        json.dumps(_forest(names, split_on, thr, [0, 1, 2, 3])))
    return str(tmp_path)


def test_an_older_hrv_weight_file_loads_and_scores_unchanged(tmp_path):
    """A file listing only the pre-breathing vocabulary (as the box's installed DREAMT weights
    do) validates, projects by name, and never reads a breathing column."""
    legacy = FEATURE_NAMES_HR + FEATURE_NAMES_HRV[:LEGACY_HRV_N]
    stager = infer.SleepStager.load(_weights(tmp_path, legacy, "hrv_rmssd_w5", 1e9))
    assert stager._hrvonly_ok
    beats = _beats(minutes=35.0, seed=11)
    est = stager.predict(_hr(35.0), ibi_samples=beats, smooth=False)
    assert est.variant == "hrvonly" and est.stage_label == "wake"   # rmssd <= 1e9: left leaf
    # and a pre-breathing HR+HRV+motion list is still wholly known
    assert all(n in infer.KNOWN_FEATURES for n in legacy + ACT_FEATURES_SCALEFREE)


def test_a_new_weight_file_can_split_on_breathing(tmp_path):
    names = FEATURE_NAMES_HR + FEATURE_NAMES_HRV
    stager = infer.SleepStager.load(_weights(tmp_path, names, "hrv_resp_rate_w5", 10.0))
    est = stager.predict(_hr(35.0), ibi_samples=_beats(minutes=35.0, seed=12), smooth=False)
    assert est.variant == "hrvonly" and est.stage_label == "rem"     # 15/min > 10: right leaf


def test_the_bundled_hr_models_never_see_the_block():
    hr = _hr(35.0)
    f = compute_features(hr, None, hr[-1][0], include_activity=False)
    assert not any(k.startswith("hrv") for k in f)
    for name in ("wake_hr.json", "stage4_hr.json", "wake_hrmotion.json", "stage4_hrmotion.json"):
        with open(os.path.join(infer.WEIGHTS_DIR, name)) as fh:
            assert not any(n.startswith("hrv") for n in json.load(fh)["feature_names"])


# ------------------------------------------------------------------ train / live parity
def test_training_rows_and_live_inference_compute_identical_breathing(tmp_path, monkeypatch):
    """The dataset builder and SleepStager.predict must hand the model the same numbers for the
    same epoch; any drift would train on one feature and serve another."""
    from sleepctl.ml.sleep_staging.dataset import build_subject_rows

    minutes = 40
    beats = _beats(minutes=minutes, brpm=13.0, jitter=0.1, gaps=((900, 915),), ectopy=0.01,
                   seed=13)
    hr = _hr(minutes)
    (tmp_path / "S1_heartrate.txt").write_text("".join(f"{t!r},{v!r}\n" for t, v in hr))
    (tmp_path / "S1_ibi.txt").write_text("".join(f"{t!r},{v!r}\n" for t, v in beats))
    (tmp_path / "S1_labeled_sleep.txt").write_text(
        "".join(f"{30 * k} 2\n" for k in range(minutes * 2 - 1)))
    ds = build_subject_rows("S1", str(tmp_path), use_activity=False, use_ibi=True)
    row = ds.rows[-1]
    epoch_end = ds.times[-1] + 30.0

    w = tmp_path / "w"
    w.mkdir()
    stager = infer.SleepStager.load(
        _weights(w, FEATURE_NAMES_HR + FEATURE_NAMES_HRV, "hrv_breath_cv_w5", 0.5))
    seen = []
    real = infer.compute_features
    monkeypatch.setattr(infer, "compute_features",
                        lambda *a, **k: seen.append(real(*a, **k)) or seen[-1])
    est = stager.predict([s for s in hr if s[0] <= epoch_end], ibi_samples=beats, smooth=False)
    assert est is not None and est.variant == "hrvonly"
    live = seen[-1]
    breathing = FEATURE_NAMES_HRV[LEGACY_HRV_N:]
    assert row["hrv_breath_rate_w10"] > 0.0
    for n in FEATURE_NAMES_HRV:
        assert live[n] == row[n], n
    assert all(row[n] != 0.0 for n in breathing if "_d2_10" not in n)
