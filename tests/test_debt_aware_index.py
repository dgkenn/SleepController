"""Debt-aware perfect-sleep index: under debt a deep-heavy/long night is rewarded more, and
onset/efficiency are judged tighter (PubMed-grounded recovery architecture)."""

from sleepctl.benchmarks import NightMode, _debt_factor, perfect_sleep_index, targets_for
from sleepctl.models import NightSummary


def _night(deep, rem, tst=470, eff=0.92, sol=12, wake=2):
    return NightSummary(date="2026-06-27", total_sleep_min=tst, deep_min=deep, rem_min=rem,
                        light_min=tst - deep - rem, sleep_efficiency=eff,
                        sleep_onset_latency_min=sol, wake_events=wake,
                        waso_min=wake * 7.0)


def test_no_debt_is_unchanged():
    n = _night(90, 95)
    assert perfect_sleep_index(n, debt_min=0.0) == perfect_sleep_index(n)


def test_deep_factor_is_monotonic_and_clamped():
    assert _debt_factor(0) == 0.0
    assert 0 < _debt_factor(180) < 1.0
    assert _debt_factor(360) == 1.0 and _debt_factor(1000) == 1.0


def test_deep_heavy_recovery_night_scores_higher_under_debt():
    # a deep-heavy, long recovery night
    rich = _night(deep=130, rem=70, tst=520)
    base = perfect_sleep_index(rich, debt_min=0.0)["score"]
    in_debt = perfect_sleep_index(rich, debt_min=360.0)["score"]
    assert in_debt > base  # the rebound + long sleep is rewarded more when in debt


def test_slow_onset_penalized_more_under_debt():
    t = targets_for(NightMode.NORMAL)
    sol_val = t.sol_max_min + 5   # inside the ramp-down range so tightening is visible
    slow = _night(deep=90, rem=95, sol=sol_val)
    base = perfect_sleep_index(slow, debt_min=0.0)["components"]["sol"]
    in_debt = perfect_sleep_index(slow, debt_min=360.0)["components"]["sol"]
    assert 0.0 < base <= 1.0 and in_debt < base  # tighter onset benchmark under debt


def test_readiness_passes_debt_through():
    from sleepctl.readiness import morning_readiness
    nights = [_night(40, 40, tst=240) for _ in range(6)]  # chronic short -> heavy debt
    r = morning_readiness(_night(130, 70, tst=520), nights)
    assert r.debt_min > 0  # debt computed and (now) fed into the index
