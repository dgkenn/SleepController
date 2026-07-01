"""Thermal-response health check: trust the Hub's water-side device level, not bed temp."""

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.thermal_health import ThermalResponseMonitor


def _mon():
    return ThermalResponseMonitor(AppConfig.default())


def _t(base, minutes):
    return base + timedelta(minutes=minutes)


def test_unknown_without_samples():
    h = _mon().status()
    assert h.state == "unknown" and h.responding is True


def test_at_setpoint_is_ok():
    m = _mon()
    now = datetime(2026, 6, 27, 2, 0)
    m.record(now, target_level=-100, device_level=-98)  # within margin of target
    h = m.status(now)
    assert h.state == "ok" and h.responding is True and h.gap == -2


def test_cooling_ramp_is_responsive():
    # Mirrors the live trace: commanded -100, device level falling steadily toward it.
    m = _mon()
    base = datetime(2026, 6, 27, 2, 0)
    for i, lvl in enumerate([93, 86, 80, 74, 69, 63, 59, 55, 53]):
        m.record(_t(base, i), target_level=-100, device_level=lvl)
    h = m.status(_t(base, 8))
    assert h.state == "ramping" and h.responding is True
    assert "cooling" in h.reason


def test_heating_ramp_is_responsive():
    # Climbing toward +100 but not there yet -> "ramping" (responding).
    m = _mon()
    base = datetime(2026, 6, 27, 2, 0)
    for i, lvl in enumerate([10, 25, 40, 52, 63, 72, 78, 82, 85]):
        m.record(_t(base, i), target_level=100, device_level=lvl)
    h = m.status(_t(base, 8))
    assert h.state == "ramping" and h.responding is True and "warming" in h.reason


def test_reaching_target_under_command_is_ok():
    # Climbs all the way to the commanded level -> "ok" (at setpoint), still responding.
    m = _mon()
    base = datetime(2026, 6, 27, 2, 0)
    for i, lvl in enumerate([10, 30, 55, 75, 90, 96, 99, 100, 100]):
        m.record(_t(base, i), target_level=100, device_level=lvl)
    h = m.status(_t(base, 8))
    assert h.state == "ok" and h.responding is True


def test_stalled_when_commanded_but_flat():
    # Commanded to cool hard, but the device level never moves -> fault (low water/cover/hw).
    m = _mon()
    base = datetime(2026, 6, 27, 2, 0)
    for i in range(9):
        m.record(_t(base, i), target_level=-100, device_level=9)  # pinned, no response
    h = m.status(_t(base, 8))
    assert h.state == "stalled" and h.responding is False
    assert "not responding" in h.reason


def test_unknown_until_enough_window_history():
    m = _mon()
    base = datetime(2026, 6, 27, 2, 0)
    m.record(base, target_level=-100, device_level=95)
    m.record(_t(base, 1), target_level=-100, device_level=94)  # only 1 min of history
    h = m.status(_t(base, 1))
    assert h.state == "unknown" and h.responding is True


def test_none_levels_are_ignored():
    m = _mon()
    now = datetime(2026, 6, 27, 2, 0)
    m.record(now, target_level=None, device_level=None)
    m.record(now, target_level=-100, device_level=None)
    assert m.status(now).state == "unknown"  # nothing recorded


def test_health_to_dict_round_trips():
    m = _mon()
    now = datetime(2026, 6, 27, 2, 0)
    m.record(now, target_level=0, device_level=0)
    d = m.status(now).to_dict()
    assert set(d) == {"state", "responding", "reason", "device_level", "target_level", "gap"}
    assert d["state"] == "ok"


def test_measured_rate_sharpens_stall_reason():
    # With a measured cool rate, a flat device level while commanded to cool is STALLED and the
    # reason quotes the expected progress (judged against the bed's real speed).
    m = _mon()
    m.set_measured_rates(cool_levels_per_min=-30, heat_levels_per_min=20)
    base = datetime(2026, 6, 27, 2, 0)
    for i in range(10):
        m.record(_t(base, i), target_level=-80, device_level=10)  # commanded cool, not moving
    h = m.status(_t(base, 9))
    assert h.state == "stalled" and h.responding is False
    assert "expected" in h.reason


def test_measured_rate_does_not_break_a_healthy_ramp():
    m = _mon()
    m.set_measured_rates(cool_levels_per_min=-30, heat_levels_per_min=20)
    base = datetime(2026, 6, 27, 2, 0)
    # device level marching down toward the target at ~the measured rate -> ramping, healthy
    for i in range(10):
        m.record(_t(base, i), target_level=-80, device_level=10 - i * 8)
    h = m.status(_t(base, 9))
    assert h.state == "ramping" and h.responding is True
