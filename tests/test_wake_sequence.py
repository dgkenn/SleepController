"""The wake experience, end to end: lightest stage, building vibration, sunrise light.

Three behaviours that are meant to happen together, and that only mean something in combination:

  1. WAIT for the lightest sleep available inside the window rather than waking on the deadline —
     waking out of deep sleep is the worst case for inertia (Brooks & Lack 2006).
  2. BUILD the vibration: silent, then gentle, then stronger, then full. A flat buzz is a worse
     waking signal than a rhythmic one (McFarlane 2020), and the whole point of starting soft is
     that most mornings never need the loud end.
  3. RAMP the light through the dawn window, so the circadian signal arrives before the haptic one.

Each is testable alone and each is tested alone elsewhere; what this file pins is the SEQUENCE, in
order, over a realistic window — the thing that would look fine in unit tests while feeling wrong
at 7am.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from sleepctl.controller.wake_orchestrator import WakeConfig, WakeOrchestrator
from sleepctl.models import SensorFrame, SleepStage

DEADLINE = datetime(2026, 8, 1, 7, 0)


def _frame(t, stage, hr=55.0, move=0.05):
    return SensorFrame(timestamp=t, stage=stage, presence=True, heart_rate=hr, hrv=55.0,
                       respiratory_rate=14.0, movement=move, bed_temp_f=72.0,
                       room_temp_f=68.0, data_age_seconds=5)


def _run(light_enabled=True, light_at_min=12, minutes=40, step=2):
    """Drive a window that is DEEP until ``light_at_min`` before the deadline, then LIGHT."""
    orch = WakeOrchestrator(WakeConfig(window_min=30, light_enabled=light_enabled,
                                       gentle_vibration=30, strong_vibration=70,
                                       max_vibration=100))
    out = []
    for i in range(minutes, -1, -step):
        t = DEADLINE - timedelta(minutes=i)
        deep = i > light_at_min
        f = _frame(t, SleepStage.DEEP if deep else SleepStage.LIGHT,
                   hr=50.0 if deep else 62.0, move=0.01 if deep else 0.25)
        out.append((i, orch.evaluate(t, f, [], DEADLINE)))
    return out


# ------------------------------------------------------------------ 1. lightest stage
def test_it_holds_through_deep_sleep_rather_than_waking_on_a_timer():
    """The reason the window exists. Vibrating while the sleeper is in deep sleep is the single
    worst thing this subsystem could do."""
    for mins_out, act in _run(light_at_min=12):
        if mins_out > 12:                      # still in DEEP
            assert act.vibration_power == 0, f"buzzed during deep sleep at T-{mins_out}"


def test_it_engages_as_soon_as_a_light_moment_arrives():
    engaged = [m for m, a in _run(light_at_min=12) if a.vibration_power > 0]
    assert engaged, "never engaged at all"
    assert max(engaged) <= 12, f"waited past the light-sleep moment (engaged at T-{max(engaged)})"


def test_a_night_that_never_lightens_still_wakes_at_the_deadline():
    """No light moment must never mean no wake. This is the guarantee the whole design rests on."""
    orch = WakeOrchestrator(WakeConfig(window_min=30, max_vibration=100))
    for i in range(40, -1, -2):
        t = DEADLINE - timedelta(minutes=i)
        act = orch.evaluate(t, _frame(t, SleepStage.DEEP, hr=48.0, move=0.0), [], DEADLINE)
    assert act.should_wake is True
    assert act.vibration_power == 100
    assert act.phase == "fire"


# ------------------------------------------------------------------ 2. building vibration
def test_vibration_builds_softest_first_and_never_goes_backwards():
    powers = [a.vibration_power for _, a in _run(light_at_min=12) if a.vibration_power > 0]
    assert powers == sorted(powers), f"vibration must only ever increase, got {powers}"
    assert powers[0] < powers[-1], "it must actually escalate, not sit at one level"
    assert powers[0] == 30, f"the first buzz must be the GENTLE rung, got {powers[0]}"
    assert powers[-1] == 100, "it must reach full power by the deadline"


def test_every_rung_of_the_ladder_is_used():
    seen = {a.vibration_power for _, a in _run(light_at_min=12)} - {0}
    assert seen == {30, 70, 100}, f"expected gentle/strong/max, saw {sorted(seen)}"


def test_the_pulse_rhythm_builds_with_the_power():
    """Rhythm, not a flat buzz — a constant signal worsens inertia where a building one eases it."""
    pulses = [a.vibration_pulse for _, a in _run(light_at_min=12) if a.vibration_power > 0]
    assert pulses[0] == "slow"
    assert "medium" in pulses
    assert pulses[-1] == "continuous"


def test_the_gentle_rung_gets_a_real_chance_before_escalating():
    """If it jumped to full in one tick the 'start soft' design would be decorative."""
    gentle = [m for m, a in _run(light_at_min=12) if a.vibration_power == 30]
    assert len(gentle) >= 1, "the gentle rung was skipped entirely"


# ------------------------------------------------------------------ 3. the sunrise light
def test_the_light_ramps_up_through_the_dawn_window():
    levels = [a.light_level for _, a in _run(light_enabled=True)]
    rising = [x for x in levels if x > 0]
    assert rising, "the dawn light never came on"
    assert rising == sorted(rising), f"the sunrise must only brighten, got {rising}"
    assert rising[-1] == pytest.approx(1.0), "it must reach full brightness by the deadline"


def test_the_light_leads_the_vibration():
    """The circadian signal should arrive BEFORE the haptic one — that ordering is the point of a
    dawn simulation, not an accident of timing."""
    run = _run(light_enabled=True, light_at_min=12)
    first_light = next(m for m, a in run if a.light_level > 0)
    first_buzz = next(m for m, a in run if a.vibration_power > 0)
    assert first_light > first_buzz, (
        f"light started at T-{first_light}, vibration at T-{first_buzz}; light must come first")


def test_the_light_is_at_least_half_brightness_once_actively_waking():
    for _, a in _run(light_enabled=True, light_at_min=12):
        if a.vibration_power > 0:
            assert a.light_level >= 0.5, "once buzzing, the room should not still be dark"


def test_without_hue_configured_the_wake_still_works_silently():
    """Hue is optional. Its absence must cost the light and NOTHING else."""
    with_hue = _run(light_enabled=True, light_at_min=12)
    without = _run(light_enabled=False, light_at_min=12)
    assert all(a.light_level == 0.0 for _, a in without)
    assert ([a.vibration_power for _, a in with_hue]
            == [a.vibration_power for _, a in without]), \
        "the vibration ladder must be identical with or without lights"
    assert without[-1][1].should_wake is True


# ------------------------------------------------------------------ the daemon's hand-off
def test_the_daemon_drives_hue_from_the_wake_action():
    """The bulbs follow light_level and the therapy lamp follows should_wake. Pinned against the
    source because reproducing it needs a live daemon + bridge."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent
           / "dashboard" / "daemon" / "live_daemon.py").read_text()
    i = src.index("def _drive_dawn")
    block = src[i:i + 900]
    assert "light_level" in block, "the sunrise ramp must drive the bulb level"
    assert "should_wake" in block, "the therapy lamp must fire on the wake signal"
    assert "set_level(0.0)" in block, "outside the window everything must be turned OFF"


def test_configuring_hue_is_what_enables_the_ramp():
    """set_dawn_light is the switch; without it the orchestrator never computes a level."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent
           / "dashboard" / "daemon" / "live_daemon.py").read_text()
    assert "set_dawn_light" in src, "nothing turns the dawn ramp on"
    i = src.index("def _refresh_hue")
    assert "set_dawn_light" in src[i:i + 1800], \
        "the ramp must be enabled from the Hue config, not left to a default"
