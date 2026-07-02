"""API surface for the randomized efficacy MICRO-trials (sleepctl.ml.efficacy_trial): the
read-only ``/efficacy/trials`` current-estimate endpoint."""

from __future__ import annotations


def test_efficacy_trials_requires_auth():
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).get("/efficacy/trials").status_code == 401


def test_efficacy_trials_shape_with_no_data(auth_client):
    r = auth_client.get("/efficacy/trials")
    assert r.status_code == 200
    body = r.json()
    assert "config" in body and "analysis" in body
    assert body["n_nights_planned"] == body["n_eligible"] + body["n_ineligible"]
    cfg = body["config"]
    for key in ("enabled", "sham_fraction", "min_nights_before_verdict",
               "auto_stop_min_n", "auto_stop_threshold"):
        assert key in cfg
    analysis = body["analysis"]
    assert set(("n_active", "n_sham", "wake_events", "deep_pct", "hrv", "efficiency",
               "verdict", "enough_data")) <= set(analysis.keys())
    for metric in ("wake_events", "deep_pct", "hrv", "efficiency"):
        m = analysis[metric]
        for stat_key in ("diff", "ci_low", "ci_high", "p"):
            assert stat_key in m


def test_efficacy_trials_reflects_seeded_rows(auth_client):
    from app.db import get_repo

    repo = get_repo()
    try:
        for i in range(12):
            d = f"2026-05-{i + 1:02d}"
            repo.assign_efficacy_trial_night(d, "active", True, 0.05)
            repo.record_efficacy_trial_outcome(d, wake_events=1, deep_pct=0.2, hrv=60.0,
                                               efficiency=0.9)
        for i in range(12):
            d = f"2026-05-{i + 13:02d}"
            repo.assign_efficacy_trial_night(d, "sham", True, 0.05)
            repo.record_efficacy_trial_outcome(d, wake_events=4, deep_pct=0.2, hrv=58.0,
                                               efficiency=0.85)
    finally:
        repo.close()

    r = auth_client.get("/efficacy/trials")
    assert r.status_code == 200
    analysis = r.json()["analysis"]
    assert analysis["n_active"] >= 12 and analysis["n_sham"] >= 12
    assert analysis["enough_data"] is True
    assert analysis["wake_events"]["diff"] > 0  # sham worse -> active control helping
    assert "reduces awakenings" in analysis["verdict"]
