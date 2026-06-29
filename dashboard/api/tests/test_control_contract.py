"""Manipulation -> backend-action contract.

Proves that when a dashboard control is manipulated, the RIGHT thing happens on the backend, end
to end:
  1. the API endpoint enqueues the correct command type + payload (or persists the right setting);
  2. the daemon, applying that command, performs the correct device/state action.

This is the guarantee that the UI and the backend can't silently drift apart.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "dashboard", "daemon"))


def _latest_command(client):
    """The most recently enqueued command (type, payload) from the bridge queue."""
    from app.db import get_repo
    repo = get_repo()
    try:
        row = repo.conn.execute(
            "SELECT type, payload FROM commands ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        repo.close()
    return (row["type"], json.loads(row["payload"] or "{}")) if row else (None, {})


# ---- 1. each control endpoint enqueues the CORRECT command + payload --------------------------
def test_control_buttons_enqueue_the_right_command(auth_client):
    cases = {
        "start": ("start", {}), "pause": ("pause", {}), "resume": ("resume", {}),
        "stop": ("stop", {}), "safe-default": ("safe_default", {}),
        "power-on": ("power_on", {}), "power-off": ("power_off", {}),
        "away-on": ("away_on", {}), "away-off": ("away_off", {}), "prime": ("prime", {}),
    }
    for action, (ctype, payload) in cases.items():
        r = auth_client.post(f"/control/{action}")
        assert r.status_code == 200 and r.json()["queued"] == ctype
        assert _latest_command(auth_client) == (ctype, payload)


def test_temp_mode_wake_nap_enqueue_right_payload(auth_client):
    auth_client.post("/tonight/temp", json={"target_f": 66.5})
    assert _latest_command(auth_client) == ("set_temp", {"target_f": 66.5})

    auth_client.post("/tonight/temp/nudge", json={"delta_f": -1.5})
    assert _latest_command(auth_client) == ("nudge_temp", {"delta_f": -1.5})

    auth_client.post("/tonight/mode", json={"mode": "manual"})
    assert _latest_command(auth_client) == ("set_mode", {"mode": "manual"})

    auth_client.post("/tonight/wake", json={"wake_time": "05:30", "window_min": 20,
                                            "vibration_power": 30, "night_type": "work"})
    t, p = _latest_command(auth_client)
    assert t == "set_wake" and p["wake_time"] == "05:30" and p["window_min"] == 20
    assert p["vibration_power"] == 30 and p["night_type"] == "work"

    auth_client.delete("/tonight/wake")
    assert _latest_command(auth_client)[0] == "clear_wake"

    auth_client.post("/tonight/induce")
    assert _latest_command(auth_client)[0] == "induce_sleep"

    auth_client.post("/tonight/nap", json={"duration_min": 20})
    t, p = _latest_command(auth_client)
    assert t == "start_nap" and p["duration_min"] == 20

    auth_client.post("/tonight/session/end")
    assert _latest_command(auth_client)[0] == "end_session"


# ---- 2. the daemon applies each command to the CORRECT device/state action --------------------
@pytest.fixture()
def daemon():
    from run_daemon import DashboardDaemon
    d = DashboardDaemon(simulate=True)
    yield d
    d.repo.close()


def _apply(daemon, ctype, payload=None):
    from app import bridge
    bridge.enqueue_command(daemon.repo.conn, ctype, payload or {})
    daemon._apply_commands()


def test_daemon_maps_commands_to_device_state(daemon):
    _apply(daemon, "stop")
    assert daemon.paused is True and daemon.power_on is False     # E-stop hard-offs

    _apply(daemon, "power_on")
    assert daemon.power_on is True and daemon.paused is False and daemon.away is False

    _apply(daemon, "away_on")
    assert daemon.away is True and daemon.power_on is False

    _apply(daemon, "away_off")
    assert daemon.away is False and daemon.power_on is True

    _apply(daemon, "pause")
    assert daemon.paused is True
    _apply(daemon, "resume")
    assert daemon.paused is False

    _apply(daemon, "set_temp", {"target_f": 64.0})
    assert daemon.manual_target_f == 64.0 and daemon.mode == "manual" and daemon.power_on is True

    _apply(daemon, "nudge_temp", {"delta_f": 2.0})
    assert daemon.manual_target_f == 66.0                         # relative to the manual target

    _apply(daemon, "set_mode", {"mode": "auto"})
    assert daemon.mode == "auto"

    _apply(daemon, "set_wake", {"wake_time": "06:15", "night_type": "work"})
    assert daemon.wake and daemon.wake["wake_time"] == "06:15"
    assert daemon.context.required_wake_time is not None

    _apply(daemon, "clear_wake")
    assert daemon.wake is None and daemon.context.required_wake_time is None

    _apply(daemon, "safe_default")
    assert daemon.power_on is True and daemon.paused is False and daemon.mode == "auto"


def test_daemon_temp_commands_are_clamped_to_safe_range(daemon):
    _apply(daemon, "set_temp", {"target_f": 999})
    assert 55.0 <= daemon.manual_target_f <= 110.0               # never commands an unsafe temp
    _apply(daemon, "set_temp", {"target_f": -999})
    assert 55.0 <= daemon.manual_target_f <= 110.0


def test_daemon_survives_a_malformed_wake_time(daemon):
    # A bad wake_time must not crash the command loop — the manipulation should degrade gracefully,
    # not take the daemon down. (The control loop keeps running afterward.)
    _apply(daemon, "set_wake", {"wake_time": "25:99"})
    # subsequent commands still apply -> the loop survived
    _apply(daemon, "power_off")
    assert daemon.power_on is False


# ---- 3. settings manipulations persist the right backend state --------------------------------
def test_settings_manipulations_persist(auth_client):
    # Hue config
    auth_client.put("/wake/light/config", json={"enabled": True, "target_ids": ["1"], "kind": "lights"})
    assert auth_client.get("/wake/light/config").json()["target_ids"] == ["1"]
    # Gym config (restore afterwards so the shared session DB isn't left mutated for other tests)
    auth_client.put("/gym/config", json={"enabled": True, "lean": "push"})
    assert auth_client.get("/gym/config").json()["config"]["lean"] == "push"
    auth_client.put("/gym/config", json={"enabled": False})
    # Shift config
    auth_client.put("/shift/config", json={"enabled": True, "next_shift": "2026-07-01T19:00:00",
                                           "kind": "night"})
    assert auth_client.get("/shift/config").json()["enabled"] is True
    auth_client.put("/shift/config", json={"enabled": False, "next_shift": None})
    # Hue config restore
    auth_client.put("/wake/light/config", json={"enabled": False})
    # Settings (benchmarks/profile)
    cur = auth_client.get("/settings").json()
    assert isinstance(cur, dict)
