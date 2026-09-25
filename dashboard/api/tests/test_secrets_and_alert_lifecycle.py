"""Credentials stay server-side, bad tokens are refused rather than crashing, and alerts neither
duplicate nor re-buzz a phone the user already answered.

  * GET /settings returned every settings_kv row verbatim -- including the Tuya plug's
    local_key, the Hue bridge token and the private calendar ICS URL.
  * ``secrets.compare_digest`` on two str raises TypeError for any non-ASCII character, so
    ``?token=%C3%A9`` 500'd every token-gated endpoint instead of 404/401.
  * Acknowledging a still-true critical health alert re-raised and re-pushed it on the next poll.
  * The per-day alert de-dup compared a LOCAL date against the UTC timestamp it had written.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import pytest


@pytest.fixture()
def repo(tmp_path):
    from sleepctl.storage.repository import Repository
    from app import db as app_db
    r = Repository(str(tmp_path / "sec.db"), check_same_thread=False)
    r.conn.executescript(app_db._DASHBOARD_DDL)
    app_db._apply_migrations(r.conn)
    r.conn.commit()
    yield r
    r.close()


# ---------------------------------------------------------------------- GET /settings redaction
def test_settings_never_returns_stored_credentials(auth_client):
    from app.db import get_repo
    r = get_repo()
    try:
        rows = {
            "wake_plug_config": {"enabled": True, "backend": "tuya",
                                 "config": {"device_id": "d1", "local_key": "S3cr3tLocalKey!!",
                                            "ip": "10.0.0.5"}},
            "hue_config": {"enabled": True, "bridge_ip": "10.0.0.2", "token": "HUE-TOKEN-abc"},
            "calendar_config": {"enabled": True,
                                "ics_url": "https://calendar.example/private-abc/basic.ics"},
            "hrv_target_ms": 55,
        }
        for k, v in rows.items():
            r.conn.execute("INSERT INTO settings_kv (key, value) VALUES (?,?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (k, json.dumps(v)))
        r.conn.commit()
    finally:
        r.close()
    resp = auth_client.get("/settings")
    assert resp.status_code == 200
    text = resp.text
    for secret in ("S3cr3tLocalKey!!", "HUE-TOKEN-abc", "private-abc"):
        assert secret not in text
    stored = resp.json()["stored"]
    assert stored["wake_plug_config"]["config"] == {"device_id": "d1", "local_key": "***",
                                                    "ip": "10.0.0.5"}
    assert stored["hue_config"]["token"] == "***"
    assert stored["hue_config"]["bridge_ip"] == "10.0.0.2"
    assert stored["calendar_config"]["ics_url"] == "***"
    # what the Settings page actually reads is untouched
    assert stored["hrv_target_ms"] == 55


def test_redaction_is_deep_and_keeps_unset_secrets_distinguishable():
    from app.main import _redact_secrets
    v = {"a": [{"api_key": "k"}, {"password": ""}], "Hue_Token": None, "n": 3,
         "nested": {"client_secret": {"x": 1}}}
    assert _redact_secrets(v) == {"a": [{"api_key": "***"}, {"password": ""}],
                                  "Hue_Token": None, "n": 3, "nested": {"client_secret": "***"}}


# ---------------------------------------------------------------------- non-ASCII tokens
@pytest.fixture()
def raw_client(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv("DIAG_TOKEN", "real-diag-token")
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path", ["/diag?token=%C3%A9", "/diag/all?token=%C3%A9",
                                  "/diag/events?token=%C3%A9", "/diag?token=r%C3%A9al"])
def test_a_non_ascii_diag_token_is_a_404_not_a_crash(raw_client, path):
    assert raw_client.get(path).status_code == 404


def test_a_non_ascii_ingest_token_is_a_401_not_a_crash(raw_client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "bcg_ingest_token", "static-ingest")
    monkeypatch.setattr(settings, "bcg_ingest_open", False)
    assert raw_client.post("/hr/ingest?token=%C3%A9", json={"hr": 60}).status_code == 401
    assert raw_client.get("/bcg/should-record?token=%C3%A9").status_code == 401


def test_the_secret_compare_still_matches_and_never_raises():
    from app.main import _secret_eq
    assert _secret_eq("real-diag-token", "real-diag-token")
    assert _secret_eq("é-token", "é-token")
    assert not _secret_eq("é", "real-diag-token")
    assert not _secret_eq("", "x") and not _secret_eq(None, "x")


# ---------------------------------------------------------------------- acked health alerts
def _stale_runtime(repo):
    repo.conn.execute("DELETE FROM runtime_state")
    repo.conn.commit()


def _fresh_runtime(repo):
    from app import bridge
    bridge.write_runtime_state(repo.conn, {"state": "idle", "extra": {}})


def test_an_acknowledged_critical_alert_stays_quiet_until_it_clears(repo, monkeypatch):
    from app import push_sender, services
    sent = []
    monkeypatch.setattr(push_sender, "deliver",
                        lambda issue, subs, *a, **k: (sent.append(issue["code"]),
                                                      push_sender.PushResult(ok=True))[1])
    _stale_runtime(repo)
    assert services.evaluate_and_sync_health_alerts(repo)["newly_raised"] == ["daemon_down"]
    services.evaluate_and_sync_health_alerts(repo)
    assert sent == ["daemon_down"]

    (aid,) = repo.conn.execute(
        "SELECT id FROM alerts WHERE type='health_daemon_down' AND acknowledged=0").fetchone()
    assert services.acknowledge_alert(repo, aid) == {"ok": True}
    for _ in range(3):
        s = services.evaluate_and_sync_health_alerts(repo)
        assert s["newly_raised"] == [] and s["suppressed"] == ["daemon_down"]
    assert sent == ["daemon_down"]
    assert repo.conn.execute("SELECT COUNT(*) FROM alerts WHERE type='health_daemon_down' "
                             "AND acknowledged=0").fetchone()[0] == 0

    # the condition clears -> the ack is forgotten, so a NEW outage raises and pushes again
    _fresh_runtime(repo)
    services.evaluate_and_sync_health_alerts(repo)
    assert "daemon_down" not in services._acked_health_codes(repo)
    _stale_runtime(repo)
    assert "daemon_down" in services.evaluate_and_sync_health_alerts(repo)["newly_raised"]
    assert sent == ["daemon_down", "daemon_down"]


def test_the_ack_endpoint_records_a_health_ack_and_ignores_ordinary_alerts(auth_client):
    from app import services
    from app.db import get_repo
    r = get_repo()
    try:
        r.conn.execute("DELETE FROM settings_kv WHERE key='health_alert_acked'")
        now = datetime.now(timezone.utc).isoformat()
        cur = r.conn.execute("INSERT INTO alerts (ts, type, severity, message, acknowledged) "
                             "VALUES (?,?,?,?,0)", (now, "health_no_water", "critical", "m"))
        hid = cur.lastrowid
        cur = r.conn.execute("INSERT INTO alerts (ts, type, severity, message, acknowledged) "
                             "VALUES (?,?,?,?,0)", (now, "short_sleep", "info", "m"))
        oid = cur.lastrowid
        r.conn.commit()
    finally:
        r.close()
    assert auth_client.post(f"/alerts/{hid}/ack").json() == {"ok": True}
    assert auth_client.post(f"/alerts/{oid}/ack").json() == {"ok": True}
    r = get_repo()
    try:
        assert services._acked_health_codes(r) == {"no_water"}
        assert r.conn.execute("SELECT COUNT(*) FROM alerts WHERE id IN (?,?) AND acknowledged=1",
                              (hid, oid)).fetchone()[0] == 2
        r.conn.execute("DELETE FROM settings_kv WHERE key='health_alert_acked'")
        r.conn.commit()
    finally:
        r.close()


# ---------------------------------------------------------------------- per-day alert de-dup
@pytest.fixture(params=["Etc/GMT+12", "Etc/GMT-14"])
def far_tz(request, monkeypatch):
    """A zone whose local date differs from the UTC date for half of every day."""
    monkeypatch.setenv("TZ", request.param)
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_a_repeated_alert_is_stored_once_per_day_in_any_zone(repo, far_tz):
    from app import services
    for _ in range(5):
        services._add_alert(repo, "stale_data", "critical", "stale")
    assert repo.conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE type='stale_data'").fetchone()[0] == 1
