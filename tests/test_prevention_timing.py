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
    has_readings,
    measure_arrival_min,
    measure_level_arrival_min,
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


# ------------------------------------------------------------------ the device-level trace
def _levels(start, minutes, level_fn):
    return [{"ts": start + timedelta(minutes=i), "device_level": level_fn(i)}
            for i in range(-5, minutes)]


def test_level_arrival_is_measured_the_same_way_as_temperature():
    # Flat -10 until t=6, then cooling at 1 level/min. 2 levels below -10 is crossed at t=8.
    def lvl(i):
        return -10 if i <= 6 else -10 - (i - 6)

    assert measure_level_arrival_min(_levels(T0, 40, lvl), T0) == 8.0


def test_a_flat_level_trace_reports_no_arrival():
    assert measure_level_arrival_min(_levels(T0, 40, lambda i: -10), T0) is None


def test_level_arrival_ignores_warming():
    """Only a fall counts -- a bed drifting warm is not a cooling maneuver arriving."""
    assert measure_level_arrival_min(_levels(T0, 40, lambda i: -10 + max(0, i)), T0) is None


def test_has_readings_separates_present_from_null():
    assert has_readings([{"ts": T0, "bed_temp_f": None}], "bed_temp_f") is False
    assert has_readings([{"ts": T0, "bed_temp_f": None},
                         {"ts": T0, "bed_temp_f": 72.0}], "bed_temp_f") is True
    assert has_readings([], "bed_temp_f") is False
    assert has_readings([{"ts": T0}], "bed_temp_f") is False, "absent key is not a reading"


# ------------------------------------------------------------------ the timing/dose split
def test_wake_before_arrival_is_a_timing_failure():
    e = PreventionEvent(ts=T0, window_type="circadian", lead_used_min=12.0,
                        prevented=False, arrival_min=14.0, wake_min=6.0,
                        arrival_source="bed_temp")
    assert e.cause == "timing"


def test_wake_after_arrival_is_a_dose_failure():
    e = PreventionEvent(ts=T0, window_type="circadian", lead_used_min=12.0,
                        prevented=False, arrival_min=5.0, wake_min=18.0,
                        arrival_source="bed_temp")
    assert e.cause == "dose"


def test_a_watched_bed_that_never_moved_is_the_extreme_timing_failure():
    """We could SEE the bed and it never moved: not missing data, the worst possible lead."""
    e = PreventionEvent(ts=T0, window_type="recurring", lead_used_min=12.0,
                        prevented=False, arrival_min=None, wake_min=9.0,
                        arrival_source="device_level")
    assert e.measurable is True
    assert e.cause == "timing"


def test_an_unwatched_bed_has_no_cause_at_all():
    """THE regression. With no trace, ``arrival_min`` is None because we were blind -- identical in
    shape to "never moved" but opposite in meaning. Scoring it as a timing failure recommends an
    ever-longer lead on the strength of a missing Autopilot subscription."""
    e = PreventionEvent(ts=T0, window_type="recurring", lead_used_min=12.0,
                        prevented=False, arrival_min=None, wake_min=9.0,
                        arrival_source=None)
    assert e.measurable is False
    assert e.cause is None


def test_prevented_events_have_no_failure_cause():
    e = PreventionEvent(ts=T0, window_type="recurring", lead_used_min=12.0,
                        prevented=True, arrival_min=5.0, wake_min=None,
                        arrival_source="bed_temp")
    assert e.cause is None


# ------------------------------------------------------------------ verdicts
def _fail(arrival, wake, window="circadian", lead=12.0, source="bed_temp"):
    return PreventionEvent(ts=T0, window_type=window, lead_used_min=lead,
                           prevented=False, arrival_min=arrival, wake_min=wake,
                           arrival_source=source)


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


