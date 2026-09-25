"""Whole-night (offline) HMM smoothing of the stager's emissions (sleep_staging.offline)."""

from __future__ import annotations

import itertools
import math
import random

import pytest

from sleepctl.ml.sleep_staging import offline
from sleepctl.ml.sleep_staging.infer import SleepStager, forward_filter

TRANS = [[0.90, 0.08, 0.01, 0.01],
         [0.02, 0.94, 0.02, 0.02],
         [0.01, 0.04, 0.95, 0.00],
         [0.01, 0.04, 0.00, 0.95]]
START = [0.9, 0.05, 0.03, 0.02]
UNIFORM = [0.25] * 4


def _random_emissions(n, seed=0):
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        e = [rng.random() + 0.05 for _ in range(4)]
        s = sum(e)
        out.append([x / s for x in e])
    return out


def _brute(emissions, temper):
    """Every path's joint probability, for tiny problems."""
    lik = [[(max(e[k], 1e-9) / 0.25) ** temper for k in range(4)] if e is not None else [1.0] * 4
           for e in emissions]
    paths = {}
    for path in itertools.product(range(4), repeat=len(emissions)):
        p = START[path[0]] * lik[0][path[0]]
        for t in range(1, len(path)):
            p *= TRANS[path[t - 1]][path[t]] * lik[t][path[t]]
        paths[path] = p
    return paths


@pytest.mark.parametrize("temper", [1.0, 0.35])
def test_forward_backward_matches_brute_force_marginals(temper):
    em = _random_emissions(5, seed=1)
    em[2] = None                                     # a missing epoch carries no evidence
    paths = _brute(em, temper)
    z = sum(paths.values())
    post = offline.forward_backward(em, TRANS, START, UNIFORM, temper)
    for t in range(len(em)):
        for k in range(4):
            want = sum(p for path, p in paths.items() if path[t] == k) / z
            assert post[t][k] == pytest.approx(want, rel=1e-6, abs=1e-9)


@pytest.mark.parametrize("temper", [1.0, 0.35])
def test_viterbi_matches_brute_force_map_path(temper):
    em = _random_emissions(6, seed=2)
    paths = _brute(em, temper)
    best = max(paths, key=paths.get)
    assert tuple(offline.viterbi(em, TRANS, START, UNIFORM, temper)) == best


def test_last_epoch_equals_the_live_forward_filter():
    """With nothing after it, the final smoothed epoch is exactly what the live filter says."""
    em = _random_emissions(40, seed=3)
    post = offline.forward_backward(em, TRANS, START, UNIFORM, 0.35)
    live = forward_filter(em, TRANS, START, UNIFORM, 0.35)
    assert post[-1] == pytest.approx(live, rel=1e-9)


def test_later_evidence_reaches_back():
    """The whole point: an epoch is judged with the epochs AFTER it too. Weak deep evidence
    followed by strong deep evidence is deep offline, while the causal filter still says light."""
    light = [0.05, 0.80, 0.10, 0.05]
    weak_deep = [0.05, 0.45, 0.45, 0.05]
    deep = [0.02, 0.08, 0.88, 0.02]
    em = [light] * 30 + [weak_deep] * 4 + [deep] * 30
    post = offline.forward_backward(em, TRANS, START, UNIFORM, 0.35)
    for t in (33, 35):                   # the last weak epoch, and the causal filter's lag
        causal = forward_filter(em[:t + 1], TRANS, START, UNIFORM, 0.35)
        assert max(range(4), key=lambda k: causal[k]) == 1
        assert max(range(4), key=lambda k: post[t][k]) == 2


def test_posteriors_are_distributions_over_a_long_night():
    em = _random_emissions(1200, seed=4)
    for i in range(300, 500):
        em[i] = None                                 # a long dropout
    post = offline.forward_backward(em, TRANS, START, UNIFORM, 0.35)
    assert len(post) == 1200
    for p in post:
        assert sum(p) == pytest.approx(1.0) and all(0.0 <= x <= 1.0 for x in p)
        assert all(math.isfinite(x) for x in p)


def test_empty_inputs():
    assert offline.forward_backward([], TRANS, START, UNIFORM) == []
    assert offline.viterbi([], TRANS, START, UNIFORM) == []
    assert offline.smooth_night([], [], {"trans": TRANS, "prior": UNIFORM}) == []
    assert offline.smooth_night([0.0, 30.0], [None, None],
                                {"trans": TRANS, "prior": UNIFORM}) == [None, None]


def test_windowed_without_lookahead_is_the_live_filter():
    """Epoch by epoch, exactly what predict's forward filter says over its trailing window."""
    em = _random_emissions(60, seed=8)
    post = offline.windowed_posteriors(em, TRANS, START, UNIFORM, 0.35, lookback=20, lookahead=0)
    for t in range(60):
        live = forward_filter(em[max(0, t - 19):t + 1], TRANS, START, UNIFORM, 0.35)
        assert post[t] == pytest.approx(live, rel=1e-9)


