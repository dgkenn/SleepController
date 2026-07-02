"""Tests for the real-time nighttime failure push (Goal: an offline bed, empty water
reservoir, wedged command queue, or a stalled control loop should page the phone before the
user finds out by being uncomfortable at 3am).

Covers: ``services._detect_failure_conditions`` / ``services.check_and_alert_failures`` (pure
detection + live/night gating + per-condition hourly rate limit, mirroring the morning report's
throttle pattern), and the two endpoints appended at the end of ``main.py``
(``GET /alerts/active``, ``POST /diag/action/test-alert``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import services
from app.bridge import write_runtime_state
from app.db import get_repo


def _set_runtime_extra(**extra_overrides):
    repo = get_repo()
    try:
        base_extra = {
            "device": {"online": True, "has_water": True, "priming": False},
            "live": True,
            "dry_run": False,
            "bed_presence": True,
        }
        base_extra.update(extra_overrides)
        write_runtime_state(repo.conn, {
            "state": "IDLE", "objective": "OPTIMIZE", "mode": "auto",
            "target_temp_f": 68.0, "bed_temp_f": 70.0, "room_temp_f": 68.0,
            "stage": "unknown", "confidence": 0.8, "target_level": -50,
            "daemon_alive": True, "extra": base_extra,
        })
    finally:
        repo.close()


def _clear_commands():
    repo = get_repo()
    try:
        repo.conn.execute("DELETE FROM commands")
        repo.conn.commit()
    finally:
        repo.close()


def _insert_stuck_command(age_seconds: float):
    repo = get_repo()
    try:
        ts = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
        repo.conn.execute(
            "INSERT INTO commands (ts, type, payload, status) VALUES (?,?,?,'pending')",
            (ts, "set_temp", "{}"),
        )
        repo.conn.commit()
    finally:
        repo.close()


def _clear_night_failure_kv():
    repo = get_repo()
    try:
        repo.conn.execute(
            "DELETE FROM settings_kv WHERE key LIKE 'night_failure_last_sent__%'")
        repo.conn.commit()
    finally:
        repo.close()


def _reset():
    _clear_commands()
    _clear_night_failure_kv()
    _set_runtime_extra()  # healthy baseline


# ------------------------------------------------------------ pure detection
def test_detect_reservoir_empty():
    repo = get_repo()
    try:
        now = datetime.now(timezone.utc)
        rt = {"updated": now.isoformat(),
              "extra": {"device": {"online": True, "has_water": False}}}
        conditions = services._detect_failure_conditions(rt, repo, now)
    finally:
        repo.close()
    codes = {c["code"] for c in conditions}
    assert "reservoir_empty" in codes
    cond = next(c for c in conditions if c["code"] == "reservoir_empty")
    assert cond["severity"] == "critical"
    assert "title" in cond and "body" in cond


def test_detect_device_offline():
    repo = get_repo()
    try:
        now = datetime.now(timezone.utc)
        rt = {"updated": now.isoformat(),
              "extra": {"device": {"online": False, "has_water": True}}}
        conditions = services._detect_failure_conditions(rt, repo, now)
    finally:
        repo.close()
    assert "device_offline" in {c["code"] for c in conditions}


def test_detect_stuck_commands():
    _clear_commands()
    _insert_stuck_command(700)  # > 10 min threshold
    repo = get_repo()
    try:
        now = datetime.now(timezone.utc)
        rt = {"updated": now.isoformat(), "extra": {"device": {"online": True, "has_water": True}}}
        conditions = services._detect_failure_conditions(rt, repo, now)
    finally:
        repo.close()
    _clear_commands()
    assert "stuck_commands" in {c["code"] for c in conditions}


def test_recent_pending_command_is_not_stuck():
    _clear_commands()
    _insert_stuck_command(30)  # well under the threshold
    repo = get_repo()
    try:
        now = datetime.now(timezone.utc)
        rt = {"updated": now.isoformat(), "extra": {"device": {"online": True, "has_water": True}}}
        conditions = services._detect_failure_conditions(rt, repo, now)
    finally:
        repo.close()
    _clear_commands()
    assert "stuck_commands" not in {c["code"] for c in conditions}


def test_detect_data_stale_at_night_only_when_bed_presence():
    repo = get_repo()
    try:
        now = datetime.now(timezone.utc)
        old = (now - timedelta(minutes=10)).isoformat()

        rt_presence = {"updated": old, "extra": {"device": {"online": True, "has_water": True},
                                                 "bed_presence": True}}
        conditions_presence = services._detect_failure_conditions(rt_presence, repo, now)

        rt_no_presence = {"updated": old, "extra": {"device": {"online": True, "has_water": True},
                                                     "bed_presence": False}}
        conditions_no_presence = services._detect_failure_conditions(rt_no_presence, repo, now)
    finally:
        repo.close()
    assert "data_stale_at_night" in {c["code"] for c in conditions_presence}
    assert "data_stale_at_night" not in {c["code"] for c in conditions_no_presence}


def test_healthy_state_has_no_conditions():
    repo = get_repo()
    try:
        now = datetime.now(timezone.utc)
        rt = {"updated": now.isoformat(),
              "extra": {"device": {"online": True, "has_water": True}, "bed_presence": True}}
        conditions = services._detect_failure_conditions(rt, repo, now)
    finally:
        repo.close()
    assert conditions == []


# ------------------------------------------------------------ gating: live + night context
def test_check_and_alert_returns_nothing_when_nobody_in_bed_and_not_night(monkeypatch):
    monkeypatch.setattr(services, "_in_night_window", lambda now: False)
    _reset()
    _set_runtime_extra(device={"online": False, "has_water": True},
                       live=True, dry_run=False, bed_presence=False)
    repo = get_repo()
    try:
        result = services.check_and_alert_failures(repo)
    finally:
        repo.close()
    _reset()
    assert result == []


def test_check_and_alert_returns_nothing_in_dry_run(monkeypatch):
    monkeypatch.setattr(services, "_in_night_window", lambda now: True)
    _reset()
    _set_runtime_extra(device={"online": False, "has_water": True},
                       live=True, dry_run=True, bed_presence=True)
    repo = get_repo()
    try:
        result = services.check_and_alert_failures(repo)
    finally:
        repo.close()
    _reset()
    assert result == []


def test_check_and_alert_returns_nothing_when_not_live(monkeypatch):
    monkeypatch.setattr(services, "_in_night_window", lambda now: True)
    _reset()
    _set_runtime_extra(device={"online": False, "has_water": True},
                       live=False, dry_run=False, bed_presence=True)
    repo = get_repo()
    try:
        result = services.check_and_alert_failures(repo)
    finally:
        repo.close()
    _reset()
    assert result == []


def test_check_and_alert_returns_conditions_when_live_and_in_bed(monkeypatch):
    monkeypatch.setattr(services, "_in_night_window", lambda now: False)
    _reset()
    _set_runtime_extra(device={"online": True, "has_water": False},
                       live=True, dry_run=False, bed_presence=True)
    repo = get_repo()
    try:
        result = services.check_and_alert_failures(repo)
    finally:
        repo.close()
    _reset()
    codes = {c["code"] for c in result}
    assert "reservoir_empty" in codes


def test_check_and_alert_logs_but_does_not_push_outside_gate(monkeypatch):
    calls = []
    monkeypatch.setattr(services.push_sender, "deliver_custom",
                        lambda **kw: calls.append(kw) or services.push_sender.PushResult(ok=False))
    monkeypatch.setattr(services, "_in_night_window", lambda now: False)
    _reset()
    _set_runtime_extra(device={"online": True, "has_water": False},
                       live=True, dry_run=False, bed_presence=False)
    repo = get_repo()
    try:
        result = services.check_and_alert_failures(repo)
        events = repo.recent_events(limit=20, category="alert")
    finally:
        repo.close()
    _reset()
    assert result == []
    assert not calls  # never pushed
    assert any(e["code"] == "reservoir_empty" for e in events)  # but still logged


# ------------------------------------------------------------ hourly rate limit
def test_rate_limit_suppresses_second_push_and_rearms_after_clear(monkeypatch):
    calls = []
    monkeypatch.setattr(services.push_sender, "deliver_custom",
                        lambda **kw: calls.append(kw) or services.push_sender.PushResult(ok=False))
    monkeypatch.setattr(services, "_in_night_window", lambda now: True)
    _reset()
    _set_runtime_extra(device={"online": True, "has_water": False},
                       live=True, dry_run=False, bed_presence=True)
    repo = get_repo()
    try:
        first = services.check_and_alert_failures(repo)
        second = services.check_and_alert_failures(repo)
    finally:
        repo.close()
    assert "reservoir_empty" in {c["code"] for c in first}
    assert "reservoir_empty" in {c["code"] for c in second}  # still an active condition
    assert len(calls) == 1  # but only pushed once (hourly throttle)

    # condition resolves -> throttle auto-clears
    _set_runtime_extra(device={"online": True, "has_water": True},
                       live=True, dry_run=False, bed_presence=True)
    repo = get_repo()
    try:
        cleared = services.check_and_alert_failures(repo)
    finally:
        repo.close()
    assert "reservoir_empty" not in {c["code"] for c in cleared}

    # condition recurs -> re-alerts immediately instead of waiting out the rest of the hour
    _set_runtime_extra(device={"online": True, "has_water": False},
                       live=True, dry_run=False, bed_presence=True)
    repo = get_repo()
    try:
        third = services.check_and_alert_failures(repo)
    finally:
        repo.close()
    _reset()
    assert "reservoir_empty" in {c["code"] for c in third}
    assert len(calls) == 2  # re-armed: second push went out


# ------------------------------------------------------------ endpoints
def test_alerts_active_requires_auth():
    from fastapi.testclient import TestClient
    from app.main import app
    r = TestClient(app).get("/alerts/active")
    assert r.status_code == 401


def test_alerts_active_returns_active_list(auth_client):
    _reset()
    r = auth_client.get("/alerts/active")
    assert r.status_code == 200
    body = r.json()
    assert "active" in body and isinstance(body["active"], list)
    _reset()


def test_diag_action_test_alert_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.post("/diag/action/test-alert").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.post("/diag/action/test-alert?token=nope").status_code == 404


def test_diag_action_test_alert_returns_verify_with_shape(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    r = client.post("/diag/action/test-alert?token=s3cret-xyz")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"ok", "sent", "verify_with"}
    assert body["verify_with"] == "/alerts/active"
