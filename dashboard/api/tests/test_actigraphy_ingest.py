"""Actigraphy counts from the wearable's OWN accelerometer (Polar PMD ACC stream).

These are unit-comparable with the model's training data (same PIM/ZCM/MAD definitions as
scripts/reduce_motion_activity.py), unlike the iPhone's unitless 0..1 movement index -- so they
are stored separately and PREFERRED for the stager's activity features.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


_ACC = {"pim": 12.5, "zcm": 148, "mad": 0.0104, "std": 0.0223, "pmax": 0.178, "n": 1560, "fs": 52}


def test_hr_ingest_persists_actigraphy_counts(auth_client):
    from app.db import get_repo
    repo = get_repo()
    repo.conn.execute("DELETE FROM actigraphy")
    repo.conn.commit()
    repo.close()

    r = auth_client.post("/hr/ingest", json={"hr": 57, "rr": [1020.0, 1005.0],
                                             "source": "verity", "acc": _ACC})
    assert r.status_code == 200 and r.json()["ok"] is True

    repo = get_repo()
    row = repo.conn.execute("SELECT * FROM actigraphy").fetchone()
    repo.close()
    assert row is not None, "actigraphy counts were not persisted"
    assert abs(row["pim"] - 12.5) < 1e-6
    assert row["zcm"] == 148 and row["n"] == 1560
    assert abs(row["fs"] - 52.0) < 1e-6
    assert row["source"] == "verity"


def test_ingest_without_acc_still_works(auth_client):
    """The generic HR-service path sends no acc block; it must remain valid."""
    r = auth_client.post("/hr/ingest", json={"hr": 60, "rr": [1000.0, 1010.0]})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_actigraphy_is_preferred_over_phone_index(client):
    """When wearable counts exist they replace the phone index, and units are reported."""
    from app import bridge
    from app.db import get_repo

    repo = get_repo()
    repo.conn.execute("DELETE FROM actigraphy")
    repo.conn.execute("DELETE FROM sensor_samples")
    repo.conn.commit()

    now = datetime.now(timezone.utc)
    for i in range(5):
        ts = (now - timedelta(seconds=2 * (5 - i))).isoformat()
        repo.conn.execute(
            "INSERT INTO sensor_samples (ts, hr, hrv, movement, source) VALUES (?,?,?,?,?)",
            (ts, 58.0, 45.0, 0.04, "iphone"))
    repo.conn.commit()

    # phone-only first
    hist = bridge.sensor_history_series(repo.conn, minutes=30.0)
    assert hist["activity_units"] == "phone_index"
    assert all(v < 1.0 for _, v in hist["activity"])

    # now add wearable counts -> they win, and units flip
    bridge.append_actigraphy(repo.conn, _ACC, "verity")
    hist = bridge.sensor_history_series(repo.conn, minutes=30.0)
    repo.close()
    assert hist["activity_units"] == "counts"
    assert len(hist["activity"]) == 1
    assert abs(hist["activity"][0][1] - 12.5) < 1e-6


def test_actigraphy_write_is_best_effort(client):
    from app import bridge

    class Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db gone")

    bridge.append_actigraphy(Boom(), _ACC)          # must not raise
    assert bridge.recent_actigraphy(Boom()) == []


def test_garbage_acc_block_is_ignored(auth_client):
    from app.db import get_repo
    repo = get_repo()
    repo.conn.execute("DELETE FROM actigraphy")
    repo.conn.commit()
    repo.close()

    # no usable numeric fields -> nothing stored, ingest still succeeds
    r = auth_client.post("/hr/ingest", json={"hr": 60, "acc": {"pim": None, "zcm": "x"}})
    assert r.status_code == 200

    repo = get_repo()
    n = repo.conn.execute("SELECT COUNT(*) c FROM actigraphy").fetchone()["c"]
    repo.close()
    assert n == 0