def test_windowed_is_forward_backward_on_its_window():
    em = _random_emissions(60, seed=9)
    em[33] = None
    post = offline.windowed_posteriors(em, TRANS, START, UNIFORM, 0.35, lookback=20, lookahead=5)
    for t in (0, 3, 30, 33, 57, 59):
        lo, hi = max(0, t - 19), min(59, t + 5)
        fb = offline.forward_backward(em[lo:hi + 1], TRANS, START, UNIFORM, 0.35)
        assert post[t] == pytest.approx(fb[t - lo], rel=1e-9)
    whole = offline.windowed_posteriors(em, TRANS, START, UNIFORM, 0.35, lookback=100,
                                        lookahead=100)
    for a, b in zip(whole, offline.forward_backward(em, TRANS, START, UNIFORM, 0.35)):
        assert a == pytest.approx(b, rel=1e-9)


def test_the_lookahead_calls_a_stage_where_it_starts():
    light = [0.05, 0.80, 0.10, 0.05]
    deep = [0.02, 0.08, 0.88, 0.02]
    em = [light] * 30 + [deep] * 30
    live = offline.windowed_posteriors(em, TRANS, START, UNIFORM, 0.35, lookahead=0)
    ahead = offline.windowed_posteriors(em, TRANS, START, UNIFORM, 0.35, lookahead=5)
    first = lambda post: next(t for t, p in enumerate(post) if offline.label_of(p) == "deep")
    assert first(live) > 30 and first(ahead) <= 30


def test_label_of_applies_the_live_wake_rule():
    assert offline.label_of([0.5, 0.2, 0.2, 0.1]) == "wake"
    assert offline.label_of([0.45, 0.1, 0.44, 0.01]) == "wake"
    assert offline.label_of([0.1, 0.2, 0.6, 0.1]) == "deep"


def _hmm():
    return {"trans": TRANS, "start": START, "prior": [0.1, 0.5, 0.2, 0.2],
            "emission_prior": UNIFORM, "temper": 0.35, "smoothing_epochs": 4}


@pytest.mark.parametrize("whole_night", [True, False])
def test_smooth_night_bins_irregular_ticks_onto_the_epoch_grid(whole_night):
    """Ticks arrive in pairs and gaps; pairs in one epoch are averaged, gaps carry no evidence,
    and every input gets its own epoch's answer back, in input order."""
    em = _random_emissions(6, seed=5)
    times = [1000.0, 1010.0, 1060.0, 1300.0, 1330.0, 1335.0]
    out = offline.smooth_night(times, em, _hmm(), whole_night=whole_night)
    grid = [None] * 12
    grid[0] = [(a + b) / 2 for a, b in zip(em[0], em[1])]
    grid[2] = em[2]
    grid[10] = em[3]
    grid[11] = [(a + b) / 2 for a, b in zip(em[4], em[5])]
    want = (offline.forward_backward(grid, TRANS, START, UNIFORM, 0.35) if whole_night
            else offline.windowed_posteriors(grid, TRANS, START, UNIFORM, 0.35, lookback=4,
                                             lookahead=offline.LOOKAHEAD_EPOCHS))
    for i, g in enumerate([0, 0, 2, 10, 11, 11]):
        assert [out[i]["probs"][k] for k in ("wake", "light", "deep", "rem")] == \
            pytest.approx(want[g])
        assert out[i]["stage"] == offline.label_of(want[g])
    rev = offline.smooth_night(list(reversed(times)), list(reversed(em)), _hmm(),
                               whole_night=whole_night)
    assert [r["stage"] for r in reversed(rev)] == [o["stage"] for o in out]


def test_smooth_night_viterbi_labels_follow_the_path():
    em = _random_emissions(50, seed=6)
    times = [30.0 * i for i in range(50)]
    out = offline.smooth_night(times, em, _hmm(), method="viterbi")
    path = offline.viterbi(em, TRANS, START, UNIFORM, 0.35)
    assert [o["stage"] for o in out] == [("wake", "light", "deep", "rem")[k] for k in path]


def test_smooth_night_refuses_something_that_is_not_a_night():
    em = _random_emissions(2)
    with pytest.raises(ValueError):
        offline.smooth_night([0.0, 30.0 * (offline.MAX_EPOCHS + 5)], em, _hmm())


def test_smooth_night_runs_on_the_bundled_hmm():
    stager = SleepStager.load()
    if not stager.hmm:
        pytest.skip("no bundled HMM")
    em = _random_emissions(100, seed=7)
    for kw in ({}, {"whole_night": True}, {"method": "viterbi"}):
        out = offline.smooth_night([30.0 * i for i in range(100)], em, stager.hmm, **kw)
        assert all(o["stage"] in ("wake", "light", "deep", "rem") for o in out)
