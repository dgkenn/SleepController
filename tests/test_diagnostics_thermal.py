"""Tests for the water-loop/capacity, external-conflict, and frozen-telemetry detection
engine (``sleepctl.diagnostics_thermal``).

Pure unit tests against synthetic ``state_history``-shaped rows -- no DB, no daemon, no
wall clock (every ``now`` is passed in explicitly), matching the "was it discovered live but
never automatically" failure modes: an air-bound water loop, a stuck prime, frozen telemetry
from a crash-looping daemon, and the Eight Sleep app's own schedule fighting this controller.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sleepctl.diagnostics_thermal import (
    analyze_thermal_capacity,
    detect_external_conflict,
    detect_frozen_telemetry,
)

BASE = datetime(2026, 7, 2, 2, 0, 0)


def _row(minutes_offset, target_level=None, bed_temp_f=None, device_level=None,
         device_target_level=None, device=None):
    ts = BASE + timedelta(minutes=minutes_offset)
    return {
        "ts": ts.isoformat(),
        "target_level": target_level,
        "bed_temp_f": bed_temp_f,
        "extra": {
            "device_level": device_level,
            "device_target_level": device_target_level,
            "device": device or {},
        },
    }


def _iso(minutes_offset):
    return (BASE + timedelta(minutes=minutes_offset)).isoformat()


# --------------------------------------------------------------------------------- stuck prime
def test_stuck_prime_flagged_after_six_plus_minutes():
    # Priming continuously True for 10 samples over 9 minutes -> past the 6-min threshold.
    history = [
        _row(i, target_level=0, bed_temp_f=70.0, device={"priming": True})
        for i in range(0, 10)
    ]
    device = {"priming": True, "needs_priming": False, "last_prime": "2026-07-01T20:00:00"}
    result = analyze_thermal_capacity(device, history, now_iso=_iso(9))
    assert result["status"] == "stuck_prime"
    assert "air-bound" in result["remedy"].lower()
    assert "prime" in result["reason"].lower()


def test_priming_just_started_is_not_stuck():
    # Only just started priming (no historical confirmation of a long-running episode) ->
    # must not false-positive as stuck.
    history = [
        _row(i, target_level=0, bed_temp_f=70.0, device={"priming": False})
        for i in range(0, 8)
    ]
    device = {"priming": True, "needs_priming": False}
    result = analyze_thermal_capacity(device, history, now_iso=_iso(8))
    assert result["status"] != "stuck_prime"


# --------------------------------------------------------------------------------- low water
def test_needs_priming_flags_low_water():
    device = {"priming": False, "needs_priming": True}
    result = analyze_thermal_capacity(device, [], now_iso=_iso(0))
    assert result["status"] == "low_water"
    assert "distilled water" in result["remedy"].lower()


def test_recent_low_water_event_flags_low_water():
    device = {"priming": False, "needs_priming": False,
              "last_low_water": _iso(-30)}  # 30 min ago
    result = analyze_thermal_capacity(device, [], now_iso=_iso(0))
    assert result["status"] == "low_water"


def test_old_low_water_event_does_not_flag():
    device = {"priming": False, "needs_priming": False,
              "last_low_water": (BASE - timedelta(hours=48)).isoformat()}
    history = [
        _row(i, target_level=10, bed_temp_f=70.0 + i * 0.1, device_level=10 + i)
        for i in range(0, 8)
    ]
    result = analyze_thermal_capacity(device, history, now_iso=_iso(7))
    assert result["status"] != "low_water"


# --------------------------------------------------------------------------------- capacity
def test_reduced_capacity_flagged_for_unresponsive_strong_cool_command():
    device = {"priming": False, "needs_priming": False}
    history = [
        _row(i, target_level=-90, bed_temp_f=69.5 + (i % 2) * 0.1,
             device_level=88 - i)  # barely moves: 88 -> 81 over 8 samples
        for i in range(0, 8)
    ]
    result = analyze_thermal_capacity(device, history, now_iso=_iso(7))
    assert result["status"] == "reduced_capacity"
    assert "air" in result["remedy"].lower()


def test_normal_responsive_cool_is_not_flagged():
    device = {"priming": False, "needs_priming": False}
    history = [
        _row(i, target_level=-90, bed_temp_f=72.0 - i * 0.6,  # drops ~4.2F over 8 samples
             device_level=95 - i * 8)  # drops steadily toward target
        for i in range(0, 8)
    ]
    result = analyze_thermal_capacity(device, history, now_iso=_iso(7))
    assert result["status"] == "ok"


def test_mild_command_does_not_trigger_capacity_check():
    # target_level never crosses the "strong" threshold -> no capacity verdict either way.
    device = {"priming": False, "needs_priming": False}
    history = [
        _row(i, target_level=-20, bed_temp_f=70.0, device_level=50)
        for i in range(0, 8)
    ]
    result = analyze_thermal_capacity(device, history, now_iso=_iso(7))
    assert result["status"] == "ok"


# --------------------------------------------------------------------------------- insufficient
def test_insufficient_history_returns_ok_or_insufficient_data():
    device = {"priming": False, "needs_priming": False}
    history = [_row(i, target_level=-90, bed_temp_f=70.0, device_level=50) for i in range(0, 3)]
    result = analyze_thermal_capacity(device, history, now_iso=_iso(2))
    assert result["status"] in ("ok", "insufficient_data")

    result2 = detect_frozen_telemetry(history)
    assert result2["status"] in ("ok", "insufficient_data")

    result3 = detect_external_conflict(device, history)
    assert result3["status"] in ("ok", "insufficient_data")


def test_empty_history_never_raises():
    device = {}
    assert analyze_thermal_capacity(device, [], now_iso=_iso(0))["status"] in (
        "ok", "insufficient_data")
    assert detect_frozen_telemetry([])["status"] in ("ok", "insufficient_data")
    assert detect_external_conflict(device, [])["status"] in ("ok", "insufficient_data")


# --------------------------------------------------------------------------------- frozen telemetry
def test_frozen_telemetry_flagged_when_bed_temp_and_level_never_move():
    history = [
        _row(i, target_level=-80, bed_temp_f=68.0, device_level=42)
        for i in range(0, 9)  # 9 samples, 8 minutes span
    ]
    result = detect_frozen_telemetry(history)
    assert result["status"] == "frozen_telemetry"
    assert "restart" in result["remedy"].lower()


def test_changing_telemetry_is_not_flagged():
    history = [
        _row(i, target_level=-80, bed_temp_f=68.0 - i * 0.1, device_level=42 - i)
        for i in range(0, 9)
    ]
    result = detect_frozen_telemetry(history)
    assert result["status"] == "ok"


def test_frozen_but_neutral_command_is_not_flagged():
    # bed steady at a neutral/hold target isn't a bug -- must not false-positive.
    history = [
        _row(i, target_level=0, bed_temp_f=68.0, device_level=0)
        for i in range(0, 9)
    ]
    result = detect_frozen_telemetry(history)
    assert result["status"] == "ok"


def test_frozen_but_too_short_span_is_insufficient_data():
    # 8 samples but all within the same minute -> span too short to call it frozen.
    history = [
        {"ts": (BASE + timedelta(seconds=i * 10)).isoformat(), "target_level": -80,
         "bed_temp_f": 68.0, "extra": {"device_level": 42}}
        for i in range(0, 9)
    ]
    result = detect_frozen_telemetry(history)
    assert result["status"] == "insufficient_data"


# --------------------------------------------------------------------------------- external conflict
def test_schedule_activity_flags_external_conflict():
    device = {"external_schedule": {"activity": "schedule", "target_level": 55, "active": True}}
    result = detect_external_conflict(device, [])
    assert result["status"] == "external_setpoint_conflict"
    assert "55" in result["remedy"]
    assert "autopilot" in result["remedy"].lower() or "schedule" in result["remedy"].lower()


def test_active_schedule_honoring_our_override_is_not_a_conflict():
    # On the Pod activity reads 'schedule' whenever a smart session is active -- including
    # when that session is faithfully applying OUR commanded override. Target == our command
    # -> not a conflict (the false positive we hit live: schedTarget=-44 == our -44).
    device = {"external_schedule": {"activity": "schedule", "target_level": -44, "active": True}}
    history = [
        _row(i, target_level=-44, bed_temp_f=70.0, device_target_level=-44)
        for i in range(0, 8)
    ]
    result = detect_external_conflict(device, history)
    assert result["status"] == "ok"


def test_active_schedule_disagreeing_with_command_still_flags():
    # schedule active AND its target is far from what we commanded -> real conflict.
    device = {"external_schedule": {"activity": "schedule", "target_level": 55, "active": True}}
    history = [
        _row(i, target_level=-44, bed_temp_f=70.0, device_target_level=-44)
        for i in range(0, 8)
    ]
    result = detect_external_conflict(device, history)
    assert result["status"] == "external_setpoint_conflict"
    assert "55" in result["reason"]


def test_no_schedule_and_no_disagreement_is_ok():
    device = {"external_schedule": {"activity": "none", "active": False}}
    history = [
        _row(i, target_level=-50, bed_temp_f=70.0, device_target_level=-50)
        for i in range(0, 8)
    ]
    result = detect_external_conflict(device, history)
    assert result["status"] == "ok"


def test_repeated_target_disagreement_flags_conflict_without_schedule_flag():
    device = {}
    history = [
        _row(i, target_level=-50, bed_temp_f=70.0, device_target_level=20)  # way off
        for i in range(0, 8)
    ]
    result = detect_external_conflict(device, history)
    assert result["status"] == "external_setpoint_conflict"
    assert "20" in result["reason"]


def test_occasional_disagreement_is_not_flagged():
    device = {}
    history = [
        _row(i, target_level=-50, bed_temp_f=70.0,
             device_target_level=-50 if i < 6 else 20)  # only 2/8 disagree
        for i in range(0, 8)
    ]
    result = detect_external_conflict(device, history)
    assert result["status"] == "ok"
