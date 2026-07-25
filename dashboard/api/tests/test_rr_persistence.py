"""Raw beat-to-beat RR intervals must be PERSISTED, not just reduced to a single RMSSD scalar.

The goal is a stager tailored to this one user, so each night's raw interval series is
irreplaceable training data: every HRV metric (SDNN, pNN50, Poincare SD1/SD2, LF/HF) derives from
it and none of it can be reconstructed after the fact.
"""

from __future__ import annotations

import json


def test_hr_ingest_persists_raw_rr(auth_client):
    from app import bridge
    from app.db import get_repo

    repo = get_repo()
    repo.conn.execute("DELETE FROM rr_intervals")
    repo.conn.commit()
    repo.close()

    rr = [1010.0, 1032.5, 998.2, 1005.0, 1021.0]
    r = auth_client.post("/hr/ingest", json={"hr": 58, "rr": rr, "source": "verity"})
    assert r.status_code == 200 and r.json()["ok"] is True

    repo = get_repo()
    rows = repo.conn.execute("SELECT ts, rr_ms, n, source FROM rr_intervals").fetchall()
    series = bridge.recent_rr_intervals(repo.conn, minutes=60.0)
    repo.close()

    assert len(rows) == 1, "the raw RR batch was not persisted"
    stored = json.loads(rows[0]["rr_ms"])
    assert len(stored) == len(rr)
    assert abs(stored[0] - 1010.0) < 0.5
    assert rows[0]["n"] == len(rr) and rows[0]["source"] == "verity"

    # flattened reader returns (epoch_seconds, rr_ms) pairs for feature computation / training
    assert len(series) == len(rr)
    assert all(isinstance(t, float) and isinstance(v, float) for t, v in series)


def test_non_physiological_rr_is_filtered_before_storage(auth_client):
    from app.db import get_repo
    repo = get_repo()
    repo.conn.execute("DELETE FROM rr_intervals")
    repo.conn.commit()
    repo.close()

    # 40 ms and 9000 ms are impossible intervals (sensor artifact) and must not be stored
    auth_client.post("/hr/ingest", json={"hr": 60, "rr": [40.0, 1000.0, 9000.0, 1010.0]})

    repo = get_repo()
    row = repo.conn.execute("SELECT rr_ms FROM rr_intervals").fetchone()
    repo.close()
    stored = json.loads(row["rr_ms"])
    assert stored == [1000.0, 1010.0], f"artifacts not filtered: {stored}"


def test_rr_persistence_never_breaks_ingest(auth_client):
    """Best-effort contract: a telemetry failure must not fail the real-time ingest path."""
    from app import bridge

    class Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db gone")

    bridge.append_rr_intervals(Boom(), [1000.0, 1010.0])  # must not raise
    assert bridge.recent_rr_intervals(Boom(), minutes=10.0) == []


def test_rr_history_endpoint_is_token_gated(client):
    # no DIAG_TOKEN configured in tests -> invisible (404), never a 401 that confirms existence
    assert client.get("/diag/rr-history").status_code == 404
    assert client.get("/diag/rr-history?token=wrong").status_code == 404


def test_rr_history_endpoint_returns_series(client, monkeypatch):
    import os
    from app.db import get_repo
    from app import bridge

    repo = get_repo()
    repo.conn.execute("DELETE FROM rr_intervals")
    repo.conn.commit()
    bridge.append_rr_intervals(repo.conn, [1000.0, 1012.0, 995.0], "verity")
    repo.close()

    monkeypatch.setenv("DIAG_TOKEN", "s3cret")
    r = client.get("/diag/rr-history?token=s3cret&minutes=60")
    assert r.status_code == 200
    body = r.json()
    assert body["n"] == 3 and len(body["rr"]) == 3
    os.environ.pop("DIAG_TOKEN", None)
