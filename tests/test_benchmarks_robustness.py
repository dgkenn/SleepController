"""Audit hardening for the perfect-sleep index: edge cases, bad input, and mode×debt conflicts.

This index defines what the whole controller optimizes toward, so it must never crash, never
NaN, always return 0..100, and resolve the debt-vs-mode conflicts sensibly."""

import math

import pytest

from sleepctl.benchmarks import (NightMode, _debt_adjust_targets, _norm_efficiency, _safe_float,
                                 perfect_sleep_index, targets_for)
from sleepctl.models import NightSummary


def _n(**kw):
    base = dict(date="2026-06-27", total_sleep_min=470, deep_min=90, rem_min=95,
               sleep_efficiency=0.92, sleep_onset_latency_min=12, wake_events=2, waso_min=14)
    base.update(kw)
    return NightSummary(**base)


# ---------------------------------------------------------------- input robustness
def test_efficiency_percent_and_fraction_score_identically():
    assert _norm_efficiency(92) == pytest.approx(0.92)
    assert _norm_efficiency(0.92) == pytest.approx(0.92)
    a = perfect_sleep_index(_n(sleep_efficiency=92.0))
    b = perfect_sleep_index(_n(sleep_efficiency=0.92))
    assert a["score"] == b["score"]      # unit-agnostic, no silent saturation


def test_safe_float_handles_nan_inf_none_garbage():
    for bad in (None, float("nan"), float("inf"), "x", [], {}):
        assert _safe_float(bad) == 0.0


def test_garbage_inputs_never_crash_or_nan():
    for bad in (None, float("nan"), -5, 99999):
        r = perfect_sleep_index(_n(total_sleep_min=bad, deep_min=bad, rem_min=bad,
                                   sleep_efficiency=bad, wake_events=bad,
                                   sleep_onset_latency_min=bad))
        assert 0.0 <= r["score"] <= 100.0 and not math.isnan(r["score"])


def test_stage_exceeding_total_is_capped():
    # bad data: 200 min "deep" on 100 min total -> deep% clamped to 1.0, not 2.0
    r = perfect_sleep_index(_n(total_sleep_min=100, deep_min=200, rem_min=200))
    assert r["components"]["deep"] <= 1.0 and r["components"]["rem"] <= 1.0


def test_insufficient_data_is_flagged_not_scored_as_terrible():
    assert perfect_sleep_index(_n(total_sleep_min=None))["insufficient_data"] is True
    assert perfect_sleep_index(_n(total_sleep_min=30))["insufficient_data"] is True
    full = perfect_sleep_index(_n())
    assert full["insufficient_data"] is False and full["missing"] == []


def test_score_always_bounded_and_monotone_in_deep():
    scores = [perfect_sleep_index(_n(deep_min=d))["score"] for d in (0, 40, 80, 120)]
    assert all(0 <= s <= 100 for s in scores)
    assert scores == sorted(scores)     # more deep (up to ideal) never lowers the score


def test_more_awakenings_never_raise_the_score():
    scores = [perfect_sleep_index(_n(wake_events=w, waso_min=w * 7))["score"] for w in (0, 2, 5, 8)]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------- mode × debt conflicts
def test_constrained_night_does_not_get_total_upweighted_by_debt():
    base = targets_for(NightMode.CONSTRAINED)
    adj = _debt_adjust_targets(base, 1.0, NightMode.CONSTRAINED)
    # you can't repay debt on a forced-short night -> total weight unchanged (no unfair penalty)
    assert adj.weights["total"] == base.weights["total"]
    # but deep IS still rewarded, and onset still tightens (efficiency intentionally not)
    assert adj.weights["deep"] > base.weights["deep"]
    assert adj.sol_max_min < base.sol_max_min
    assert adj.efficiency_min == base.efficiency_min


def test_recovery_night_rem_weight_not_downweighted_by_debt():
    base = targets_for(NightMode.RECOVERY)
    adj = _debt_adjust_targets(base, 1.0, NightMode.RECOVERY)
    # RECOVERY rewards the REM rebound; debt must not fight it
    assert adj.weights["rem"] == base.weights["rem"]


def test_continuity_weights_preserved_under_debt():
    for mode in NightMode:
        base = targets_for(mode)
        adj = _debt_adjust_targets(base, 1.0, mode)
        for k in ("waso", "awakenings"):
            assert adj.weights.get(k, 0) == base.weights.get(k, 0)  # maintenance never buried


def test_onset_tightening_is_floored():
    base = targets_for(NightMode.CONSTRAINED)  # already tight sol
    adj = _debt_adjust_targets(base, 1.0, NightMode.CONSTRAINED)
    assert adj.sol_max_min >= 4.0


def test_no_debt_is_byte_identical():
    n = _n()
    assert perfect_sleep_index(n, debt_min=0.0) == perfect_sleep_index(n)
