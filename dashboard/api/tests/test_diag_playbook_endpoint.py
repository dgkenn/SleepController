"""Tests for GET /diag/playbook -- the known-issue playbook catalog + which entries currently
match this instance's live diagnostics -- and its wiring into /diag's plaintext output."""

from __future__ import annotations

from app import bridge
from app.db import get_repo


def test_diag_playbook_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.get("/diag/playbook").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.get("/diag/playbook").status_code == 404
    assert client.get("/diag/playbook?token=nope").status_code == 404


def test_diag_playbook_returns_full_catalog(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    r = client.get("/diag/playbook?token=s3cret-xyz")
    assert r.status_code == 200
    body = r.json()
    assert "entries" in body and "matches" in body
    ids = {e["id"] for e in body["entries"]}
    expected = {
        "water_reservoir_empty", "watchdog_restart_storm", "daemon_heartbeat_stale",
        "dry_run_left_on", "pyeight_auth_failure", "no_credentials_configured",
        "db_locked", "port_in_use", "calendar_ics_unreachable", "device_offline",
    }
    assert expected.issubset(ids)
    for entry in body["entries"]:
        assert {"id", "symptom", "likely_cause", "fix", "auto_fixable", "matched"}.issubset(
            entry.keys())


def test_diag_playbook_matches_water_reservoir_when_has_water_false(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    repo = get_repo()
    try:
        bridge.write_runtime_state(repo.conn, {
            "state": "IDLE",
            "extra": {
                "live": True, "dry_run": False,
                "device": {"online": True, "has_water": False, "priming": False,
                          "needs_priming": False},
            },
        })
    finally:
        repo.close()

    r = client.get("/diag/playbook?token=s3cret-xyz")
    assert r.status_code == 200
    body = r.json()

    matched_ids = {m["id"] for m in body["matches"]}
    assert "water_reservoir_empty" in matched_ids

    entry_by_id = {e["id"]: e for e in body["entries"]}
    assert entry_by_id["water_reservoir_empty"]["matched"] is True


def test_diag_text_output_shows_likely_causes_section_when_matched(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    repo = get_repo()
    try:
        bridge.write_runtime_state(repo.conn, {
            "state": "IDLE",
            "extra": {
                "live": True, "dry_run": False,
                "device": {"online": True, "has_water": False, "priming": False,
                          "needs_priming": False},
            },
        })
    finally:
        repo.close()

    r = client.get("/diag?token=s3cret-xyz")
    assert r.status_code == 200
    assert "=== LIKELY CAUSES & FIXES ===" in r.text
    assert "reservoir" in r.text.lower()

    # the lossless JSON form must carry the same matches
    r_json = client.get("/diag?token=s3cret-xyz&format=json")
    assert r_json.status_code == 200
    matches = r_json.json().get("playbook_matches") or []
    assert any(m["id"] == "water_reservoir_empty" for m in matches)
