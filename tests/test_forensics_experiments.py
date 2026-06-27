"""Awakening forensics + n-of-1 experiment engine."""

import tempfile

import pytest

from sleepctl.experiments import (analyze_experiment, assign_arm, create_experiment,
                                  get_experiment, list_experiments, stop_experiment)
from sleepctl.forensics import awakening_forensics, forensics_summary, suggest_experiment
from sleepctl.models import ContextRecord
from sleepctl.storage.repository import Repository


@pytest.fixture
def repo():
    r = Repository(tempfile.mktemp(suffix=".db"))
    yield r
    r.close()


def _wake(repo, ts, night, bed, room=70, hr=72):
    repo.conn.execute(
        "INSERT INTO raw_samples (ts, night_date, stage, bed_temp_f, room_temp_f, heart_rate, "
        "wake_event) VALUES (?,?,?,?,?,?,1)", (ts, night, "awake", bed, room, hr))
    repo.conn.commit()


def _night_metric(repo, date, wake_events=None, outcome=None):
    repo.conn.execute(
        "INSERT OR REPLACE INTO nightly_summaries (date, wake_events, outcome_score) VALUES (?,?,?)",
        (date, wake_events, outcome))
    repo.conn.commit()


# ---- forensics ------------------------------------------------------------
def test_forensics_attributes_warm_bed_and_alcohol(repo):
    repo.save_context(ContextRecord(date="2026-06-25", alcohol=True))
    # night's median ~ 66; the awakening sample is much warmer + at 3:30am
    for i in range(6):
        _wake(repo, f"2026-06-25T23:{10+i:02d}:00", "2026-06-25", bed=66)
    _wake(repo, "2026-06-26T03:30:00", "2026-06-25", bed=72, room=74, hr=78)
    events = awakening_forensics(repo, limit=10)
    assert events
    top = events[0]
    factors = {c["factor"] for c in top["likely_causes"]}
    assert "warm_bed" in factors and "alcohol" in factors
    assert top["top_cause"] in factors


def test_forensics_summary_aggregates(repo):
    repo.save_context(ContextRecord(date="2026-06-25", alcohol=True))
    _wake(repo, "2026-06-26T03:30:00", "2026-06-25", bed=74, room=75)
    summary = forensics_summary(awakening_forensics(repo))
    assert summary["n_awakenings"] >= 1 and summary["top_factors"]


def test_forensics_empty(repo):
    assert awakening_forensics(repo) == []


def test_hyperarousal_cause_without_thermal_trigger(repo):
    # HR surge, cool bed/room, no context -> physiological hyperarousal (behavioral target).
    for i in range(4):
        _wake(repo, f"2026-06-25T23:{10+i:02d}:00", "2026-06-25", bed=66, room=66, hr=58)
    _wake(repo, "2026-06-26T03:30:00", "2026-06-25", bed=66, room=66, hr=80)
    events = awakening_forensics(repo, limit=10)
    factors = {c["factor"] for c in events[0]["likely_causes"]}
    assert "hyperarousal" in factors and "warm_bed" not in factors


def test_suggest_experiment_from_warm_bed():
    summary = {"n_awakenings": 5, "top_factors": [{"factor": "warm_bed", "count": 4},
                                                  {"factor": "circadian", "count": 2}]}
    spec = suggest_experiment(summary)
    assert spec and spec["metric"] == "wake_events" and "neutral_f" in spec["arm_b"]["params"]
    assert spec["washout_nights"] == 1


def test_suggest_experiment_none_for_behavioral():
    summary = {"n_awakenings": 3, "top_factors": [{"factor": "alcohol", "count": 3}]}
    assert suggest_experiment(summary) is None


# ---- n-of-1 experiments ---------------------------------------------------
def test_schedule_has_washout_and_counterbalance(repo):
    exp = create_experiment(repo, {
        "name": "cooler neutral", "metric": "wake_events", "min_nights_per_arm": 1,
        "washout_nights": 1,
        "arm_a": {"label": "70F", "params": {"neutral_f": 70}},
        "arm_b": {"label": "68F", "params": {"neutral_f": 68}},
    })
    arms = [assign_arm(repo, exp["id"], f"2026-06-{20+i:02d}") for i in range(8)]
    # one cycle = A, washout, B, washout; next cycle counterbalances (B first)
    assert arms[:4] == ["a", "washout", "b", "washout"]
    assert arms[4:8] == ["b", "washout", "a", "washout"]
    assert assign_arm(repo, exp["id"], "2026-06-20") == "a"   # idempotent


def test_paired_analysis_picks_lower_wake_arm(repo):
    exp = create_experiment(repo, {
        "metric": "wake_events", "min_nights_per_arm": 1, "washout_nights": 1,
        "arm_a": {"label": "control", "params": {}},
        "arm_b": {"label": "cooler", "params": {}},
    })
    dates = [f"2026-06-{20+i:02d}" for i in range(8)]   # 2 cycles
    for d in dates:
        assign_arm(repo, exp["id"], d)
    a = get_experiment(repo, exp["id"])["assignments"]
    for d, slot in a.items():
        if slot in ("a", "b"):
            _night_metric(repo, d, wake_events=3 if slot == "a" else 0)
    res = analyze_experiment(repo, exp["id"])
    assert res["n_cycles"] == 2 and res["enough_data"] is True
    assert res["winner"] == "cooler" and res["diff"] < 0
    assert res["ci"] is not None and res["ci"][1] < 0   # interval excludes zero
    assert res["washout_nights"] == 1

    finished = stop_experiment(repo, exp["id"])
    assert finished["status"] == "complete" and finished["result"]["winner"] == "cooler"
    assert list_experiments(repo, status="complete")


def test_one_cycle_is_not_enough(repo):
    exp = create_experiment(repo, {"metric": "wake_events", "min_nights_per_arm": 1,
                                   "washout_nights": 1})
    for i in range(4):   # only one cycle
        d = f"2026-07-{1+i:02d}"
        slot = assign_arm(repo, exp["id"], d)
        if slot in ("a", "b"):
            _night_metric(repo, d, wake_events=2)
    assert analyze_experiment(repo, exp["id"])["enough_data"] is False


def test_unknown_metric_rejected(repo):
    with pytest.raises(ValueError):
        create_experiment(repo, {"metric": "not_a_metric"})
