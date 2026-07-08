"""Retention pruning for the previously-unbounded high-write tables (raw_samples, decisions,
interventions, thermal_samples) -- unlike events/state_history/sensor_samples, these were never
pruned before, growing forever for the life of the DB on a 24/7 box. Mirrors test_events.py's
coverage of ``prune_events``."""

from __future__ import annotations

from datetime import datetime, timedelta

from sleepctl.storage.repository import Repository


def _insert(repo, table: str, ts: str, extra_cols: str = "", extra_vals: tuple = ()) -> None:
    repo.conn.execute(
        f"INSERT INTO {table} (ts{extra_cols}) VALUES (?{',?' * len(extra_vals)})",
        (ts,) + extra_vals,
    )


def test_prune_raw_samples_keeps_recent_deletes_old():
    repo = Repository(":memory:")
    old_ts = (datetime.now() - timedelta(days=90)).isoformat()
    recent_ts = (datetime.now() - timedelta(days=1)).isoformat()
    for _ in range(3):
        _insert(repo, "raw_samples", old_ts)
    for _ in range(4):
        _insert(repo, "raw_samples", recent_ts)
    repo.conn.commit()

    deleted = repo.prune_raw_samples(keep_days=45)
    assert deleted == 3
    remaining = repo.conn.execute("SELECT COUNT(*) c FROM raw_samples").fetchone()["c"]
    assert remaining == 4


def test_prune_decisions_keeps_recent_deletes_old():
    repo = Repository(":memory:")
    old_ts = (datetime.now() - timedelta(days=60)).isoformat()
    recent_ts = (datetime.now() - timedelta(days=2)).isoformat()
    for _ in range(2):
        _insert(repo, "decisions", old_ts)
    for _ in range(5):
        _insert(repo, "decisions", recent_ts)
    repo.conn.commit()

    deleted = repo.prune_decisions(keep_days=45)
    assert deleted == 2
    remaining = repo.conn.execute("SELECT COUNT(*) c FROM decisions").fetchone()["c"]
    assert remaining == 5


def test_prune_interventions_keeps_recent_deletes_old():
    repo = Repository(":memory:")
    old_ts = (datetime.now() - timedelta(days=100)).isoformat()
    recent_ts = (datetime.now() - timedelta(hours=1)).isoformat()
    for _ in range(4):
        _insert(repo, "interventions", old_ts)
    for _ in range(1):
        _insert(repo, "interventions", recent_ts)
    repo.conn.commit()

    deleted = repo.prune_interventions(keep_days=45)
    assert deleted == 4
    remaining = repo.conn.execute("SELECT COUNT(*) c FROM interventions").fetchone()["c"]
    assert remaining == 1


def test_prune_thermal_samples_keeps_recent_deletes_old():
    repo = Repository(":memory:")
    old_ts = (datetime.now() - timedelta(days=200)).isoformat()
    recent_ts = datetime.now().isoformat()
    for _ in range(6):
        _insert(repo, "thermal_samples", old_ts)
    for _ in range(2):
        _insert(repo, "thermal_samples", recent_ts)
    repo.conn.commit()

    deleted = repo.prune_thermal_samples(keep_days=45)
    assert deleted == 6
    remaining = repo.conn.execute("SELECT COUNT(*) c FROM thermal_samples").fetchone()["c"]
    assert remaining == 2


def test_prune_never_raises_on_bad_connection():
    repo = Repository(":memory:")
    repo.conn.close()
    assert repo.prune_raw_samples() == 0
    assert repo.prune_decisions() == 0
    assert repo.prune_interventions() == 0
    assert repo.prune_thermal_samples() == 0
