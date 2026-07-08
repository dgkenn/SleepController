"""Tests for the one-click repair (#2) + token-gated remote recovery (#12) + morning report
(#6) endpoints added at the end of ``app/main.py``. Gated identically to ``/diag`` (secret
``DIAG_TOKEN``, constant-time compare, 404 on missing/wrong token) except the morning-report
READ view, which is session-auth-gated like the rest of the dashboard.
"""

from __future__ import annotations

import json
import os

from app.bridge import VALID_COMMANDS, write_runtime_state
from app.main import _run_dir


def _clear_commands():
    from app.db import get_repo
    repo = get_repo()
    try:
        repo.conn.execute("DELETE FROM commands")
        repo.conn.commit()
    finally:
        repo.close()


def _clear_kv(*keys):
    from app.db import get_repo
    repo = get_repo()
    try:
        for k in keys:
            repo.conn.execute("DELETE FROM settings_kv WHERE key=?", (k,))
        repo.conn.commit()
    finally:
        repo.close()


def _set_healthy_runtime():
    from app.db import get_repo
    repo = get_repo()
    try:
        write_runtime_state(repo.conn, {
            "state": "IDLE", "objective": "OPTIMIZE", "mode": "auto",
            "target_temp_f": 68.0, "bed_temp_f": 70.0, "room_temp_f": 68.0,
            "stage": "unknown", "confidence": 0.8, "target_level": -50,
            "daemon_alive": True,
            "extra": {"device": {"online": True, "has_water": True, "needs_priming": False},
                      "thermal_health": {"state": "ok"}},
        })
    finally:
        repo.close()


# ------------------------------------------------------------------ /diag/repair
def test_diag_repair_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.post("/diag/repair").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.post("/diag/repair").status_code == 404
    assert client.post("/diag/repair?token=nope").status_code == 404


