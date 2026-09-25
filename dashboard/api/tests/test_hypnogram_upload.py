"""Uploading an EEG-headband hypnogram for a night and reading back the agreement report."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

from app.db import get_repo

NIGHT = "2025-03-01"
START = datetime(2025, 3, 1, 23, 0)


def _seed_ticks():
    repo = get_repo()
    try:
        repo.conn.execute("DELETE FROM raw_samples WHERE night_date = ?", (NIGHT,))
        rows = []
        for i in range(120):
            t = START + timedelta(seconds=30 * i + 10)
            stage = "awake" if i < 10 else ("deep" if 60 <= i < 90 else "light")
            rows.append((t.isoformat(), NIGHT, stage, 58.0,
                         "settling" if i < 10 else "maintenance", t.isoformat()))
        repo.conn.executemany(
            "INSERT INTO raw_samples (ts, night_date, stage, heart_rate, controller_state,"
            " sample_ts) VALUES (?,?,?,?,?,?)", rows)
        repo.conn.commit()
    finally:
        repo.close()


def _csv():
    lines = ["time,stage"]
    for i in range(120):
        stage = "W" if i < 10 else ("N3" if 60 <= i < 90 else "N2")
        lines.append(f"{(START + timedelta(seconds=30 * i)).isoformat(sep=' ')},{stage}")
    return "\n".join(lines).encode()


def test_upload_requires_auth(client):
    from fastapi.testclient import TestClient
    from app.main import app
    anon = TestClient(app)
    r = anon.post(f"/nights/{NIGHT}/hypnogram", content=_csv())
    assert r.status_code == 401


def test_upload_then_report(auth_client):
    _seed_ticks()
    r = auth_client.post(f"/nights/{NIGHT}/hypnogram?source=headband&restage=false",
                         content=_csv(), headers={"content-type": "text/csv"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["import"]["night_date"] == NIGHT and body["import"]["scored_epochs"] == 120
    rec = body["agreement"]["streams"]["recorded"]
    assert rec["kappa"] == 1.0 and rec["n_epochs"] == 120
    assert rec["minutes"]["eeg"]["deep"] == 15.0

    rep = auth_client.get(f"/nights/{NIGHT}/hypnogram?restage=true").json()
    assert rep["available"] and rep["source"] == "headband"
    assert "restaged" in rep["streams"]

    listed = auth_client.get("/hypnograms").json()
    assert any(n["night_date"] == NIGHT for n in listed["nights"])


def test_auto_night_files_under_the_overlapping_controller_night(auth_client):
    _seed_ticks()
    r = auth_client.post("/nights/auto/hypnogram?restage=false", content=_csv())
    assert r.status_code == 200, r.text
    assert r.json()["import"]["night_date"] == NIGHT


def test_bad_uploads_are_400s_with_a_reason(auth_client):
    r = auth_client.post(f"/nights/{NIGHT}/hypnogram", content=b"a,b\nfoo,bar\n")
    assert r.status_code == 400 and "could not import" in r.json()["detail"]
    assert auth_client.post(f"/nights/{NIGHT}/hypnogram", content=b"").status_code == 400
    assert auth_client.post("/nights/03-01-2025/hypnogram", content=_csv()).status_code == 400


def test_calibration_endpoint_reports_learning_until_enough_nights(auth_client):
    _seed_ticks()
    auth_client.post(f"/nights/{NIGHT}/hypnogram?restage=false", content=_csv())
    prof = auth_client.post("/staging/calibration").json()
    assert prof["enabled"] is False
    got = auth_client.get("/staging/calibration").json()
    assert got["enabled"] is False and "rationale" in got


def test_delete_removes_the_night(auth_client):
    _seed_ticks()
    auth_client.post(f"/nights/{NIGHT}/hypnogram?restage=false", content=_csv())
    r = auth_client.delete(f"/nights/{NIGHT}/hypnogram")
    assert r.json()["deleted_epochs"] == 120
    assert auth_client.get(f"/nights/{NIGHT}/hypnogram").json()["available"] is False


def _daemon_with(monkeypatch, profile_json, stager):
    ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    sys.path.insert(0, os.path.join(ROOT, "dashboard", "daemon"))
    from live_daemon import LiveDashboardDaemon
    from sleepctl.config import AppConfig
    from sleepctl.controller import state_estimator
    from sleepctl.loop.live import SimulatedLiveClient
    monkeypatch.setattr(state_estimator, "_get_stager", lambda: stager)
    repo = get_repo()
    repo.conn.execute(
        "INSERT INTO staging_calibration (id, ts, enabled, n_nights, profile) VALUES (1,'x',1,3,?)"
        " ON CONFLICT(id) DO UPDATE SET profile=excluded.profile", (profile_json,))
    repo.conn.commit()
    try:
        return LiveDashboardDaemon(AppConfig.default(), SimulatedLiveClient(scenario="normal",
                                   seed=7), repo, dry_run=True, verbose=False)
    finally:
        repo.conn.execute("DELETE FROM staging_calibration")
        repo.conn.commit()


class _Stager:
    def __init__(self):
        from sleepctl.learning.eeg_calibration import population_hmm
        self.hmm = population_hmm()
        self.hrv_available = False
        self.bias = None

    def set_wake_bias(self, b):
        self.bias = b

    def set_personal_hmm(self, trans, prior=None):
        pass


def test_daemon_start_up_applies_a_stored_calibration(monkeypatch):
    import json
    st = _Stager()
    trans = [list(r) for r in st.hmm["trans"]]
    prof = {"enabled": True, "hmm": {"trans": trans, "emission_prior": [0.3, 0.2, 0.3, 0.2],
                                     "temper": 0.42}}
    d = _daemon_with(monkeypatch, json.dumps(prof), st)
    assert d._deepen_policy is not None               # the rest of the profile load ran
    assert st.hmm["temper"] == 0.42 and st.hmm["emission_prior"] == [0.3, 0.2, 0.3, 0.2]
    assert st.bias == 1.0


def test_a_corrupt_calibration_never_breaks_start_up(monkeypatch):
    st = _Stager()
    before = dict(st.hmm)
    d = _daemon_with(monkeypatch, "{not json", st)
    assert d._deepen_policy is not None
    assert st.hmm == before
