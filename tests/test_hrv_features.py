"""HRV features from RAW inter-beat intervals.

The deployed stager consumes summary statistics of a HEART-RATE series and scores 4-class kappa
0.436 with wake recall 0.413. Topalidis et al. (Sensors 2023) reached kappa 0.75 on the SAME
Polar Verity Sense, at home, on a majority-poor-sleeper cohort, from inter-beat intervals alone.
We already persist those intervals and never fed them to the stager.

Measured on this user's own labelled night, 5-min windows (n=40 awake, 55 asleep), these
features separate the states at Cohen's d up to 1.36 (pNN20) -- the best discriminator found in
the project, and one that exists only in beat-to-beat data.
"""

from __future__ import annotations

import math

from sleepctl.ml.sleep_staging.hrv_features import (
    IBI_MAX_MS, IBI_MIN_MS, clean_ibis, hrv_features, sample_entropy,
)


def _series(mean_ms=880.0, jitter=8.0, n=300):
    """A plausible resting tachogram with mild beat-to-beat jitter."""
    return [mean_ms + (jitter if i % 2 else -jitter) + 3.0 * math.sin(i / 7.0)
            for i in range(n)]


def _times(ibis):
    t, out = 0.0, []
    for x in ibis:
        out.append(t)
        t += x / 1000.0
    return out


def test_clean_drops_impossible_intervals():
    """300-2000 ms is 200-30 bpm; outside that is an artifact, not a heartbeat."""
    raw = [880.0, 120.0, 890.0, 3000.0, 875.0]
    out = clean_ibis(raw)
    assert all(IBI_MIN_MS <= x <= IBI_MAX_MS for x in out)
    assert 120.0 not in out and 3000.0 not in out


def test_clean_drops_beat_to_beat_jumps():
    """A PPG that loses the pulse merges or splits beats, producing jumps real sinus rhythm
    never makes. Observed live: [352, 1245, 428, 714, 290, 303, 579] -- 170->48->140 bpm."""
    raw = [880.0, 890.0, 1245.0, 885.0]      # the 1245 is a merged beat
    assert 1245.0 not in clean_ibis(raw)


def test_artifact_run_cannot_drag_the_reference():
    """Each interval is compared to the last KEPT one, so a run of artifacts cannot walk the
    filter away from physiology one small step at a time."""
    raw = [880.0] + [880.0 * (1.15 ** k) for k in range(1, 6)]   # each +15% on the last
    out = clean_ibis(raw)
    assert max(out) < 880.0 * 1.25, f"artifact run walked the reference up: {out}"


def test_time_domain_recovers_known_values():
    ibis = _series()
    f = hrv_features(_times(ibis), ibis)
    assert abs(f["ibi_mean"] - 880.0) < 5.0
    assert abs(f["hr_from_ibi"] - 60000.0 / 880.0) < 0.5
    # alternating +-8 ms -> successive differences ~16 ms, so pnn20 low and pnn50 ~0
    assert 0.0 <= f["ibi_pnn50"] < 0.1
    assert f["ibi_rmssd"] > 0.0


def test_pnn20_rises_with_beat_to_beat_variability():
    """pNN20 was the strongest separator measured (d=1.36, 0.32 awake vs 0.45 asleep): more
    successive-interval variability once genuinely asleep."""
    quiet = hrv_features(*_flip(_series(jitter=5.0)))
    varied = hrv_features(*_flip(_series(jitter=30.0)))
    assert varied["ibi_pnn20"] > quiet["ibi_pnn20"]
    assert varied["ibi_rmssd"] > quiet["ibi_rmssd"]


def _flip(ibis):
    return _times(ibis), ibis


def test_poincare_geometry_is_consistent():
    ibis = _series()
    f = hrv_features(_times(ibis), ibis)
    assert f["ibi_sd1"] > 0 and f["ibi_sd2"] > 0
    assert 0 < f["ibi_sd1_sd2"] < 5
    assert f["ibi_ellipse_area"] > 0


def test_frequency_bands_are_present_and_normalised():
    ibis = _series(n=600)
    f = hrv_features(_times(ibis), ibis)
    assert f["ibi_total_power"] > 0
    assert abs((f["ibi_lf_nu"] + f["ibi_hf_nu"]) - 1.0) < 1e-6


def test_sample_entropy_lower_for_regular_series():
    regular = [880.0 + (2.0 if i % 2 else -2.0) for i in range(200)]
    import random
    rng = random.Random(0)
    noisy = [880.0 + rng.uniform(-60, 60) for _ in range(200)]
    assert sample_entropy(regular) < sample_entropy(noisy)


def test_short_or_contaminated_windows_return_nothing():
    """Missing features must be distinguishable from zero-valued ones."""
    assert hrv_features([0, 1, 2], [880.0, 885.0, 890.0]) == {}
    assert hrv_features([0, 1, 2], [50.0, 60.0, 55.0]) == {}
