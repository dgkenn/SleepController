"""Validation backtest: the closed loop must beat no-control on the response-aware model, hold
the safety invariants, and be deterministic."""

from sleepctl.eval.backtest import backtest, run_night


def test_controller_beats_no_control_baseline():
    rep = backtest(nights=10, seed=7)
    c, b, d = rep["controller"], rep["baseline"], rep["delta"]
    assert d["wake_events"] < 0                  # fewer awakenings (the #1 problem)
    assert d["deep_min"] > 0                     # more deep sleep
    assert d["efficiency"] > 0                   # better efficiency
    assert d["outcome_score"] > 0                # better overall reward
    assert c["grogginess"] <= b["grogginess"]    # not groggier


def test_safety_invariants_hold_every_tick():
    rep = backtest(nights=10, seed=7)
    s = rep["safety"]
    assert s["max_step_f"] <= s["max_step_limit"] + 1e-9   # slew never exceeded
    assert s["out_of_bounds_ticks"] == 0                   # always within 55-110 F


def test_improvement_holds_on_fragmented_nights():
    rep = backtest(nights=8, scenario="clustered_awakenings", seed=11)
    assert rep["delta"]["wake_events"] < 0 and rep["delta"]["outcome_score"] > 0


def test_deterministic_given_seed():
    a = run_night("controller", seed=3)
    b = run_night("controller", seed=3)
    assert a == b
