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
    assert "did not move at all" in h.reason
    assert "water level" in h.reason        # a motionless loop IS the hardware diagnosis


def test_slow_progress_is_ramping_not_stalled():
    """Movement in the commanded direction proves the loop works -- pump, water, cover -- so it
    must not be reported as a stall telling the user to power-cycle the Hub and check hoses.

    Observed 2026-08-05 23:18: the bed was cooling -49 -> -52 while commanded to -82 and the
    battery still failed with "not responding". ``thermal_min_progress_levels`` is a static
    5-level guess that only becomes rate-aware once the on-bed self-test has measured THIS bed.
    """
    m = _mon()
    base = datetime(2026, 6, 27, 2, 0)
    for i in range(9):
        m.record(_t(base, i), target_level=-82, device_level=-49 - i // 3)  # -49 -> -52, slow
    h = m.status(_t(base, 8))
    assert h.state == "ramping" and h.responding is True
    assert "slower than expected" in h.reason
    assert "self-test" in h.reason          # points at calibration, not at hardware


def test_moving_away_from_target_reports_observation_not_a_verdict():
    """A loop moving the WRONG way is a working loop that is not following us -- hoses and water
    level explain a bed that sits still, not one that reverses. But the CAUSE must stay a list of
    candidates: the obvious suspect (this account's un-disableable Eight Sleep schedule) turned
    out to be mirroring our target exactly all night, so naming it would have sent the user to
    change a setting that was not the problem."""
    m = _mon()
    base = datetime(2026, 6, 27, 2, 0)
    for i in range(9):
        m.record(_t(base, i), target_level=-72, device_level=-56 + i)  # drifting warm
    h = m.status(_t(base, 8))
    assert h.state == "stalled" and h.responding is False
    assert "WRONG WAY" in h.reason
    assert "not following us" in h.reason
    assert "hose" not in h.reason.lower()
    # candidates, not a verdict: blaming the schedule outright was wrong -- on the night that
    # motivated this, the schedule was mirroring our target exactly
    assert "powered on" in h.reason and "schedule" in h.reason


def test_a_fresh_colder_target_does_not_score_against_the_old_ones_data():
    """THE regression, reproduced live 2026-08-24 five times in one night. The bed was
    overshooting a moderate cool target, rebounding a few levels as it settled -- ordinary,
    working behaviour -- when a fresh, much colder settle/precool command replaced the old
    target. The window must not compare the NEW target against device movement recorded under
    the OLD one: doing so reads a normal settle-rebound as "moved the WRONG WAY" and tells the
    user to power-cycle the Hub over a bed that was never broken."""
    m = _mon()
    base = datetime(2026, 6, 27, 2, 0)
    m.record(_t(base, 0), target_level=-50, device_level=-55)   # overshot the old target
    m.record(_t(base, 6), target_level=-50, device_level=-50)   # rebounded/settled -- still fine
    m.record(_t(base, 7), target_level=-73, device_level=-50)   # fresh, much colder command
    h = m.status(_t(base, 7))
    assert h.state == "unknown" and h.responding is True
    assert "WRONG WAY" not in h.reason


def test_after_a_target_change_real_progress_is_still_recognized():
    """The fix must not blind the monitor going forward -- once enough samples accumulate
    under the NEW target, ordinary ramping is judged normally again."""
    m = _mon()
    base = datetime(2026, 6, 27, 2, 0)
    m.record(_t(base, 0), target_level=-50, device_level=-50)
    m.record(_t(base, 1), target_level=-73, device_level=-50)   # target changes here
    for i, lvl in enumerate([-52, -56, -61, -66, -71, -73, -73], start=2):
        m.record(_t(base, i), target_level=-73, device_level=lvl)
    h = m.status(_t(base, 8))
    assert h.state in ("ramping", "ok") and h.responding is True


def test_after_a_target_change_a_genuine_wrong_way_fault_is_still_caught():
    """The fix defers judgement until there's clean history under the new target -- it must not
    permanently hide a REAL fault that persists after the change, only the stale-reference false
    positive at the moment of the change itself."""
    m = _mon()
    base = datetime(2026, 6, 27, 2, 0)
    m.record(_t(base, 0), target_level=-50, device_level=-50)
    m.record(_t(base, 1), target_level=-73, device_level=-50)   # target changes here
    for i, lvl in enumerate([-48, -46, -44, -42, -40, -38, -36], start=2):
        m.record(_t(base, i), target_level=-73, device_level=lvl)  # genuinely drifting warmer
    h = m.status(_t(base, 8))
    assert h.state == "stalled" and h.responding is False
    assert "WRONG WAY" in h.reason


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


def test_self_test_captures_resting_baseline_and_warmback():
    import asyncio
    from sleepctl.loop.live import SimulatedLiveClient
    from sleepctl.loop.self_test import run_self_test

    async def go():
        client = SimulatedLiveClient(scenario="normal")
        await client.connect()
        return await run_self_test(client, mode="full")
    rep = asyncio.new_event_loop().run_until_complete(go())
    # resting baseline captured while "lying still" during sensing
    assert rep.resting_baseline and rep.resting_baseline["hr"] is not None
    assert any(c.name == "resting_baseline" for c in rep.checks)
