"""The go-to-sleep phase was not warming at all on half of all nights.

`decide_warm_pulse` A/Bs the warm opener by alternating on night index, and judges the arms on
`onset_latency_min`. That value was corrupted for as long as a daemon restart could re-anchor bed
entry: every onset on 2026-08-27 reported ~1.0 min latency (one exactly 0.0) against a
rollup-computed 36.3 min. An A/B fed a broken outcome cannot converge -- it just alternates
forever, silently removing the warm opener on every odd night for a user who had explicitly
opted into it and then reported the phase was not warm enough.
"""
from sleepctl.config import AppConfig
from sleepctl.learning.onset_tuning import (_MIN_CREDIBLE_ONSET_LATENCY_MIN, decide_warm_pulse)


def _rec(latency, pulse_on):
    return {"onset_latency_min": latency, "warm_pulse_on": pulse_on}


def test_with_no_credible_history_the_pulse_runs_rather_than_alternating():
    """The user opted in. Withholding it on a coin flip nothing can learn from is worse."""
    for night_index in (237, 238, 239, 240):
        run, why = decide_warm_pulse([], night_index)
        assert run is True, f"night {night_index} withheld the opener"
        assert "credible" in why


def test_corrupted_latencies_cannot_decide_the_arm():
    """~1 min onsets are artifacts of the bed-entry bug, not fast sleep."""
    records = [_rec(1.0, True) for _ in range(10)] + [_rec(0.0, False) for _ in range(10)]
    run, why = decide_warm_pulse(records, night_index=239)
    assert run is True and "credible" in why


def test_credible_history_restores_normal_exploration():
    # A genuine WASH (well inside the 1.5 min decision margin), so it must fall through to the
    # alternating explore rather than picking an arm.
    records = [_rec(20.0, True) for _ in range(5)] + [_rec(20.4, False) for _ in range(5)]
    assert decide_warm_pulse(records, night_index=238)[0] is True
    assert decide_warm_pulse(records, night_index=239)[0] is False


def test_a_clear_win_for_the_pulse_is_still_learned():
    records = [_rec(12.0, True) for _ in range(5)] + [_rec(30.0, False) for _ in range(5)]
    run, why = decide_warm_pulse(records, night_index=239)
    assert run is True and "faster" in why


def test_a_clear_win_for_skipping_it_is_also_still_learned():
    """The gate must not become a one-way ratchet that can only ever say 'warm'."""
    records = [_rec(30.0, True) for _ in range(5)] + [_rec(12.0, False) for _ in range(5)]
    run, why = decide_warm_pulse(records, night_index=238)
    assert run is False and "skipping" in why


def test_the_credibility_floor_is_above_the_observed_artifacts():
    assert _MIN_CREDIBLE_ONSET_LATENCY_MIN > 1.0


def test_the_warm_opener_is_substantially_warmer_than_before():
    """Direct user report: 'we want it to get much warmer'."""
    t = AppConfig.default().tunables
    assert t.onset_warm_nudge_f >= 4.0
    assert t.onset_warm_comfort_cap_f >= t.onset_warm_nudge_f


def test_the_warm_peak_is_reachable_and_within_the_device_range():
    from sleepctl.controller.calibration import fahrenheit_to_level, level_to_fahrenheit
    t = AppConfig.default().tunables
    peak = 69.0 + min(t.onset_warm_nudge_f, t.onset_warm_comfort_cap_f)
    assert 55.0 <= peak <= 110.0
    assert abs(level_to_fahrenheit(fahrenheit_to_level(peak)) - peak) <= 1.0
