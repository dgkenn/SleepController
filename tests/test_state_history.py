"""48h runtime-state history (``state_history`` table): record_state_snapshot / state_history
on Repository, and the on-write pruning of rows older than ~7 days."""

from __future__ import annotations

from datetime import datetime, timedelta

from sleepctl.storage.repository import Repository, _iso


def _snapshot(**overrides) -> dict:
    base = {
        "state": "maintenance", "mode": "auto", "target_temp_f": 68.0,
        "bed_temp_f": 69.2, "room_temp_f": 66.0, "stage": "deep", "confidence": 0.8,
        "target_level": -20, "daemon_alive": True, "extra": {"live": True},
    }
    base.update(overrides)
    return base


def test_record_state_snapshot_and_read_back():
    repo = Repository(":memory:")
    repo.record_state_snapshot(_snapshot())
    rows = repo.state_history(hours=48)
    assert len(rows) == 1
    r = rows[0]
    assert r["state"] == "maintenance"
    assert r["mode"] == "auto"
    assert r["target_temp_f"] == 68.0
    assert r["bed_temp_f"] == 69.2
    assert r["stage"] == "deep"
    assert r["daemon_alive"] == 1
    assert r["extra"] == {"live": True}
    assert r["ts"]  # timestamp populated (defaults to now when not supplied)


def test_state_history_newest_first_and_limit():
    repo = Repository(":memory:")
    for i in range(5):
        repo.record_state_snapshot(_snapshot(state=f"state{i}"))
    rows = repo.state_history(hours=48, limit=3)
    assert len(rows) == 3
    assert [r["state"] for r in rows] == ["state4", "state3", "state2"]


def test_state_history_windowed_by_hours():
    repo = Repository(":memory:")
    now = datetime.now()
    recent_ts = _iso(now - timedelta(hours=2))
    old_ts = _iso(now - timedelta(hours=72))
    repo.record_state_snapshot(_snapshot(state="recent", ts=recent_ts))
    repo.record_state_snapshot(_snapshot(state="old", ts=old_ts))

    rows = repo.state_history(hours=48)
    states = {r["state"] for r in rows}
    assert "recent" in states
    assert "old" not in states


def test_record_state_snapshot_prunes_rows_older_than_seven_days():
    repo = Repository(":memory:")
    now = datetime.now()
    # a row just inside the 7-day window survives a later write's prune...
    almost_week_ts = _iso(now - timedelta(days=6, hours=23))
    repo.record_state_snapshot(_snapshot(state="almost_week", ts=almost_week_ts))
    # ...but one older than ~7 days is pruned on the NEXT write (each write prunes, including
    # the row it just inserted if that row is itself past the cutoff).
    ancient_ts = _iso(now - timedelta(days=10))
    repo.record_state_snapshot(_snapshot(state="ancient", ts=ancient_ts))

    all_rows = repo.conn.execute("SELECT state FROM state_history").fetchall()
    states = [r["state"] for r in all_rows]
    assert "ancient" not in states
    assert "almost_week" in states


def test_record_state_snapshot_never_raises_on_bad_input():
    repo = Repository(":memory:")
    # a snapshot missing every optional key must still not raise (defensive by design)
    repo.record_state_snapshot({})
    rows = repo.state_history()
    assert len(rows) == 1
