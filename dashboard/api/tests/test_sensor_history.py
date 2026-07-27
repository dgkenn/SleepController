"""Dense trailing sensor history for the wearable sleep-stager.

The per-tick controller frame carries only ~1 sample/minute, but the Verity forwarder writes ~1 HR
sample every 2 s into ``sensor_samples``. Short-timescale HR variability is a major sleep-staging
signal, so the daemon hands the stager this DENSE series instead of the frame buffer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _seed(conn, n=30, step_s=2.0, hr=58.0, movement=0.04):
    """Insert ``n`` sensor_samples spaced ``step_s`` apart, ending now."""
    now = datetime.now(timezone.utc)
    for i in range(n):
        ts = (now - timedelta(seconds=step_s * (n - i))).isoformat()
        conn.execute(
            "INSERT INTO sensor_samples (ts, hr, hrv, movement, source, fs, n_samples)"
            " VALUES (?,?,?,?,?,?,?)",
            (ts, hr + (i % 3), 45.0, movement, "verity", None, 0),
        )
    conn.commit()


def test_sensor_history_series_returns_dense_hr(client):
    from app import bridge
    from app.db import get_repo
    repo = get_repo()
    repo.conn.execute("DELETE FROM sensor_samples")
    # These tests assert on the ACTIVITY series, and sensor_history_series prefers wearable
    # actigraphy counts over the phone index whenever any exist -- so this test owns that table
    # too, or a stray row left by an earlier test silently replaces the series under it.
    repo.conn.execute("DELETE FROM actigraphy")
    repo.conn.commit()
    _seed(repo.conn, n=40, step_s=2.0)
    hist = bridge.sensor_history_series(repo.conn, minutes=45.0)
    repo.close()

    assert len(hist["hr"]) == 40, "dense HR series should carry every ingested sample"
    assert len(hist["activity"]) == 40
    # ascending timestamps, (epoch_seconds, value) pairs
    ts = [t for t, _ in hist["hr"]]
    assert ts == sorted(ts)
    assert all(isinstance(t, float) and isinstance(v, float) for t, v in hist["hr"])
    # denser than 1/minute — the whole point
    span_min = (ts[-1] - ts[0]) / 60.0
    assert len(ts) / max(span_min, 1e-9) > 5.0


def test_sensor_history_series_respects_window(client):
    from app import bridge
    from app.db import get_repo
    repo = get_repo()
    repo.conn.execute("DELETE FROM sensor_samples")
    # These tests assert on the ACTIVITY series, and sensor_history_series prefers wearable
    # actigraphy counts over the phone index whenever any exist -- so this test owns that table
    # too, or a stray row left by an earlier test silently replaces the series under it.
    repo.conn.execute("DELETE FROM actigraphy")
    repo.conn.commit()
    # one very old row + fresh rows; the old one must fall outside the window
    old = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    repo.conn.execute(
        "INSERT INTO sensor_samples (ts, hr, hrv, movement, source) VALUES (?,?,?,?,?)",
        (old, 99.0, 10.0, 0.9, "verity"))
    repo.conn.commit()
    _seed(repo.conn, n=10, step_s=2.0)
    hist = bridge.sensor_history_series(repo.conn, minutes=30.0)
    repo.close()
    assert len(hist["hr"]) == 10
    assert all(v < 90 for _, v in hist["hr"]), "6h-old sample must be excluded"


def test_sensor_history_series_never_raises_on_bad_db(client):
    """Best-effort contract: the stager must degrade to the frame buffer, never crash the tick."""
    from app import bridge

    class Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db gone")

    out = bridge.sensor_history_series(Boom(), minutes=45.0)
    assert out["hr"] == [] and out["activity"] == [] and out["activity_units"] is None


def test_bridge_wearable_source_exposes_history(client):
    from app.db import get_repo
    from sleepctl.adapters.bcg import BridgeWearableSource
    repo = get_repo()
    repo.conn.execute("DELETE FROM sensor_samples")
    # These tests assert on the ACTIVITY series, and sensor_history_series prefers wearable
    # actigraphy counts over the phone index whenever any exist -- so this test owns that table
    # too, or a stray row left by an earlier test silently replaces the series under it.
    repo.conn.execute("DELETE FROM actigraphy")
    repo.conn.commit()
    _seed(repo.conn, n=20, step_s=2.0)
    hist = BridgeWearableSource(repo).read_history(minutes=45.0)
    repo.close()
    assert len(hist["hr"]) == 20


def test_estimator_prefers_dense_history_over_frame_buffer():
    """``_hr_series`` must use frame.hr_history when present (dense), else the frame buffer."""
    from sleepctl.controller.state_estimator import _hr_series
    from sleepctl.models import SensorFrame, SleepStage

    now = datetime(2026, 6, 23, 1, 0)
    sparse = [SensorFrame(timestamp=now, stage=SleepStage.UNKNOWN, heart_rate=60.0)]
    frame = SensorFrame(timestamp=now, stage=SleepStage.UNKNOWN, heart_rate=60.0)

    # no dense history -> falls back to the frame buffer
    assert len(_hr_series(sparse, frame)) == 2

    # dense history present -> used verbatim
    frame.hr_history = [(1000.0 + i * 2.0, 57.0) for i in range(50)]
    dense = _hr_series(sparse, frame)
    assert len(dense) == 50
    assert dense[0] == (1000.0, 57.0)
