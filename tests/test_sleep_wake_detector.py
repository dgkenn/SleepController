"""A dedicated SLEEP/WAKE detector, because two-stage and four-stage are different problems.

Altini & Kinnunen (Sensors 2021;21:4302) report 94% for sleep/wake from an accelerometer alone
and 96% with autonomic features, against 57% and 79% at four stages. Our four-class stager sits
at kappa 0.395-0.419 with wake_f1 0.450-0.493 -- and wake is what this system acts on.

The autonomic channel exists because `hrv_features`, the inter-beat-interval extractor in the
Topalidis lineage, was written and wired to NOTHING: the Verity's richest signal reached no model
at all. It ships DISABLED, because its weights are hand-set from literature directions with no
labelled IBI data to fit them against.
"""
import random

import pytest

from sleepctl.ml.sleep_wake import (AUTONOMIC_FEATURE_NAMES, SleepWakeDetector,
                                    windows_from_ibis)


def _night(sleep_frac=0.79, n_beats=2400, seed=1):
    """IBIs for a night that is mostly quiet sleep, ending in a block of wake."""
    random.seed(seed)
    rr, t = [], 0.0
    cut = int(n_beats * sleep_frac)
    for i in range(n_beats):
        v = 1100 + random.gauss(0, 40) if i < cut else 750 + random.gauss(0, 90)
        t += v / 1000.0
        rr.append((t, v))
    feats, _ = windows_from_ibis(rr)
    n = len(feats)
    k = int(n * sleep_frac)
    counts = [2.0] * k + [400.0] * (n - k)
    hr = [55.0] * k + [78.0] * (n - k)
    truth = [i >= k for i in range(n)]
    return feats, counts, hr, truth


def _accuracy(res, truth):
    return sum(1 for r, t in zip(res, truth) if r.awake == t) / max(1, len(truth))


def test_the_validated_channels_detect_the_wake_block():
    feats, counts, hr, truth = _night()
    res = SleepWakeDetector().score_night(feats, counts=counts, hr=hr)
    assert _accuracy(res, truth) >= 0.9


def test_no_false_wake_during_quiet_sleep():
    """False awakenings are the expensive error: they drive pre-emption and pollute the ledger."""
    feats, counts, hr, truth = _night()
    res = SleepWakeDetector().score_night(feats, counts=counts, hr=hr)
    assert sum(1 for r, t in zip(res, truth) if r.awake and not t) == 0


def test_the_autonomic_channel_is_off_by_default():
    """Hand-set weights with no labelled IBI data must not drive a control decision."""
    assert SleepWakeDetector.AUTONOMIC_DEFAULT is False
    feats, counts, hr, _ = _night()
    res = SleepWakeDetector().score_night(feats, counts=counts, hr=hr)
    assert all(r.channels.get("autonomic") is None for r in res)


def test_the_autonomic_channel_can_be_enabled_explicitly():
    feats, counts, hr, _ = _night()
    res = SleepWakeDetector(use_autonomic=True).score_night(feats, counts=counts, hr=hr)
    assert any(r.channels.get("autonomic") is not None for r in res)


def test_a_missing_channel_is_dropped_not_scored_as_sleep():
    """Treating "no accelerometer tonight" as evidence of sleep is how a sensor outage becomes a
    report of perfect sleep -- a failure this project has already lived through."""
    feats, _counts, hr, _ = _night()
    res = SleepWakeDetector().score_night(feats, counts=None, hr=hr)
    assert all(r.channels.get("motion") is None for r in res)
    assert all(r.n_channels >= 1 for r in res)


def test_with_no_channels_at_all_it_reports_that_rather_than_guessing():
    res = SleepWakeDetector().score_night([{}, {}, {}], counts=None, hr=None)
    assert all(r.n_channels == 0 and not r.awake for r in res)
    assert all("no channels" in r.reasons for r in res)


def test_a_feature_name_mismatch_raises_instead_of_silently_scoring_zero():
    """The first run of this module had the `ibi_` prefix missing from every feature name, so the
    whole autonomic channel returned 0.0 for an entire night with no error anywhere."""
    windows = [{"wrong_name": 1.0 * i} for i in range(20)]
    with pytest.raises(ValueError, match="no HRV feature names matched"):
        SleepWakeDetector(use_autonomic=True)._autonomic_scores(windows)


def test_too_few_windows_yields_no_autonomic_opinion():
    windows = [{"ibi_rmssd": 40.0}] * 3
    assert SleepWakeDetector(use_autonomic=True)._autonomic_scores(windows) == [None] * 3


def test_every_exported_feature_is_one_the_detector_actually_reads():
    """The export exists to make this channel calibratable; exporting the wrong columns would
    quietly make that impossible."""
    feats, _, _, _ = _night()
    produced = {k for f in feats if f for k in f}
    for name in AUTONOMIC_FEATURE_NAMES:
        assert name in produced, f"{name} is exported but never produced by the extractor"


def test_windows_are_built_from_past_intervals_only():
    """Mirrors how it runs live: an epoch may only see data that already happened."""
    rr = [(float(i), 1000.0) for i in range(600)]
    feats, starts = windows_from_ibis(rr, epoch_s=60.0, window_s=300.0)
    assert starts[0] == rr[0][0]
    assert not feats[0], "the first epoch has no preceding window to characterise"
