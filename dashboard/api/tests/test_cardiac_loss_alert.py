"""Losing the wearable mid-night must be LOUD.

On this deployment the Polar Verity is the only source of sleep stage, heart rate and movement
-- the Pod's own biometrics are subscription-gated -- so a dropped band leaves the controller
steering blind. On 2026-08-06 it disconnected at 00:01 during a live MAINTENANCE session and
nothing surfaced it for six hours: the diagnostics row said "info", no alert was ever raised, and
the only trace was the forwarder writing "no Polar/HR sensor found this scan" ~2,200 times into a
log file nobody was reading.
"""

from __future__ import annotations

from app import health_monitor as hm


def _state(**extra):
    return {"updated": "2026-08-06T02:00:00", "daemon_alive": True, "stale": False,
            "state": extra.pop("state", "maintenance"), "extra": extra}


def _codes(issues):
    return {i["code"] for i in issues}


def test_cardiac_loss_mid_session_is_critical():
    issues = hm.evaluate_health(_state(cardiac_age_s=7200.0))
    assert "cardiac_sensor_lost" in _codes(issues)
    got = next(i for i in issues if i["code"] == "cardiac_sensor_lost")
    assert got["severity"] == "critical"
    assert "120 min" in got["message"]


def test_cardiac_loss_while_idle_is_not_alerted():
    """Out of bed with the band on a charger is the normal daytime state, not an incident."""
    issues = hm.evaluate_health(_state(state="idle", cardiac_age_s=7200.0))
    assert "cardiac_sensor_lost" not in _codes(issues)


def test_brief_reconnect_gap_does_not_page():
    """The forwarder rescans every 10s; a normal BLE blip must not wake anyone at 3am."""
    issues = hm.evaluate_health(_state(cardiac_age_s=120.0))
    assert "cardiac_sensor_lost" not in _codes(issues)


def test_missing_cardiac_age_is_not_an_alert():
    """Absence of the field (older daemon, or never any wearable) is not evidence of loss."""
    assert "cardiac_sensor_lost" not in _codes(hm.evaluate_health(_state()))
    assert "cardiac_sensor_lost" not in _codes(hm.evaluate_health(_state(cardiac_age_s=None)))
