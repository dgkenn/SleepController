"""Awakening forensics + n-of-1 experiment engine."""

import tempfile

import pytest

from sleepctl.experiments import (analyze_experiment, assign_arm, create_experiment,
                                  get_experiment, list_experiments, stop_experiment)
from sleepctl.forensics import awakening_forensics, forensics_summary
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


# ---- n-of-1 experiments ---------------------------------------------------
def test_create_and_balanced_assignment(repo):
    exp = create_experiment(repo, {
        "name": "cooler neutral", "metric": "wake_events", "min_nights_per_arm": 2,
        "arm_a": {"label": "70F", "params": {"neutral_f": 70}},
        "arm_b": {"label": "68F", "params": {"neutral_f": 68}},
    })
    arms = [assign_arm(repo, exp["id"], f"2026-06-{20+i:02d}") for i in range(4)]
    assert arms.count("a") == 2 and arms.count("b") == 2   # balanced
    # re-assigning the same date is idempotent
    assert assign_arm(repo, exp["id"], "2026-06-20") == arms[0]


def test_analyze_picks_lower_wake_arm(repo):
    exp = create_experiment(repo, {
        "metric": "wake_events", "min_nights_per_arm": 2,
        "arm_a": {"label": "control", "params": {}},
        "arm_b": {"label": "cooler", "params": {}},
    })
    dates = [f"2026-06-{20+i:02d}" for i in range(6)]
    for d in dates:
        assign_arm(repo, exp["id"], d)
    a = get_experiment(repo, exp["id"])["assignments"]
    # control arm = 3 wakes/night, cooler arm = 0 wakes/night
    for d, arm in a.items():
        _night_metric(repo, d, wake_events=3 if arm == "a" else 0)
    res = analyze_experiment(repo, exp["id"])
    assert res["enough_data"] is True
    assert res["winner"] == "cooler" and res["diff"] < 0

    finished = stop_experiment(repo, exp["id"])
    assert finished["status"] == "complete" and finished["result"]["winner"] == "cooler"
    assert list_experiments(repo, status="complete")


def test_unknown_metric_rejected(repo):
    with pytest.raises(ValueError):
        create_experiment(repo, {"metric": "not_a_metric"})
