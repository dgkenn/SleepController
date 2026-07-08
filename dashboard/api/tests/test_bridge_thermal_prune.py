"""Retention pruning for ``thermal_samples`` (dashboard-layer table -- see ``db.py``'s
_DASHBOARD_DDL). Unlike events/state_history/sensor_samples, this table was never pruned before,
growing unbounded for the life of the DB on a 24/7 box. Mirrors the sleepctl-core coverage in
tests/test_retention_pruning.py (raw_samples/decisions/interventions)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import bridge
from app.db import connect


def test_prune_thermal_samples_keeps_recent_deletes_old():
    conn = connect(":memory:")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    recent_ts = datetime.now(timezone.utc).isoformat()
    for _ in range(6):
        conn.execute("INSERT INTO thermal_samples (ts) VALUES (?)", (old_ts,))
    for _ in range(2):
        conn.execute("INSERT INTO thermal_samples (ts) VALUES (?)", (recent_ts,))
    conn.commit()

    deleted = bridge.prune_thermal_samples(conn, keep_days=45)
    assert deleted == 6
    remaining = conn.execute("SELECT COUNT(*) c FROM thermal_samples").fetchone()["c"]
    assert remaining == 2
    conn.close()


def test_prune_thermal_samples_never_raises_on_bad_connection():
    conn = connect(":memory:")
    conn.close()
    assert bridge.prune_thermal_samples(conn) == 0
