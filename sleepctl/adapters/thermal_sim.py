"""Pure, deterministic thermal-response model matched to REAL Pod 2 measurements.

The idealized ``ScriptedNight``/``SimulatorSource`` in ``sleepctl.adapters.simulator``
treats the bed as an instant, unbounded actuator (bed_temp_f jitters around 70F regardless
of the commanded level). That's fine for exercising the sleep-stage state machine, but it
can't validate the controller's behavior against the actual device dynamics we measured live:

  * Commands set a ``target`` level (-100..+100); the ACTUAL plate level only ramps toward
    it slowly, ~1-1.5 levels/min (NOT instant). Measured: commanding -100 from -50 took ~9
    min to reach only ~-63 (13 levels in 9 min => ~1.44 levels/min).
  * Bed-surface temp follows the plate level with first-order LAG and is bounded by
    capacity/ambient -- in a warm room the achievable band was narrow (measured ~66-79F),
    nowhere near the full 55-110F theoretical device range. Side OFF -> bed ~= ambient;
    cooling pulls it down, heating pushes it up, both with lag.

This module holds the two small, pure helpers that model that -- no wall-clock, no
randomness, no device I/O, so callers get an identical trajectory for the same inputs every
time. ``sleepctl.loop.live.SimulatedLiveClient`` is the only caller today (opt-in via its
``pod_scenario`` argument); the default (legacy) simulator path never touches this module.
"""

from __future__ import annotations

from sleepctl.controller.calibration import clamp_fahrenheit, level_to_fahrenheit

# -- measured/derived defaults --------------------------------------------------------------
# Plate ramp rate, levels/min. Measured live: commanding -100 from -50 reached ~-63 in ~9 min
# (~1.44 levels/min); the brief says "~1-1.5 levels/min" -- 1.4 sits in that band.
DEFAULT_RAMP_PER_MIN = 1.4

# air_bound (reduced heat-transfer capacity): the plate itself ramps slower too.
AIR_BOUND_RAMP_PER_MIN = 0.5

# Fraction of the idealized level<->F swing (calibration.py's 55-110F table, level 0 ~= 81F)
# that is actually achievable given the room/capacity. 1.0 = unconstrained/idealized (today's
# default simulator). ~0.24 reproduces the measured warm-room 66-79F band against ambient
# ~72F and the full 55-110F theoretical span ((79-72)/(110-72) ~= 0.18, (72-66)/(72-55) ~=
# 0.35 -- 0.24 sits between the measured heat-side and cool-side ratios).
DEFAULT_CAPACITY = 0.24

# air_bound: heat-transfer-limited further still -- a narrower band on top of the slower ramp.
AIR_BOUND_CAPACITY = 0.10

# Bed thermal-mass lag time constant (minutes): first-order approach to the level-implied
# setpoint. ~15 min => ~63% of the way there after 15 min, ~95% after ~45 min -- consistent
# with the multi-tens-of-minutes settle observed live (never instant).
DEFAULT_TAU_MIN = 15.0


def step_plate_level(
    actual: float,
    target: float,
    dt_min: float,
    ramp_per_min: float = DEFAULT_RAMP_PER_MIN,
) -> float:
    """Slew the ACTUAL plate level toward ``target`` at a fixed rate (levels/min).

    Never overshoots (clamps exactly to ``target`` once within one step). ``ramp_per_min <=
    0`` freezes the plate in place (models a stuck/air-locked element that cannot move at
    all, e.g. mid a prime that never completes).
    """
    if dt_min <= 0 or actual == target:
        return float(target if actual == target else actual)
    if ramp_per_min <= 0:
        return float(actual)
    max_delta = abs(ramp_per_min) * dt_min
    delta = target - actual
    if abs(delta) <= max_delta:
        return float(target)
    return actual + (max_delta if delta > 0 else -max_delta)


def level_setpoint_f(
    plate_level: float,
    ambient_f: float,
    capacity: float = DEFAULT_CAPACITY,
) -> float:
    """The bed-temp setpoint IMPLIED by a plate level, ambient temp, and capacity.

    ``capacity`` in (0, 1]: the fraction of the idealized level->F swing (vs. ambient) that
    is actually achievable right now. 1.0 reproduces the full 55-110F theoretical range;
    smaller values narrow the achievable band toward ambient (a capacity-limited room/unit).
    """
    ideal_f = level_to_fahrenheit(plate_level)
    capacity = max(0.0, min(1.0, capacity))
    return clamp_fahrenheit(ambient_f + capacity * (ideal_f - ambient_f))


def step_bed_temp(
    bed_f: float,
    plate_level: float,
    ambient_f: float,
    dt_min: float,
    capacity: float = DEFAULT_CAPACITY,
    tau_min: float = DEFAULT_TAU_MIN,
) -> float:
    """First-order approach of the bed surface toward a capacity-bounded setpoint.

    ``setpoint = level_setpoint_f(plate_level, ambient_f, capacity)``; the bed moves a
    fraction ``1 - exp(-dt_min/tau_min)`` of the remaining gap toward it each call, so it
    always has LAG (never instant) and never exceeds the capacity-limited band no matter how
    long the same level is held.
    """
    if dt_min <= 0:
        return float(bed_f)
    setpoint = level_setpoint_f(plate_level, ambient_f, capacity)
    tau_min = max(tau_min, 1e-6)
    alpha = 1.0 - pow(2.718281828459045, -dt_min / tau_min)
    return bed_f + (setpoint - bed_f) * alpha
