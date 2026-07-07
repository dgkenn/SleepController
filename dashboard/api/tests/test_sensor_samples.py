"""Phone-BCG sensor-samples history: append-only accumulation (unlike the ``live_sensor``
singleton the daemon reads for real-time fusion) + the token-gated /diag/sensor-history export.

Covers the persistence gap: overnight phone data must ACCUMULATE for later model training /
nightly learning instead of being overwritten batch-to-batch."""

from __future__ import annotations

import math


def _accel_batch(secs=20.0, fs=50.0, bpm=60.0):
    """A synthetic 3-axis accel batch: ~1 g gravity baseline on z + a small ballistic heartbeat
    -- long enough (>=5s) for BCGProcessor.vitals() to yield a reading, so /bcg/ingest actually
    writes a sample."""
    n = int(secs * fs)
    f_beat = bpm / 60.0
    ax, ay, az = [], [], []
    for i in range(n):
        t = i / fs
        beat = 0.03 * (math.sin(2 * math.pi * f_beat * t) ** 7)
        ax.append(beat)
        ay.append(0.01 * math.sin(2 * math.pi * 0.25 * t))
        az.append(1.0 + beat)
    return {"fs": fs, "ax": ax, "ay": ay, "az": az, "source": "iphone"}


def test_record_and_read_sensor_samples():
    from app import bridge
    from app.db import connect

    conn = connect(":memory:")

    bridge.append_sensor_sample(conn, {
        "hr": 58.0, "hrv": 40.0, "movement": 0.1, "source": "iphone", "fs": 50.0, "n_samples": 1000,
    })
    bridge.append_sensor_sample(conn, {
        "hr": 61.0, "hrv": 35.0, "movement": 0.3, "source": "iphone", "fs": 50.0, "n_samples": 1000,
    })

    rows = bridge.recent_sensor_samples(conn, limit=10)
    assert len(rows) == 2
    # most-recent first (ts DESC)
    newest, oldest = rows[0], rows[1]
    assert newest["hr"] == 61.0 and newest["movement"] == 0.3
    assert oldest["hr"] == 58.0 and oldest["movement"] == 0.1
    assert newest["source"] == "iphone" and newest["fs"] == 50.0 and newest["n_samples"] == 1000

    conn.close()


def test_recent_sensor_samples_respects_limit():
    from app import bridge
    from app.db import connect

    conn = connect(":memory:")
    for i in range(5):
        bridge.append_sensor_sample(conn, {
            "hr": 60.0 + i, "hrv": 30.0, "movement": 0.1, "source": "iphone",
            "fs": 50.0, "n_samples": 500,
        })
    assert len(bridge.recent_sensor_samples(conn, limit=3)) == 3
    conn.close()


def test_ingest_bcg_appends_history_without_disturbing_live_singleton(auth_client):
    """Two ingest batches must produce TWO rows in sensor_samples (append, not overwrite) while
    live_sensor -- the daemon's real-time-fusion read -- still holds exactly one row."""
    from app import bridge
    from app.db import get_repo

    repo = get_repo()
    before = len(bridge.recent_sensor_samples(repo.conn, limit=10000))
    repo.close()

    r1 = auth_client.post("/bcg/ingest", json=_accel_batch(bpm=58.0))
    assert r1.status_code == 200 and r1.json()["vitals"] is not None

    r2 = auth_client.post("/bcg/ingest", json=_accel_batch(bpm=72.0))
    assert r2.status_code == 200 and r2.json()["vitals"] is not None

    repo = get_repo()
    history = bridge.recent_sensor_samples(repo.conn, limit=10000)
    live = repo.conn.execute("SELECT COUNT(*) c FROM live_sensor").fetchone()["c"]
    latest = bridge.read_sensor_sample(repo.conn)
    repo.close()

    assert len(history) == before + 2       # append, not overwrite
    assert live == 1                        # singleton contract for the daemon is untouched
    assert latest is not None and latest["source"] == "iphone"
    # newest history row (ts DESC) reflects the latest ingest, same as the singleton
    assert history[0]["movement"] is not None


def test_diag_sensor_history_requires_token(client, monkeypatch):
    # disabled by default (no DIAG_TOKEN) -> 404 even with a token
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.get("/diag/sensor-history?token=whatever").status_code == 404

    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.get("/diag/sensor-history").status_code == 404          # no token
    assert client.get("/diag/sensor-history?token=nope").status_code == 404  # wrong token


def test_diag_sensor_history_returns_seeded_rows(client, monkeypatch, auth_client):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")

    auth_client.post("/bcg/ingest", json=_accel_batch(bpm=65.0))

    r = client.get("/diag/sensor-history?token=s3cret-xyz&limit=5")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list) and len(rows) >= 1
    assert rows[0]["source"] == "iphone"
