"""Realistic Pod-2 thermal model: the pure helpers in ``thermal_sim`` + the
``SimulatedLiveClient`` opt-in wiring (``pod_scenario``).

Measured live: commanding -100 from -50 took ~9 min to reach only ~-63 (NOT instant, ~1-1.5
levels/min); bed-surface temp follows the plate with lag and is bounded by capacity/ambient
(warm-room band measured ~66-79F, nowhere near the full 55-110F theoretical range). These
tests pin the model against those numbers and check the legacy (idealized) simulator path is
completely unaffected unless a scenario is explicitly selected.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from sleepctl.adapters import thermal_sim
from sleepctl.adapters.thermal_sim import (
    AIR_BOUND_CAPACITY,
    AIR_BOUND_RAMP_PER_MIN,
    DEFAULT_CAPACITY,
    DEFAULT_RAMP_PER_MIN,
    level_setpoint_f,
    step_bed_temp,
    step_plate_level,
)
from sleepctl.loop.live import SimulatedLiveClient


# --------------------------------------------------------------------------- step_plate_level
def test_plate_level_ramps_not_instant():
    """One tick moves only ``ramp_per_min * dt_min`` toward the target, never jumps there."""
    actual = step_plate_level(-50.0, -100.0, 1.0, DEFAULT_RAMP_PER_MIN)
    assert actual == pytest.approx(-50.0 - DEFAULT_RAMP_PER_MIN, abs=1e-9)
    assert actual != -100.0


def test_plate_level_matches_measured_rate_9min():
    """Commanding -100 from -50 measured ~9 min to reach only ~-63 live."""
    actual = -50.0
    for _ in range(9):
        actual = step_plate_level(actual, -100.0, 1.0, DEFAULT_RAMP_PER_MIN)
    assert -66.0 <= actual <= -60.0  # ~-63 measured; give a few levels of slack


def test_plate_level_never_overshoots_and_settles_exactly():
    actual = -50.0
    for _ in range(500):
        actual = step_plate_level(actual, -100.0, 1.0, DEFAULT_RAMP_PER_MIN)
    assert actual == -100.0


def test_plate_level_zero_ramp_freezes_in_place():
    """ramp_per_min<=0 models a stuck/air-locked element: it cannot move at all."""
    actual = -12.0
    for _ in range(50):
        actual = step_plate_level(actual, 90.0, 1.0, 0.0)
    assert actual == -12.0


def test_plate_level_zero_dt_is_a_no_op():
    assert step_plate_level(5.0, 80.0, 0.0, DEFAULT_RAMP_PER_MIN) == 5.0


# --------------------------------------------------------------------------------- step_bed_temp
def test_bed_temp_has_first_order_lag_not_instant():
    """A single tick only closes PART of the gap to the setpoint -- never jumps there."""
    setpoint = level_setpoint_f(-100.0, 72.0, DEFAULT_CAPACITY)
    bed = step_bed_temp(72.0, -100.0, 72.0, 1.0, DEFAULT_CAPACITY)
    assert bed != pytest.approx(setpoint, abs=1e-6)
    assert 72.0 > bed > setpoint  # moved toward it, but partway


def test_bed_temp_approaches_capacity_bounded_setpoint_with_lag():
    bed = 72.0
    trail = []
    for _ in range(200):
        bed = step_bed_temp(bed, -100.0, 72.0, 1.0, DEFAULT_CAPACITY)
        trail.append(bed)
    # monotonically cooling toward the asymptote, never overshoots it
    assert all(a >= b - 1e-9 for a, b in zip(trail, trail[1:]))
    setpoint = level_setpoint_f(-100.0, 72.0, DEFAULT_CAPACITY)
    assert trail[-1] == pytest.approx(setpoint, abs=0.05)


def test_bed_temp_normal_band_matches_measured_warm_room_range():
    """Full-swing asymptotes at normal (non-fault) capacity should land near the measured
    ~66-79F warm-room band, nowhere near the full 55-110F theoretical range."""
    ambient = 72.0
    cool = heat = ambient
    for _ in range(600):
        cool = step_bed_temp(cool, -100.0, ambient, 1.0, DEFAULT_CAPACITY)
        heat = step_bed_temp(heat, 100.0, ambient, 1.0, DEFAULT_CAPACITY)
    assert 64.0 <= cool <= 70.0
    assert 78.0 <= heat <= 84.0
    assert (heat - cool) < 20.0  # nowhere near the full 55-110F (55F) theoretical span


def test_air_bound_narrows_band_and_slows_ramp_vs_normal():
    ambient = 72.0
    # ramp: air_bound must be strictly slower than normal.
    normal_actual = air_actual = -50.0
    for _ in range(9):
        normal_actual = step_plate_level(normal_actual, -100.0, 1.0, DEFAULT_RAMP_PER_MIN)
        air_actual = step_plate_level(air_actual, -100.0, 1.0, AIR_BOUND_RAMP_PER_MIN)
    assert abs(air_actual - (-50.0)) < abs(normal_actual - (-50.0))

    # band: air_bound's achievable swing must be narrower than normal's.
    normal_cool = air_cool = ambient
    normal_heat = air_heat = ambient
    for _ in range(600):
        normal_cool = step_bed_temp(normal_cool, -100.0, ambient, 1.0, DEFAULT_CAPACITY)
        air_cool = step_bed_temp(air_cool, -100.0, ambient, 1.0, AIR_BOUND_CAPACITY)
        normal_heat = step_bed_temp(normal_heat, 100.0, ambient, 1.0, DEFAULT_CAPACITY)
        air_heat = step_bed_temp(air_heat, 100.0, ambient, 1.0, AIR_BOUND_CAPACITY)
    normal_span = normal_heat - normal_cool
    air_span = air_heat - air_cool
    assert air_span < normal_span
    assert AIR_BOUND_CAPACITY < DEFAULT_CAPACITY


def test_level_setpoint_f_is_bounded_by_the_device_clamp():
    assert 55.0 <= level_setpoint_f(-100.0, 72.0, 1.0) <= 110.0
    assert 55.0 <= level_setpoint_f(200.0, 500.0, 1.0) <= 110.0  # extreme inputs still clamp


# ---------------------------------------------------------- SimulatedLiveClient wiring / API
def test_unknown_pod_scenario_rejected():
    with pytest.raises(ValueError):
        SimulatedLiveClient(pod_scenario="not_a_real_scenario")


def test_default_pod_scenario_is_legacy_idealized_and_unchanged():
    """No pod_scenario -> bed_temp_f/device fields come straight from the scripted night,
    the exact behavior every pre-existing test/consumer already depends on."""
    c = SimulatedLiveClient(scenario="normal", seed=7, start=datetime(2026, 6, 23, 23, 0))
    frame = c.read_frame()
    assert 69.0 <= frame.bed_temp_f <= 71.0  # ScriptedNight's fixed ~70F jitter, unbounded
    status = c.device_status()
    assert status["online"] is True and status["simulated"] is True
    assert status["pod_scenario"] is None
    assert status["priming"] is False


def test_pod_scenario_realistic_ramps_and_lags_through_read_frame():
    async def go():
        c = SimulatedLiveClient(scenario="normal", seed=7, start=datetime(2026, 6, 23, 23, 0),
                                 pod_scenario="realistic")
        c.read_frame()
        await c.set_heating_level(-90)
        levels = []
        for _ in range(5):
            f = c.read_frame()
            await c.set_heating_level(-90)
            levels.append(f.device_level)
        return levels

    levels = asyncio.run(go())
    # ramping, not instant: device_level should move gradually toward -90, not jump there.
    assert levels[0] != -90
    gaps = [abs(-90 - lv) for lv in levels]
    assert all(a >= b for a, b in zip(gaps, gaps[1:]))  # monotonic progress toward target


def test_determinism_same_seed_scenario_identical_trajectory():
    async def trajectory(pod_scenario):
        c = SimulatedLiveClient(scenario="normal", seed=11, start=datetime(2026, 6, 23, 23, 0),
                                 pod_scenario=pod_scenario, competing_period_ticks=7)
        out = []
        for i in range(40):
            f = c.read_frame()
            await c.set_heating_level(-40 if i % 2 == 0 else 20)
            out.append((f.device_level, f.target_level, f.bed_temp_f, f.data_age_seconds,
                        f.heart_rate))
        return out

    for scenario in ("realistic", "air_bound", "competing_controller", "frozen_telemetry",
                      "rate_limited", "stuck_prime"):
        a = asyncio.run(trajectory(scenario))
        b = asyncio.run(trajectory(scenario))
        assert a == b, f"{scenario} trajectory not deterministic"


def test_stuck_prime_never_advances():
    async def go():
        c = SimulatedLiveClient(scenario="normal", seed=7, start=datetime(2026, 6, 23, 23, 0),
                                 pod_scenario="stuck_prime")
        for _ in range(20):
            c.read_frame()
            await c.set_heating_level(-80)
            await c.prime_pod()
        return c

    c = asyncio.run(go())
    status = c.device_status()
    assert status["priming"] is True
    assert status["last_prime"] is None          # a prime that can never finish
    assert c.prime_count == 20                    # calls were made; they just never "land"
    assert status["device_level"] == 0            # the plate never moved at all


def test_air_bound_priming_never_completes_and_narrow_status_band():
    async def go():
        c = SimulatedLiveClient(scenario="normal", seed=7, start=datetime(2026, 6, 23, 23, 0),
                                 pod_scenario="air_bound", ambient_f=72.0)
        for _ in range(30):
            c.read_frame()
            await c.set_heating_level(-100)
        return c

    c = asyncio.run(go())
    status = c.device_status()
    assert status["priming"] is True  # air_bound: "a prime never completes"
    assert status["device_level"] == 0


def test_competing_controller_periodically_resets_target():
    async def go():
        c = SimulatedLiveClient(scenario="normal", seed=7, start=datetime(2026, 6, 23, 23, 0),
                                 pod_scenario="competing_controller", competing_period_ticks=5,
                                 competing_target_level=-68)
        targets = []
        for _ in range(16):
            f = c.read_frame()
            await c.set_heating_level(30)  # our controller wants +30 the whole time
            targets.append(f.target_level)
        return targets, c

    targets, c = asyncio.run(go())
    # every 5th tick (5, 10, 15) the external actor wins that tick's target register
    assert targets[5] == -68 and targets[10] == -68 and targets[15] == -68
    # our own command reasserts on the other ticks
    assert targets[6] == 30 and targets[11] == 30
    assert c.device_status()["external_schedule"]["override_count"] == 3


def test_frozen_telemetry_stops_changing_and_age_grows():
    async def go():
        c = SimulatedLiveClient(scenario="normal", seed=7, start=datetime(2026, 6, 23, 23, 0),
                                 pod_scenario="frozen_telemetry", freeze_after_ticks=4)
        out = []
        for i in range(20):
            f = c.read_frame()
            await c.set_heating_level(-90 if i % 3 else 40)  # commands keep changing
            out.append((f.bed_temp_f, f.device_level, f.data_age_seconds))
        return out

    out = asyncio.run(go())
    frozen_tail = out[6:]  # a couple ticks after the freeze point to be safely inside it
    first = frozen_tail[0]
    assert all(row[0] == first[0] and row[1] == first[1] for row in frozen_tail)
    ages = [row[2] for row in frozen_tail]
    assert all(b > a for a, b in zip(ages, ages[1:]))  # age keeps growing, never resets


def test_rate_limited_occasionally_stale():
    async def go():
        c = SimulatedLiveClient(scenario="normal", seed=7, start=datetime(2026, 6, 23, 23, 0),
                                 pod_scenario="rate_limited", rate_limited_every=4)
        ages = []
        for i in range(16):
            f = c.read_frame()
            await c.set_heating_level(-50)
            ages.append(f.data_age_seconds)
        return ages

    ages = asyncio.run(go())
    for i, age in enumerate(ages):
        if i > 0 and i % 4 == 0:
            assert age is None
        else:
            assert age is not None
