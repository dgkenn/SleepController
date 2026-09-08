"""Epoch-level accelerometer use: movement clusters and the restlessness ramp."""
from sleepctl.controller.actigraphy_epochs import (CLUSTER_AROUSAL, cluster_score, epoch_counts,
                                                   restlessness)

NOW = 100_000.0


def _hist(bursts_at, base=0.5, span_s=3000.0, step=2.0):
    """Dense counts every 2 s: a quiet baseline plus bursts (count 6) at the given offsets."""
    out = []
    t = NOW - span_s
    while t <= NOW:
        v = base
        for b in bursts_at:
            if abs((NOW - t) - b) < 1.0:
                v = 6.0
        out.append((t, v))
        t += step
    return out


def test_epoch_counts_are_newest_first_and_missing_epochs_read_zero():
    ep = epoch_counts([(NOW - 10, 3.0), (NOW - 40, 7.0)], NOW, epochs=4)
    assert ep == [3.0, 7.0, 0.0, 0.0]


def test_one_isolated_burst_is_not_a_cluster_but_a_run_of_movement_is():
    one = cluster_score(_hist([5]), NOW, burst_thresh=5.0)
    assert one < CLUSTER_AROUSAL * 1.5
    run = cluster_score(_hist([5, 40, 70, 100]), NOW, burst_thresh=5.0)
    assert run >= CLUSTER_AROUSAL and run > one


def test_a_ramp_reads_as_high_density_against_a_quiet_baseline():
    quiet = restlessness(_hist([]), NOW, burst_thresh=5.0)
    assert quiet["density"] == 0.0
    ramp = restlessness(_hist([10, 60, 120, 200, 270]), NOW, burst_thresh=5.0)
    assert ramp["density"] >= 4 and ramp["ratio"] >= 2.0


def test_a_restless_night_does_not_make_ordinary_turning_a_ramp():
    # bursts every 3 minutes all night: the baseline absorbs them, the ratio stays near 1
    bursts = list(range(10, 3000, 180))
    r = restlessness(_hist(bursts), NOW, burst_thresh=5.0)
    assert r["ratio"] < 2.0
