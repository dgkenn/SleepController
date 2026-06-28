"""iPhone-accelerometer ingest: /bcg/ingest derives movement (+best-effort HR) and publishes
it to the bridge for the daemon to fuse onto the Pod frame. Zero device risk (phone-only)."""

from __future__ import annotations

import math


def _accel_batch(secs=20.0, fs=50.0, bpm=60.0, moving=False):
    """A synthetic 3-axis accel batch: ~1 g gravity baseline on z + a small ballistic
    heartbeat, plus an optional gross-movement burst — what an iPhone on the mattress sees."""
    n = int(secs * fs)
    f_beat = bpm / 60.0
    ax, ay, az = [], [], []
    for i in range(n):
        t = i / fs
        beat = 0.03 * (math.sin(2 * math.pi * f_beat * t) ** 7)
        burst = 0.4 * math.sin(2 * math.pi * 6 * t) if moving else 0.0
        ax.append(beat + burst)
        ay.append(0.01 * math.sin(2 * math.pi * 0.25 * t) + burst)
        az.append(1.0 + beat + burst)            # gravity on z
    return {"fs": fs, "ax": ax, "ay": ay, "az": az, "source": "iphone"}


def test_ingest_requires_auth(client):
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).post("/bcg/ingest", json={"mag": [0.0]}).status_code == 401


def test_ingest_publishes_movement_to_bridge(auth_client):
    r = auth_client.post("/bcg/ingest", json=_accel_batch(moving=True))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["ingested"] > 0
    assert body["vitals"] is not None
    assert body["vitals"]["movement"] is not None

    # the bridge now holds a fresh phone sample the daemon can read
    from app import bridge
    from app.db import get_repo
    repo = get_repo()
    s = bridge.read_sensor_sample(repo.conn)
    repo.close()
    assert s is not None and s["movement"] is not None
    assert s["source"] == "iphone"
    assert s["age_seconds"] is not None and s["age_seconds"] < 30


def test_moving_batch_reads_more_movement_than_calm(auth_client):
    auth_client.post("/bcg/ingest", json=_accel_batch(moving=False))
    from app import bridge
    from app.db import get_repo
    repo = get_repo()
    calm = bridge.read_sensor_sample(repo.conn)["movement"]
    repo.close()

    auth_client.post("/bcg/ingest", json=_accel_batch(moving=True))
    repo = get_repo()
    moving = bridge.read_sensor_sample(repo.conn)["movement"]
    repo.close()
    assert moving > calm


def test_mag_form_is_accepted(auth_client):
    # the pre-computed 1-D magnitude form (single axis / already-collapsed) is accepted too.
    r = auth_client.post("/bcg/ingest", json={"fs": 50.0, "mag": [1.0] * 50})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["ingested"] == 50


def test_empty_batch_is_rejected_cleanly(auth_client):
    r = auth_client.post("/bcg/ingest", json={"fs": 50.0, "ax": [], "ay": [], "az": []})
    assert r.status_code == 200 and r.json()["ok"] is False


def _write_presence(presence):
    """Stamp a fresh runtime_state with a given bed presence (drives should-record)."""
    from app import bridge
    from app.db import get_repo
    repo = get_repo()
    bridge.write_runtime_state(repo.conn, {
        "state": "MAINTENANCE", "daemon_alive": True,
        "extra": {"power_on": True, "bed_presence": presence},
    })
    repo.close()


def test_should_record_follows_bed_presence(auth_client):
    _write_presence(True)
    assert auth_client.get("/bcg/should-record").json()["record"] is True
    _write_presence(False)
    body = auth_client.get("/bcg/should-record").json()
    assert body["record"] is False and body["presence"] is False
