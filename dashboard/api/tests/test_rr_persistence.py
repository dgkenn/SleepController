"""Raw beat-to-beat RR intervals must be PERSISTED, not just reduced to a single RMSSD scalar.

The goal is a stager tailored to this one user, so each night's raw interval series is
irreplaceable training data: every HRV metric (SDNN, pNN50, Poincare SD1/SD2, LF/HF) derives from
it and none of it can be reconstructed after the fact.
"""

from __future__ import annotations

import json

import pytest


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


def test_published_hrv_uses_a_window_not_the_2s_batch(client):
    """ROOT-CAUSE REGRESSION. The forwarder POSTs every --batch-seconds (default 2.0) = ~6 beats,
    and RMSSD over 6 beats is noise, not HRV. Measured live: per-batch RMSSD had sd 3.3 ms across
    a 10-23 ms range while the same intervals over a proper window sat steady at 24.7 ms -- both
    jittery and ~34% biased low, because a 2 s window misses the slow respiratory variation that
    dominates real RMSSD.

    That noise made SleepOnsetDetector's `hrv_rise` signal flicker (18 of 55 frames on a real
    night). With respiration paywalled, only `asleep_stage` and `stillness` were dependable, so a
    flickering third signal meant onset could never persist for its required 10 unbroken minutes
    and sleep was NEVER detected. Publishing a windowed RMSSD is the fix.
    """
    from app.db import get_repo
    from app import bridge, services

    repo = get_repo()
    repo.conn.execute("DELETE FROM rr_intervals")
    repo.conn.commit()

    # A history whose TRUE variability is large (alternating 900/1000 ms -> RMSSD ~100 ms)...
    history = [900.0, 1000.0] * 30
    bridge.append_rr_intervals(repo.conn, history, "verity")

    # ...then a 2-second batch that happens to land on near-identical beats, whose own RMSSD is
    # ~1 ms. The published HRV must reflect the window, not this misleadingly flat batch.
    flat_batch = [950.0, 951.0, 950.0, 951.0, 950.0, 951.0]
    out = services.ingest_hr(repo, {"hr": 60, "rr": flat_batch, "source": "verity"})

    assert out["ok"] is True
    batch_only = services._rmssd(flat_batch)
    assert batch_only is not None and batch_only < 5.0      # the batch alone looks ~flat
    assert out["hrv"] is not None
    assert out["hrv"] > 20.0, (
        f"published HRV {out['hrv']} tracked the 2s batch ({batch_only:.1f} ms) instead of the "
        "window -- the noise that prevented sleep onset from ever confirming")
    repo.close()


def test_windowed_hrv_falls_back_to_the_batch_when_history_is_too_sparse(client):
    """Must not publish a worse number than the batch value: too few intervals in the window
    (fresh start, gap, armband just put on) keeps the per-batch estimate rather than a windowed
    estimate computed from almost nothing."""
    from app.db import get_repo
    from app import bridge, services

    repo = get_repo()
    repo.conn.execute("DELETE FROM rr_intervals")
    repo.conn.commit()

    batch = [900.0, 1000.0, 900.0, 1000.0]          # only 4 intervals total, under the floor
    out = services.ingest_hr(repo, {"hr": 60, "rr": batch, "source": "verity"})

    assert out["ok"] is True
    assert out["hrv"] == pytest.approx(services._rmssd(batch), rel=1e-6)
    repo.close()