def test_a_blind_box_is_not_reported_as_a_dead_water_loop():
    """THE regression, at the verdict level. On a Pod with no Autopilot membership every
    ``bed_temp_f`` is NULL, so the old code saw "no arrivals" and told the user to go check the
    priming and reservoir -- a confident accusation against hardware it could not see."""
    rep = analyze([_fail(None, 6.0, source=None) for _ in range(5)])
    assert rep.verdict == "no_thermal_data"
    assert "cannot tell" in rep.detail
    assert "membership" in rep.remedy
    # It may POINT AT the loop's real check; it must not accuse the loop of being at fault.
    assert "check priming" not in (rep.remedy or "")
    assert "not delivering" not in (rep.remedy or "")
    assert rep.recommended_lead_min is None


def test_one_measurable_event_is_enough_to_stop_the_blind_verdict():
    """The blind verdict claims NOTHING was observable; a single real trace refutes that."""
    rep = analyze([_fail(None, 6.0, source=None) for _ in range(5)] + [_fail(4.0, 20.0)])
    assert rep.verdict != "no_thermal_data"
    assert rep.n_measurable == 1


def test_blind_failures_are_excluded_from_the_split_not_counted_as_timing():
    rep = analyze([_fail(4.0, 20.0) for _ in range(4)]
                  + [_fail(None, 6.0, source=None) for _ in range(6)])
    assert rep.verdict == "dose_limited", "blind events must not outvote the measured ones"
    assert rep.n_timing == 0
    assert rep.n_measurable == 4


def test_the_blind_count_is_surfaced_rather_than_hidden():
    """Below the floor, the user should learn WHY there is too little data."""
    rep = analyze([_fail(14.0, 5.0)] + [_fail(None, 6.0, source=None) for _ in range(4)])
    assert rep.verdict == "insufficient_data"
    assert "no thermal trace" in rep.detail


def test_insufficient_data_below_the_failure_floor():
    rep = analyze([_fail(14.0, 5.0) for _ in range(MIN_FAILURES_FOR_VERDICT - 1)])
    assert rep.verdict == "insufficient_data"


def test_successes_alone_do_not_trigger_a_failure_verdict():
    ok = [PreventionEvent(ts=T0, window_type="recurring", lead_used_min=12.0,
                          prevented=True, arrival_min=6.0, arrival_source="bed_temp")
          for _ in range(10)]
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
# thermal_samples lives in the DASHBOARD schema, not the engine's -- mirrored here (columns under
# test only) so these stay engine-suite tests rather than dragging in the dashboard app package.
_THERMAL_SAMPLES_DDL = """
CREATE TABLE IF NOT EXISTS thermal_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    device_level INTEGER,
    target_level INTEGER
);
"""
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
    assert {e.arrival_source for e in rep.events} == {"bed_temp"}
    repo.close()


def _seed_precools(repo, now, n=5, wake_at=4, temp_fn=None):
    """n pre-cools with raw_samples written; ``temp_fn`` None means NULL bed_temp_f throughout,
    which is what a Pod with no Autopilot membership actually records."""
    starts = []
    for k in range(n):
        start = now - timedelta(hours=6 - k)
        starts.append(start)
        repo.log_precool_event(start.date().isoformat(), start, "circadian", 12.0, 20.0)
        for i in range(-5, 30):
            t = start + timedelta(minutes=i)
            repo.conn.execute(
                "INSERT INTO raw_samples (ts, night_date, bed_temp_f, wake_event) VALUES (?,?,?,?)",
                (t.isoformat(), start.date().isoformat(),
                 None if temp_fn is None else temp_fn(i), 1 if i == wake_at else 0))
    repo.conn.commit()
    return starts