def test_diag_repair_reports_each_subaction(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    _clear_commands()
    _set_healthy_runtime()

    r = client.post("/diag/repair?token=s3cret-xyz")
    assert r.status_code == 200
    body = r.json()
    assert "actions" in body and len(body["actions"]) == 4
    for a in body["actions"]:
        assert set(a) == {"action", "done", "detail"}


def test_diag_repair_only_enqueues_safe_commands(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    _clear_commands()
    from app.db import get_repo
    repo = get_repo()
    try:
        write_runtime_state(repo.conn, {
            "state": "IDLE", "mode": "auto", "daemon_alive": True,
            "extra": {"device": {"needs_priming": True}},
        })
    finally:
        repo.close()

    r = client.post("/diag/repair?token=s3cret-xyz")
    assert r.status_code == 200

    repo = get_repo()
    try:
        types = {row["type"] for row in repo.conn.execute("SELECT type FROM commands")}
    finally:
        repo.close()
    # every enqueued command type must be one this endpoint is allowed to send AND a member of
    # the daemon's own VALID_COMMANDS allowlist (defense in depth against an unsafe command).
    assert types <= {"safe_default", "prime"}
    assert types <= VALID_COMMANDS


def test_diag_repair_is_idempotent(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    _clear_commands()
    _set_healthy_runtime()

    first = client.post("/diag/repair?token=s3cret-xyz").json()
    second = client.post("/diag/repair?token=s3cret-xyz").json()
    assert len(first["actions"]) == len(second["actions"]) == 4

    from app.db import get_repo
    repo = get_repo()
    try:
        n = repo.conn.execute("SELECT COUNT(*) c FROM commands").fetchone()["c"]
    finally:
        repo.close()
    # a healthy runtime_state shouldn't have enqueued anything at all, on either call
    assert n == 0


# ------------------------------------------------------------------ /diag/action/restart
def test_diag_action_restart_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.post("/diag/action/restart?target=daemon").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.post("/diag/action/restart?target=daemon").status_code == 404
    assert client.post("/diag/action/restart?token=nope&target=daemon").status_code == 404


def test_diag_action_restart_writes_restart_request_for_allowed_target(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    run = _run_dir()
    flag = os.path.join(run, "restart.request")
    if os.path.exists(flag):
        os.remove(flag)

    r = client.post("/diag/action/restart?token=s3cret-xyz&target=daemon")
    assert r.status_code == 200
    assert r.json() == {"requested": "daemon",
                        "verify_with": "/diag/all"}
    assert os.path.exists(flag)
    assert open(flag, encoding="utf-8").read().strip() == "daemon"
    os.remove(flag)


def test_diag_action_restart_all_targets_allowed(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    run = _run_dir()
    flag = os.path.join(run, "restart.request")
    for target in ("daemon", "api", "web", "all"):
        r = client.post(f"/diag/action/restart?token=s3cret-xyz&target={target}")
        assert r.status_code == 200
        assert open(flag, encoding="utf-8").read().strip() == target
    os.remove(flag)


def test_diag_action_restart_rejects_unknown_target(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    run = _run_dir()
    flag = os.path.join(run, "restart.request")
    if os.path.exists(flag):
        os.remove(flag)

    for bad in ("shell", "rm -rf /", "../../etc", "", "DAEMON"):
        r = client.post(f"/diag/action/restart?token=s3cret-xyz&target={bad}")
        assert r.status_code == 400
    assert not os.path.exists(flag)  # never wrote a flag for a rejected target


def test_diag_action_restart_logs_a_remote_action_event(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    client.post("/diag/action/restart?token=s3cret-xyz&target=web")
    r = client.get("/diag/events?token=s3cret-xyz&category=remote_action&limit=10")
    assert r.status_code == 200
    rows = r.json()
    assert any(row["code"] == "restart_request" and row["data"].get("target") == "web"
              for row in rows)
    os.remove(os.path.join(_run_dir(), "restart.request"))


# ------------------------------------------------------------------ /diag/action/reconnect
def test_diag_action_reconnect_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.post("/diag/action/reconnect").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.post("/diag/action/reconnect?token=nope").status_code == 404


def test_diag_action_reconnect_enqueues_safe_default_and_dedupes(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    _clear_commands()

    first = client.post("/diag/action/reconnect?token=s3cret-xyz")
    assert first.status_code == 200
    assert first.json()["reconnect_requested"] is True
    assert first.json()["command_id"] is not None

    second = client.post("/diag/action/reconnect?token=s3cret-xyz")
    assert second.json()["command_id"] is None  # already pending -- not duplicated

    from app.db import get_repo
    repo = get_repo()
    try:
        rows = repo.conn.execute(
            "SELECT type FROM commands WHERE status='pending'").fetchall()
    finally:
        repo.close()
    assert [r["type"] for r in rows] == ["safe_default"]


# ------------------------------------------------------------------ /diag/morning-report (read)
def test_diag_morning_report_requires_auth():
    from fastapi.testclient import TestClient
    from app.main import app
    r = TestClient(app).get("/diag/morning-report")
    assert r.status_code == 401


def test_diag_morning_report_view_shape(auth_client):
    r = auth_client.get("/diag/morning-report")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"health_verdict", "headline", "body"}
    assert isinstance(body["headline"], str) and body["headline"]
    assert isinstance(body["body"], str) and body["body"]


# ------------------------------------------------------------------ /diag/morning-report/send
def test_diag_morning_report_send_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.post("/diag/morning-report/send").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.post("/diag/morning-report/send?token=nope").status_code == 404


def _fake_report(verdict):
    return {"health_verdict": verdict, "headline": f"{verdict}: test", "body": f"System: {verdict}",
            "night": None, "generated_at": "2026-07-02T00:00:00+00:00"}


def test_diag_morning_report_send_throttles_to_once_per_day(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    _clear_kv("morning_report_last_sent_date", "morning_report_last_critical_push")
    from app import services
    monkeypatch.setattr(services, "build_morning_report", lambda repo: _fake_report("HEALTHY"))

    first = client.post("/diag/morning-report/send?token=s3cret-xyz").json()
    assert first["reason"] in ("vapid_not_configured", "no_subscriptions")  # push infra unset in tests
    assert first["trigger"] == "daily"

    second = client.post("/diag/morning-report/send?token=s3cret-xyz").json()
    assert second["sent"] is False
    assert second["reason"] == "throttled"
    _clear_kv("morning_report_last_sent_date", "morning_report_last_critical_push")


def test_diag_morning_report_send_critical_bypasses_daily_throttle_once_per_hour(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    _clear_kv("morning_report_last_sent_date", "morning_report_last_critical_push")
    from app import services
    monkeypatch.setattr(services, "build_morning_report", lambda repo: _fake_report("DOWN"))

    first = client.post("/diag/morning-report/send?token=s3cret-xyz").json()
    assert first["trigger"] == "critical_now"

    second = client.post("/diag/morning-report/send?token=s3cret-xyz").json()
    assert second["sent"] is False
    assert second["reason"] == "throttled"  # hourly cooldown still active
    _clear_kv("morning_report_last_sent_date", "morning_report_last_critical_push")


def test_diag_morning_report_send_force_bypasses_throttle(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    _clear_kv("morning_report_last_sent_date", "morning_report_last_critical_push")
    from app import services
    monkeypatch.setattr(services, "build_morning_report", lambda repo: _fake_report("HEALTHY"))

    client.post("/diag/morning-report/send?token=s3cret-xyz")
    forced = client.post("/diag/morning-report/send?token=s3cret-xyz&force=true").json()
    assert forced["trigger"] == "forced"
    _clear_kv("morning_report_last_sent_date", "morning_report_last_critical_push")


def test_build_morning_report_sane_structure():
    from app.db import get_repo
    from app import services
    repo = get_repo()
    try:
        report = services.build_morning_report(repo)
    finally:
        repo.close()
    assert report["health_verdict"] in ("HEALTHY", "DEGRADED", "DOWN", "UNKNOWN")
    assert isinstance(report["headline"], str) and report["headline"]
    assert isinstance(report["body"], str) and "System:" in report["body"]
    assert "night" in report and "generated_at" in report


def test_maybe_send_morning_report_json_serializable():
    """The dict returned by services.maybe_send_morning_report must round-trip through JSON
    cleanly (it's returned directly by the FastAPI endpoint)."""
    from app.db import get_repo
    from app import services
    repo = get_repo()
    try:
        _clear_kv("morning_report_last_sent_date", "morning_report_last_critical_push")
        result = services.maybe_send_morning_report(repo, force=True)
    finally:
        repo.close()
    json.dumps(result)  # must not raise
    assert "sent" in result and "report" in result
