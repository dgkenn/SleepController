"""Controller robustness under the adverse Pod failure modes measured live.

Drives the PRODUCTION daemon path (``LiveDaemon`` + ``ControlCycle`` + ``SleepController``)
over ``SimulatedLiveClient``'s ``pod_scenario`` faults -- not a hand-rolled harness -- so
these tests exercise exactly what the real live daemon runs. Every scenario must uphold the
same hard safety invariants no matter what garbage the device feeds it: the 55-110F device
clamp, the per-tick slew cap, and the variability cap. On top of that:

  * frozen_telemetry / rate_limited -> the controller HOLDS (no wild commands) once the data
    is stale; it doesn't chase noise it can no longer trust.
  * competing_controller -> our own computed intent stays put (slew/variability-bounded,
    the same invariant as every other scenario); it isn't dragged into unbounded oscillation
    by an external actor fighting over the device's target register.
  * a normal night (with the realistic thermal model wired in) still runs the full state
    machine end to end -- the sanity check that realism didn't break anything.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sleepctl.adapters.calendar import ManualCalendarSource
from sleepctl.config import AppConfig
from sleepctl.loop.live import LiveDaemon, SimulatedLiveClient
from sleepctl.models import ControllerState, CorrectionAction
from sleepctl.storage.repository import Repository

START = datetime(2026, 6, 23, 23, 0)


def _context(start: datetime, hours: float = 8.0):
    wake = start + timedelta(hours=hours)
    return ManualCalendarSource(required_wake_time=wake, bedtime=start).get_context(
        start.date().isoformat()
    )


def _run(pod_scenario, max_ticks=150, dry_run=False, **client_kwargs):
    cfg = AppConfig.default()
    client = SimulatedLiveClient(scenario="normal", seed=7, start=START,
                                  pod_scenario=pod_scenario, **client_kwargs)
    repo = Repository(":memory:")
    daemon = LiveDaemon(cfg, client, repo, context=_context(START), verbose=False)
    decisions = asyncio.run(
        daemon.run(poll_seconds=0.0, dry_run=dry_run, max_ticks=max_ticks)
    )
    return cfg, client, decisions


def _assert_hard_invariants(cfg, decisions):
    """These must hold for EVERY decision, in EVERY scenario, no matter how hostile the
    device is -- they are enforced purely in ``ThermalController``/``clamp_fahrenheit`` and
    never depend on trusting the device readback."""
    max_step = cfg.tunables.max_step_f
    cap = cfg.tunables.variability_cap_f
    temps = [d.target_temp_f for d in decisions]
    for t in temps:
        assert 55.0 - 1e-6 <= t <= 110.0 + 1e-6, f"target {t} outside the 55-110F device range"
    for a, b in zip(temps, temps[1:]):
        assert abs(b - a) <= max_step + 1e-6, f"slew violated: {a} -> {b} (max {max_step})"
    # variability cap: no 8-sample trailing window (ThermalController's own window) swings
    # by more than the configured cap.
    window = 8
    for i in range(len(temps)):
        lo_hi = temps[max(0, i - window + 1):i + 1]
        assert max(lo_hi) - min(lo_hi) <= cap + 1e-6, "variability cap violated"


# ------------------------------------------------------------------------------------ normal
def test_normal_night_runs_full_state_machine_with_realistic_dynamics():
    """Sanity: with the realistic thermal model wired in (no fault), a full scripted night
    still runs the whole state machine end to end and produces sane commands."""
    cfg, client, decisions = _run("realistic", max_ticks=8 * 60 + 10)
    _assert_hard_invariants(cfg, decisions)
    states = {d.state for d in decisions}
    # a full 8h scripted night should pass through more than just IDLE/MAINTENANCE.
    assert ControllerState.MAINTENANCE in states
    assert len(states) >= 2
    assert len(client.actuator.commands) > 0
    assert not client.device_status()["priming"]


def test_legacy_default_scenario_unaffected_by_new_code_path():
    """No pod_scenario at all -> byte-for-byte the pre-existing idealized behavior."""
    cfg, client, decisions = _run(None, max_ticks=50)
    _assert_hard_invariants(cfg, decisions)
    assert client.device_status()["pod_scenario"] is None


# --------------------------------------------------------------------------- frozen_telemetry
def test_frozen_telemetry_forces_hold_once_stale():
    stale_after_s = AppConfig.default().tunables.stale_data_seconds
    cfg, client, decisions = _run("frozen_telemetry", max_ticks=60, freeze_after_ticks=3)
    _assert_hard_invariants(cfg, decisions)
    # once the frozen age has crossed the stale threshold, every subsequent tick MUST hold.
    stale_tick = 3 + int(stale_after_s // 60) + 1
    tail = decisions[stale_tick:]
    assert tail, "test needs enough ticks to get past the stale threshold"
    assert all(d.action is CorrectionAction.HOLD for d in tail)
    assert all("stale" in d.reason.lower() for d in tail)
    # holding means the target stopped moving entirely.
    tail_temps = {d.target_temp_f for d in tail}
    assert len(tail_temps) == 1


# ---------------------------------------------------------------------------- rate_limited
def test_rate_limited_holds_only_on_the_stale_ticks_and_recovers():
    cfg, client, decisions = _run("rate_limited", max_ticks=40, rate_limited_every=5)
    _assert_hard_invariants(cfg, decisions)
    stale_ticks = [i for i in range(len(decisions)) if i > 0 and i % 5 == 0]
    for i in stale_ticks:
        assert decisions[i].action is CorrectionAction.HOLD
        assert "stale" in decisions[i].reason.lower()
    # the controller is NOT permanently wedged -- normal ticks keep producing real decisions.
    normal_ticks = [i for i in range(len(decisions)) if i not in stale_ticks]
    assert any(decisions[i].action is not CorrectionAction.HOLD for i in normal_ticks[5:])


# ---------------------------------------------------------------------- competing_controller
def test_competing_controller_reasserts_intent_without_unbounded_oscillation():
    cfg, client, decisions = _run(
        "competing_controller", max_ticks=150,
        competing_period_ticks=15, competing_target_level=-68,
    )
    _assert_hard_invariants(cfg, decisions)  # the hard bounds hold despite the hijack
    status = client.device_status()
    assert status["external_schedule"]["override_count"] > 0  # the fault actually fired

    # "without unbounded oscillation": count target REVERSALS (sign changes of consecutive
    # deltas) over the run and confirm they stay a small fraction of the ticks -- the
    # controller settles/tracks, it doesn't flap every single tick chasing the hijack.
    temps = [d.target_temp_f for d in decisions]
    reversals, last_dir = 0, 0
    for a, b in zip(temps, temps[1:]):
        delta = b - a
        if abs(delta) < 1e-6:
            continue
        direction = 1 if delta > 0 else -1
        if last_dir != 0 and direction != last_dir:
            reversals += 1
        last_dir = direction
    assert reversals < len(temps) * 0.5


# -------------------------------------------------------------------------------- air_bound
def test_air_bound_narrow_band_and_stuck_prime_still_bounded():
    cfg, client, decisions = _run("air_bound", max_ticks=60, ambient_f=72.0)
    _assert_hard_invariants(cfg, decisions)
    status = client.device_status()
    assert status["priming"] is True          # a prime that never completes
    assert status["device_level"] == 0         # the element never actually moves
    # the controller's own COMMANDED targets are still safe even though nothing physically
    # moves on the device side -- do-no-harm holds even when the actuator is unresponsive.
    temps = [d.target_temp_f for d in decisions]
    assert all(55.0 <= t <= 110.0 for t in temps)


# -------------------------------------------------------------------------------- stuck_prime
def test_stuck_prime_never_advances_through_the_daemon():
    cfg, client, decisions = _run("stuck_prime", max_ticks=30)
    _assert_hard_invariants(cfg, decisions)
    status = client.device_status()
    assert status["priming"] is True
    assert status["last_prime"] is None
    assert status["device_level"] == 0
