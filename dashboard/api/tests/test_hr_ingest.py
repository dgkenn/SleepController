"""Dedicated cardiac-sensor ingest (Polar Verity Sense): /hr/ingest takes HR + RR intervals,
computes HRV (RMSSD), and publishes an AUTHORITATIVE cardiac channel that ``read_fused_sensor``
merges with — never clobbering — the iPhone accelerometer's movement channel. Zero device risk
(an independent sensor; the Pod is never touched)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


# ----------------------------------------------------------------- endpoint + HRV math
def test_hr_ingest_requires_auth(client):
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).post("/hr/ingest", json={"hr": 60}).status_code == 401


def test_rmssd_known_value():
    from app import services
    # diffs = [+50, -50] -> squares [2500, 2500] -> mean 2500 -> sqrt = 50 ms
    assert abs(services._rmssd([800.0, 850.0, 800.0]) - 50.0) < 1e-6
    assert services._rmssd([1000.0]) is None          # need >= 2 intervals
    assert services._rmssd([]) is None


def test_hr_ingest_writes_cardiac_and_computes_hrv(auth_client):
    r = auth_client.post("/hr/ingest", json={"hr": 58, "rr": [1010, 1032, 998, 1005],
                                             "source": "verity"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["hr"] == 58
    assert body["hrv"] is not None and body["hrv"] > 0
    assert body["rr_count"] == 4

    from app import bridge
    from app.db import get_repo
    repo = get_repo()
    c = bridge.read_cardiac_sample(repo.conn)
    repo.close()
    assert c is not None and c["hr"] == 58 and c["source"] == "verity"
    assert c["age_seconds"] is not None and c["age_seconds"] < 30


def test_hr_ingest_derives_hr_from_rr_when_absent(auth_client):
    # RR all 1000 ms -> mean interval 1000 ms -> 60000/1000 = 60 bpm
    r = auth_client.post("/hr/ingest", json={"rr": [1000, 1000, 1000, 1000]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert abs(body["hr"] - 60.0) < 0.5


def test_hr_ingest_rejects_empty_batch(auth_client):
    r = auth_client.post("/hr/ingest", json={"source": "verity"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


# ----------------------------------------------------------------- per-field fusion / merge
def _write_phone(conn, *, hr=None, hrv=None, movement=None, source="iphone"):
    from app import bridge
    bridge.write_sensor_sample(conn, {"hr": hr, "hrv": hrv, "movement": movement, "source": source})


def _write_cardiac(conn, *, hr=None, hrv=None, source="verity", age_s=0.0):
    from app import bridge
    bridge.write_cardiac_sample(conn, {"hr": hr, "hrv": hrv, "source": source})
    if age_s:
        old = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
        conn.execute("UPDATE live_cardiac SET updated=? WHERE id=1", (old,))
        conn.commit()


def test_fused_merges_phone_movement_with_verity_hr():
    from app import bridge
    from app.db import get_repo
    repo = get_repo()
    _write_phone(repo.conn, movement=0.30)                 # phone: movement only (hr best-effort None)
    _write_cardiac(repo.conn, hr=60, hrv=45)               # verity: authoritative HR/HRV
    fused = bridge.read_fused_sensor(repo.conn)
    repo.close()
    assert fused["movement"] == 0.30                        # movement from the phone
    assert fused["hr"] == 60 and fused["hrv"] == 45         # HR/HRV from the Verity
    assert fused["hr_source"] == "verity"


def test_verity_hr_is_authoritative_over_phone_best_effort():
    from app import bridge
    from app.db import get_repo
    repo = get_repo()
    _write_phone(repo.conn, hr=55, movement=0.1)           # phone best-effort HR present...
    _write_cardiac(repo.conn, hr=60)                        # ...but the Verity wins
    fused = bridge.read_fused_sensor(repo.conn)
    repo.close()
    assert fused["hr"] == 60 and fused["hr_source"] == "verity"


def test_stale_verity_falls_back_to_phone_hr():
    from app import bridge
    from app.db import get_repo
    repo = get_repo()
    _write_phone(repo.conn, hr=55, movement=0.1)           # fresh phone HR
    _write_cardiac(repo.conn, hr=60, age_s=300)            # Verity HR is 5 min stale -> dropped
    fused = bridge.read_fused_sensor(repo.conn)
    repo.close()
    assert fused["hr"] == 55 and fused["hr_source"] == "iphone"


def test_bridge_wearable_source_returns_merged_sample():
    from app.db import get_repo
    from sleepctl.adapters.bcg import BridgeWearableSource
    repo = get_repo()
    _write_phone(repo.conn, movement=0.22)
    _write_cardiac(repo.conn, hr=61, hrv=40)
    sample = BridgeWearableSource(repo).read_sample()
    repo.close()
    assert sample is not None
    assert sample.movement == 0.22
    assert sample.heart_rate == 61 and sample.hrv == 40
    assert sample.age_seconds is not None and sample.age_seconds < 30