def test_from_repo_falls_back_to_the_device_level_when_there_is_no_sensed_temperature(tmp_path):
    """The membership-free path. No bed_temp_f anywhere -- the water-side level trace must carry
    the measurement rather than the whole analysis going dark."""
    from sleepctl.storage.repository import Repository

    repo = Repository(str(tmp_path / "lvl.db"))
    repo.conn.executescript(_THERMAL_SAMPLES_DDL)
    now = datetime.now()
    starts = _seed_precools(repo, now)
    for start in starts:
        for i in range(-5, 30):
            t = start + timedelta(minutes=i)
            level = -10 if i < 9 else -10 - 1.5 * (i - 9)   # 2 levels below -10 crossed at i=11
            repo.conn.execute(
                "INSERT INTO thermal_samples (ts, device_level, target_level) VALUES (?,?,?)",
                (t.isoformat(), level, -60))
    repo.conn.commit()
    repo.resolve_precool_events()

    rep = from_repo(repo)
    assert {e.arrival_source for e in rep.events} == {"device_level"}
    assert rep.verdict == "timing_limited", "wake at +4 precedes arrival at +11"
    assert rep.median_arrival_min == 11.0
    repo.close()


def test_from_repo_reports_a_blind_box_as_blind_not_as_a_dead_loop(tmp_path):
    """No sensed temperature AND no level trace: the honest answer is "we cannot see", and it must
    not become an accusation against the water loop."""
    from sleepctl.storage.repository import Repository

    repo = Repository(str(tmp_path / "blind.db"))
    _seed_precools(repo, datetime.now())
    repo.resolve_precool_events()

    rep = from_repo(repo)
    assert rep.n_resolved == 5
    assert rep.verdict == "no_thermal_data"
    assert all(e.arrival_source is None for e in rep.events)
    assert "check priming" not in (rep.remedy or "")
    repo.close()


def test_from_repo_still_convicts_a_loop_it_can_actually_see(tmp_path):
    """The other side of the same coin: a level trace that is present and FLAT is real evidence,
    and must still produce the actuator verdict."""
    from sleepctl.storage.repository import Repository

    repo = Repository(str(tmp_path / "dead.db"))
    repo.conn.executescript(_THERMAL_SAMPLES_DDL)
    now = datetime.now()
    starts = _seed_precools(repo, now)
    for start in starts:
        for i in range(-5, 30):
            repo.conn.execute(
                "INSERT INTO thermal_samples (ts, device_level, target_level) VALUES (?,?,?)",
                ((start + timedelta(minutes=i)).isoformat(), -10, -60))  # commanded cold, never moves
    repo.conn.commit()
    repo.resolve_precool_events()

    rep = from_repo(repo)
    assert rep.verdict == "no_thermal_response"
    assert "water loop" in rep.remedy
    repo.close()


def test_from_repo_never_raises_on_a_broken_repo():
    class Boom:
        class conn:
            @staticmethod
            def execute(*a, **k):
                raise RuntimeError("db gone")

    rep = from_repo(Boom())
    assert rep.verdict == "insufficient_data" and rep.n_resolved == 0


# ------------------------------------------------------------------ timestamp hygiene
def test_aware_and_naive_timestamps_can_be_mixed():
    """The daemon writes naive-local; other paths write aware UTC. Subtracting one from the other
    raises TypeError, which would take out every duration this module computes."""
    from datetime import timezone

    start = datetime(2026, 7, 28, 2, 0, 0)
    aware = [{"ts": (start + timedelta(minutes=i)).replace(tzinfo=timezone.utc).astimezone(),
              "bed_temp_f": 72.0 if i < 6 else 70.0, "wake_event": 1 if i == 3 else 0}
             for i in range(-5, 20)]
    # Must not raise, and must find the same offsets as the naive equivalent.
    assert measure_arrival_min(aware, start) == 6.0
    assert first_wake_min(aware, start) == 3.0


def test_aware_timestamps_are_converted_not_merely_stripped():
    """Stripping tzinfo off a UTC value would shift every offset by the local UTC offset."""
    from datetime import timezone

    start = datetime(2026, 7, 28, 2, 0, 0)
    naive = _trace(start, 20, lambda i: 72.0 if i < 6 else 70.0)
    aware = [{"ts": s["ts"].replace(tzinfo=None).astimezone(), **{k: v for k, v in s.items()
                                                                 if k != "ts"}}
             for s in naive]
    assert measure_arrival_min(aware, start) == measure_arrival_min(naive, start)
