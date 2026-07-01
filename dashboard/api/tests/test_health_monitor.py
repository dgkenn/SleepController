"""Unit tests for the pure health evaluator (dashboard/api/app/health_monitor.py).

No DB, no FastAPI, no network — just dicts in, issues out. One test per issue type,
plus a couple of "stays healthy" / "clears" sanity checks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import health_monitor as hm


def _rt(**overrides) -> dict:
    """A healthy baseline runtime_state, overridden per test."""
    base = {
        "daemon_alive": True,
        "stale": False,
        "updated": datetime.now(timezone.utc).isoformat(),
        "extra": {
            "thermal_health": {"state": "ok", "responding": True, "reason": "at setpoint"},
            "device": {"online": True, "has_water": True, "priming": False},
            "telemetry_stale": False,
            "data_age_s": 5.0,
            "device_error": None,
        },
    }
    base.update({k: v for k, v in overrides.items() if k != "extra"})
    if "extra" in overrides:
        base["extra"] = {**base["extra"], **overrides["extra"]}
    return base


def _codes(issues):
    return {i["code"] for i in issues}


def test_healthy_state_has_no_issues():
    assert hm.evaluate_health(_rt()) == []


def test_daemon_down_when_stale():
    rt = _rt(stale=True, daemon_alive=False)
    issues = hm.evaluate_health(rt)
    assert "daemon_down" in _codes(issues)
    assert all(i["severity"] == "critical" for i in issues if i["code"] == "daemon_down")


def test_daemon_down_when_never_reported():
    rt = _rt(updated=None, stale=True)
    issues = hm.evaluate_health(rt)
    assert "daemon_down" in _codes(issues)


def test_daemon_down_reports_age_in_message():
    old = (datetime.now(timezone.utc) - timedelta(minutes=42)).isoformat()
    rt = _rt(stale=True, daemon_alive=False, updated=old)
    issues = hm.evaluate_health(rt)
    msg = next(i["message"] for i in issues if i["code"] == "daemon_down")
    assert "min" in msg


def test_thermal_stalled():
    rt = _rt(extra={"thermal_health": {"state": "stalled", "responding": False,
                                        "reason": "no response to level change"}})
    issues = hm.evaluate_health(rt)
    assert "thermal_stalled" in _codes(issues)
    issue = next(i for i in issues if i["code"] == "thermal_stalled")
    assert issue["severity"] == "critical"
    assert "no response to level change" in issue["message"]


def test_thermal_ramping_is_not_an_issue():
    rt = _rt(extra={"thermal_health": {"state": "ramping", "responding": True, "reason": "ok"}})
    assert "thermal_stalled" not in _codes(hm.evaluate_health(rt))


def test_no_water():
    rt = _rt(extra={"device": {"online": True, "has_water": False, "priming": False}})
    issues = hm.evaluate_health(rt)
    assert "no_water" in _codes(issues)
    issue = next(i for i in issues if i["code"] == "no_water")
    assert issue["severity"] == "critical"


def test_device_offline():
    rt = _rt(extra={"device": {"online": False, "has_water": True}})
    issues = hm.evaluate_health(rt)
    assert "device_offline" in _codes(issues)


def test_telemetry_stale():
    rt = _rt(extra={"telemetry_stale": True, "data_age_s": 900})
    issues = hm.evaluate_health(rt)
    assert "telemetry_stale" in _codes(issues)
    issue = next(i for i in issues if i["code"] == "telemetry_stale")
    assert issue["severity"] == "warning"
    assert "900" in issue["message"]


def test_device_error_surfaced():
    rt = _rt(extra={"device_error": "401 Unauthorized"})
    issues = hm.evaluate_health(rt)
    assert "device_error" in _codes(issues)
    issue = next(i for i in issues if i["code"] == "device_error")
    assert issue["severity"] == "warning"
    assert "401" in issue["message"]


def test_repeated_cloud_errors_escalates_to_critical():
    rt = _rt()
    issues = hm.evaluate_health(rt, recent_errors=["timeout", "timeout", "timeout"])
    assert "repeated_cloud_errors" in _codes(issues)
    issue = next(i for i in issues if i["code"] == "repeated_cloud_errors")
    assert issue["severity"] == "critical"


def test_below_threshold_recent_errors_does_not_escalate():
    rt = _rt()
    issues = hm.evaluate_health(rt, recent_errors=["timeout"])
    assert "repeated_cloud_errors" not in _codes(issues)


def test_multiple_issues_can_coexist():
    rt = _rt(extra={
        "thermal_health": {"state": "stalled", "responding": False, "reason": "x"},
        "device": {"online": True, "has_water": False},
    })
    issues = hm.evaluate_health(rt)
    assert {"thermal_stalled", "no_water"} <= _codes(issues)


def test_malformed_state_never_raises():
    assert hm.evaluate_health(None) == []
    assert hm.evaluate_health({}) != []  # {} has no "updated" -> daemon_down, but shouldn't crash
    assert hm.evaluate_health({"extra": "not-a-dict"}) is not None


def test_worst_severity():
    assert hm.worst_severity([]) is None
    issues = [{"code": "a", "severity": "info"}, {"code": "b", "severity": "critical"},
              {"code": "c", "severity": "warning"}]
    assert hm.worst_severity(issues) == "critical"


def test_is_critical():
    assert hm.is_critical({"severity": "critical"}) is True
    assert hm.is_critical({"severity": "warning"}) is False
