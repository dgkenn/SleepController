"""The iPhone's sleep schedule drives the controller via Shortcuts automations."""
import json

import pytest

from app import automation, bridge


@pytest.mark.parametrize("raw,want", [
    ("7:00 AM", "07:00"), ("07:00", "07:00"), ("6:45", "06:45"), ("12:30 AM", "00:30"),
    ("12:15 PM", "12:15"), ("5:30 p.m.", "17:30"), ("2026-11-22T07:10:00-05:00", "07:10"),
    ("Nov 22, 2026 at 6:50 AM", "06:50"), ("22.11.2026, 06:40", "06:40"),
    ("", None), ("no alarm", None), (None, None), ("25:00", None),
])
def test_alarm_text_in_the_formats_shortcuts_produces(raw, want):
    assert automation.parse_alarm(raw) == want


@pytest.mark.parametrize("raw,want", [
    ("bedtime", "bedtime"), ("Bedtime Begins", "bedtime"), ("Wind Down Begins", "wind_down"), ("bedtime_begins", "bedtime"),
    ("wind down", "wind_down"), ("Waking Up", "wake"), ("wake", "wake"), ("nap", None),
])
def test_event_names(raw, want):
    assert automation.normalize_event(raw) == want


def _queued(conn):
    return [r[0] for r in conn.execute(
        "SELECT type FROM commands WHERE status='pending' ORDER BY id").fetchall()]


@pytest.fixture()
def conn():
    from app.db import get_repo
    c = get_repo().conn
    c.execute("DELETE FROM commands")
    c.execute("DELETE FROM automation_events")
    bridge.write_runtime_state(c, {"state": "idle"})
    return c


def test_wind_down_arms_the_wake_once(conn):
    out = automation.handle(conn, {"event": "wind_down", "alarm": "7:00 AM"})
    assert out["ok"] and out["alarm"] == "07:00"
    automation.handle(conn, {"event": "bedtime", "alarm": "7:00 AM"})
    assert _queued(conn) == ["set_wake", "induce_sleep"]     # the same alarm is not re-armed


def test_a_changed_alarm_is_re_armed(conn):
    automation.handle(conn, {"event": "wind_down", "alarm": "7:00 AM"})
    automation.handle(conn, {"event": "bedtime", "alarm": "6:30 AM"})
    assert _queued(conn) == ["set_wake", "set_wake", "induce_sleep"]


def test_bedtime_does_not_restart_a_running_night(conn):
    bridge.write_runtime_state(conn, {"state": "maintenance"})
    out = automation.handle(conn, {"event": "bedtime"})
    assert _queued(conn) == [] and out["actions"][0]["skipped"] == "induce_sleep"


def test_waking_up_ends_a_running_night_only(conn):
    automation.handle(conn, {"event": "wake"})
    assert _queued(conn) == []
    bridge.write_runtime_state(conn, {"state": "maintenance"})
    automation.handle(conn, {"event": "wake", "alarm": "7:00 AM"})
    assert _queued(conn) == ["woke_up"]                    # and no wake re-armed in the morning


def test_an_unknown_event_is_refused(conn):
    assert automation.handle(conn, {"event": "party"})["ok"] is False
    assert _queued(conn) == []


def test_the_endpoint_needs_no_login_by_default(monkeypatch, conn):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.delenv("SLEEPCTL_NO_LOGIN", raising=False)
    r = TestClient(app).post("/automation/sleep", json={"event": "bedtime", "alarm": "07:05"})
    assert r.status_code == 200 and r.json()["alarm"] == "07:05"


def test_the_snapshot_block_has_no_times(conn):
    automation.handle(conn, {"event": "bedtime", "alarm": "7:00 AM"})
    blob = json.dumps(automation.summary(conn))
    assert "07:00" not in blob and "bedtime" in blob
