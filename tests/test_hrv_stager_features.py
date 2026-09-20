"""The beat-interval feature block: pure stdlib, causal, and silent when no beats are given."""
import math
import random

from sleepctl.ml.sleep_staging.features import (FEATURE_NAMES_HR, FEATURE_NAMES_HRMOTION,
                                                FEATURE_NAMES_HRV, FEATURE_NAMES_HRV_MOTION,
                                                compute_features, compute_norm_stats,
                                                hrv_bucket_summaries)


def _beats(minutes=35, hr=60.0, sd_ms=40.0, seed=1):
    random.seed(seed)
    t, out = 0.0, []
    while t < minutes * 60.0:
        ibi = 60000.0 / hr + random.gauss(0, sd_ms)
        out.append((t, ibi))
        t += ibi / 1000.0
    return out


def _hr(minutes=35, hr=60.0):
    return [(float(s), hr + math.sin(s / 60.0)) for s in range(0, minutes * 60, 2)]


def test_no_beats_means_the_v2_feature_dict_exactly():
    hr = _hr()
    base = compute_features(hr, None, hr[-1][0], include_activity=False)
    assert set(base) >= set(FEATURE_NAMES_HR)
    assert not any(k.startswith("hrv") for k in base)


def test_the_hrv_block_is_emitted_and_normalised_when_beats_are_given():
    hr, ibi = _hr(), _beats()
    end = hr[-1][0]
    ns = compute_norm_stats(hr, None, ibi_samples=ibi)
    assert ns.get("hrv_n", 0) > 5
    f = compute_features(hr, None, end, norm_stats=ns, include_activity=False, ibi_samples=ibi)
    assert all(n in f for n in FEATURE_NAMES_HRV)
    assert f["hrv_present_w2"] == 1.0 and f["hrv_present_w30"] == 1.0
    assert abs(f["hrv_hr_w5"] - 60.0) < 3.0
    assert f["hrv_rmssd_w5"] > 20.0
    assert f["hrvn_present"] == 1.0 and 0.0 <= f["hrvn_rmssd_rank5"] <= 1.0
    assert all(math.isfinite(v) for v in f.values())


def test_a_change_in_variability_shows_in_the_cross_scale_delta():
    calm = _beats(minutes=30, sd_ms=15.0, seed=2)
    t0 = calm[-1][0]
    burst = [(t0 + t, v) for t, v in _beats(minutes=3, sd_ms=80.0, seed=3)]
    ibi = calm + burst
    hr = _hr(minutes=34)
    f = compute_features(hr, None, ibi[-1][0], include_activity=False, ibi_samples=ibi)
    assert f["hrv_rmssd_d2_10"] > 0.0        # last 2 min far more variable than last 10


def test_bucket_summaries_are_causal_and_keyed_by_end_time():
    ibi = _beats(minutes=10)
    b = hrv_bucket_summaries(ibi, bucket_s=120.0)
    assert len(b) >= 4
    ends = [e for e, _ in b]
    assert ends == sorted(ends) and all(e % 120.0 == 0 for e in ends)
    assert set(b[0][1]) == {"hr", "rmssd", "sd1_sd2", "lf_hf", "hf_nu"}


def test_variant_name_lists_are_disjoint_and_ordered():
    assert set(FEATURE_NAMES_HRV).isdisjoint(FEATURE_NAMES_HRMOTION)
    assert FEATURE_NAMES_HRV_MOTION[: len(FEATURE_NAMES_HR)] == FEATURE_NAMES_HR
    assert len(FEATURE_NAMES_HRV) > 60
