"""Prevention-timing audit: can pre-emptive cooling physically arrive before the awakening?

The distinction under test is the whole point of the module -- a failure where the bed had not yet
moved needs a LONGER LEAD, and a failure where it had arrived needs a different DOSE. Conflating
them makes the settle learner tune magnitude against a timing problem forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from sleepctl.learning.prevention_timing import (
    ARRIVAL_DELTA_F,
    MIN_FAILURES_FOR_VERDICT,
    PreventionEvent,
    analyze,
    first_wake_min,
    from_repo,
    measure_arrival_min,
)

T0 = datetime(2026, 7, 28, 2, 0, 0)


def _trace(start, minutes, temp_fn, wake_at=None):
    """One sample per minute from ``start - 5`` so there is a pre-cool reference reading."""
    out = []
    for i in range(-5, minutes):
        t = start + timedelta(minutes=i)
        out.append({"ts": t, "bed_temp_f": temp_fn(i),
                    "wake_event": 1 if (wake_at is not None and i == wake_at) else 0})
    return out


# ------------------------------------------------------------------ arrival measurement
def test_arrival_is_measured_from_the_temperature_at_the_start():
    # Flat 72 until t=8, then a steady drop. 0.5 F below 72 is first crossed at t=10.
    def temp(i):
        return 72.0 if i <= 8 else 72.0 - 0.25 * (i - 8)

    assert measure_arrival_min(_trace(T0, 40, temp), T0) == 10.0


def test_arrival_ignores_a_drop_that_precedes_the_precool():
    """A bed already falling before the command must not be credited as this maneuver arriving."""
    def temp(i):
        return 74.0 - 0.5 * i if i < 0 else 72.0  # dropped BEFORE t=0, flat after

    assert measure_arrival_min(_trace(T0, 40, temp), T0) is None


def test_arrival_is_none_when_the_bed_never_moves():
    assert measure_arrival_min(_trace(T0, 40, lambda i: 72.0), T0) is None


def test_arrival_respects_the_search_horizon():
    def temp(i):
        return 72.0 if i < 30 else 60.0

    assert measure_arrival_min(_trace(T0, 60, temp), T0, search_min=20.0) is None
    assert measure_arrival_min(_trace(T0, 60, temp), T0, search_min=45.0) == 30.0


def test_arrival_handles_a_missing_reference_reading():
    """Trace starts exactly at the pre-cool: use the first in-window reading as the reference."""
    samples = [{"ts": T0 + timedelta(minutes=i), "bed_temp_f": 72.0 - 0.2 * i, "wake_event": 0}
               for i in range(20)]
    assert measure_arrival_min(samples, T0) == 3.0  # 0.2*3 = 0.6 >= 0.5


def test_arrival_survives_junk_rows():
    samples = [{"ts": None, "bed_temp_f": 70.0, "wake_event": 0},
               {"ts": T0 - timedelta(minutes=1), "bed_temp_f": None, "wake_event": 0},
               {"ts": T0, "bed_temp_f": "warm", "wake_event": 0},
               {"ts": T0 + timedelta(minutes=1), "bed_temp_f": 72.0, "wake_event": 0},
               {"ts": T0 + timedelta(minutes=2), "bed_temp_f": 71.0, "wake_event": 0}]
    assert measure_arrival_min(samples, T0) == 2.0
    assert measure_arrival_min([], T0) is None


def test_first_wake_min_picks_the_earliest_in_window():
    samples = _trace(T0, 40, lambda i: 72.0)
    samples[5 + 12]["wake_event"] = 1   # i = 12
    samples[5 + 20]["wake_event"] = 1   # i = 20
    assert first_wake_min(samples, T0) == 12.0
    assert first_wake_min(samples, T0, search_min=5.0) is None


# ------------------------------------------------------------------ the timing/dose split
def test_wake_before_arrival_is_a_timing_failure():
    e = PreventionEvent(ts=T0, window_type="circadian", lead_used_min=12.0,
                        prevented=False, arrival_min=14.0, wake_min=6.0)
    assert e.cause == "timing"


def test_wake_after_arrival_is_a_dose_failure():
    e = PreventionEvent(ts=T0, window_type="circadian", lead_used_min=12.0,
                        prevented=False, arrival_min=5.0, wake_min=18.0)
    assert e.cause == "dose"


def test_no_measurable_arrival_counts_as_timing_not_unknown():
    """Commanded cooling that never showed up is the most extreme timing failure, not missing data."""
    e = PreventionEvent(ts=T0, window_type="recurring", lead_used_min=12.0,
                        prevented=False, arrival_min=None, wake_min=9.0)
    assert e.cause == "timing"


def test_prevented_events_have_no_failure_cause():
    e = PreventionEvent(ts=T0, window_type="recurring", lead_used_min=12.0,
                        prevented=True, arrival_min=5.0, wake_min=None)
    assert e.cause is None


# ------------------------------------------------------------------ verdicts
def _fail(arrival, wake, window="circadian", lead=12.0):
    return PreventionEvent(ts=T0, window_type=window, lead_used_min=lead,
                           prevented=False, arrival_min=arrival, wake_min=wake)


def test_timing_limited_verdict_recommends_a_longer_lead():
    rep = analyze([_fail(14.0, 5.0) for _ in range(5)])
    assert rep.verdict == "timing_limited"
    assert rep.n_timing == 5 and rep.n_dose == 0
    # must cover the measured arrival with margin, and never fall below the lead already in use
    assert rep.recommended_lead_min >= 14.0
    assert rep.recommended_lead_min >= 12.0
    assert "lead" in rep.remedy


def test_dose_limited_verdict_does_not_touch_the_lead():
    rep = analyze([_fail(4.0, 20.0) for _ in range(5)])
    assert rep.verdict == "dose_limited"
    assert rep.n_dose == 5 and rep.n_timing == 0
    assert "not the constraint" in rep.remedy


def test_mixed_verdict_when_neither_dominates():
    rep = analyze([_fail(14.0, 5.0), _fail(14.0, 5.0), _fail(4.0, 20.0), _fail(4.0, 20.0)])
    assert rep.verdict == "mixed"


def test_a_dead_water_loop_is_reported_as_the_actuator_not_the_lead():
    """Cooling commanded repeatedly, bed never moved -- do not send the user to tune lead time."""
    rep = analyze([_fail(None, 6.0) for _ in range(5)])
    assert rep.verdict == "no_thermal_response"
    assert "water loop" in rep.remedy
    assert rep.recommended_lead_min is None, "must not recommend a lead when nothing actuates"


def test_insufficient_data_below_the_failure_floor():
    rep = analyze([_fail(14.0, 5.0) for _ in range(MIN_FAILURES_FOR_VERDICT - 1)])
    assert rep.verdict == "insufficient_data"


def test_successes_alone_do_not_trigger_a_failure_verdict():
    ok = [PreventionEvent(ts=T0, window_type="recurring", lead_used_min=12.0,
                          prevented=True, arrival_min=6.0) for _ in range(10)]
    rep = analyze(ok)
    assert rep.verdict == "insufficient_data"
    assert rep.n_failures == 0
    assert rep.median_arrival_min == 6.0


def test_empty_report_is_safe():
    rep = analyze([])
    assert rep.verdict == "insufficient_data" and rep.to_dict()["n_resolved"] == 0


def test_per_window_breakdown_separates_window_types():
    rep = analyze([_fail(14.0, 5.0, window="circadian"),
                   _fail(14.0, 5.0, window="circadian"),
                   _fail(4.0, 20.0, window="cycle_boundary"),
                   _fail(4.0, 20.0, window="cycle_boundary")])
    assert rep.by_window["circadian"]["timing"] == 2
    assert rep.by_window["cycle_boundary"]["dose"] == 2


# ------------------------------------------------------------------ live ledger
def test_from_repo_reads_the_real_tables(tmp_path):
    from sleepctl.storage.repository import Repository

    repo = Repository(str(tmp_path / "t.db"))
    now = datetime.now()
    # Three pre-cools that failed: the bed only starts moving at +9 min, the wake lands at +4.
    for k in range(5):
        start = now - timedelta(hours=6 - k)
        repo.log_precool_event(start.date().isoformat(), start, "circadian", 12.0, 20.0)
        for i in range(-5, 30):
            t = start + timedelta(minutes=i)
            temp = 72.0 if i < 9 else 72.0 - 0.3 * (i - 9)
            repo.conn.execute(
                "INSERT INTO raw_samples (ts, night_date, bed_temp_f, wake_event) VALUES (?,?,?,?)",
                (t.isoformat(), start.date().isoformat(), temp, 1 if i == 4 else 0))
    repo.conn.commit()
    repo.resolve_precool_events()

    rep = from_repo(repo)
    assert rep.n_resolved == 5
    assert rep.n_failures == 5, "a wake inside the window must resolve as a failure"
    assert rep.verdict == "timing_limited"
    assert rep.median_arrival_min == 11.0   # 0.5 F below 72 first crossed at i=11
    assert rep.median_wake_min == 4.0
    repo.close()


def test_from_repo_never_raises_on_a_broken_repo():
    class Boom:
        class conn:
            @staticmethod
            def execute(*a, **k):
                raise RuntimeError("db gone")

    rep = from_repo(Boom())
    assert rep.verdict == "insufficient_data" and rep.n_resolved == 0
