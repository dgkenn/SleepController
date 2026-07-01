"""Shared command-state helpers (dashboard/daemon/command_spec.py).

These pure functions are the single source of truth for the STATE semantics that
``run_daemon.DashboardDaemon`` and ``live_daemon.LiveDashboardDaemon`` both apply from the
command queue (power/paused/away/mode/manual target/wake dict). Both daemons call the SAME
helpers for these commands, so this test both documents the contract and guards against the two
``_apply_commands`` chains drifting apart again in the future (the bug this module fixes).
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "dashboard", "daemon"))

import command_spec as cs  # noqa: E402


class _FakeState:
    """Minimal stand-in for the plain state fields both daemons expose under identical names."""
    def __init__(self):
        self.mode = "auto"
        self.paused = False
        self.power_on = True
        self.away = False
        self.manual_target_f = None
        self.last_target_f = None


class _FakeCfg:
    class _T:
        wake_window_min = 25
        wake_vibration_power = 40
    tunables = _T()


class _FakeDaemon(_FakeState):
    """Adds the ``context`` object clear_wake needs."""
    def __init__(self):
        super().__init__()
        self.wake = None

        class _Ctx:
            required_wake_time = "something"
            night_type = "work"
            is_short_sleep_day = True
        self.context = _Ctx()


def test_clamp_temp_bounds_to_safe_range():
    assert cs.clamp_temp(999) == cs.TEMP_MAX_F
    assert cs.clamp_temp(-999) == cs.TEMP_MIN_F
    assert cs.clamp_temp(70) == 70


def test_apply_pause_and_resume():
    s = _FakeState()
    cs.apply_pause(s)
    assert s.paused is True
    cs.apply_start_or_resume(s)
    assert s.paused is False


def test_apply_stop_state_hard_offs():
    s = _FakeState()
    cs.apply_stop_state(s)
    assert s.power_on is False and s.paused is True


def test_apply_power_on_off_state():
    s = _FakeState()
    s.away = True
    cs.apply_power_off_state(s)
    assert s.power_on is False and s.paused is True

    cs.apply_power_on_state(s)
    assert s.power_on is True and s.paused is False and s.away is False


def test_apply_away_on_off_state():
    s = _FakeState()
    cs.apply_away_on_state(s)
    assert s.away is True and s.power_on is False

    cs.apply_away_off_state(s)
    assert s.away is False and s.power_on is True


def test_apply_safe_default_state_resets_everything():
    s = _FakeState()
    s.paused, s.power_on, s.away = True, False, True
    s.manual_target_f, s.mode = 90.0, "manual"
    cs.apply_safe_default_state(s)
    assert s.paused is False and s.power_on is True and s.away is False
    assert s.manual_target_f is None and s.mode == "auto"


def test_apply_set_mode():
    s = _FakeState()
    cs.apply_set_mode(s, {"mode": "manual"})
    assert s.mode == "manual"
    cs.apply_set_mode(s, {})  # missing mode -> default "auto"
    assert s.mode == "auto"


def test_apply_set_temp_clamps_and_switches_to_manual():
    s = _FakeState()
    s.power_on, s.paused = False, True
    target = cs.apply_set_temp(s, {"target_f": 999})
    assert target == cs.TEMP_MAX_F
    assert s.manual_target_f == cs.TEMP_MAX_F
    assert s.mode == "manual" and s.power_on is True and s.paused is False


def test_apply_nudge_temp_relative_to_manual_target():
    s = _FakeState()
    s.manual_target_f = 64.0
    target = cs.apply_nudge_temp(s, {"delta_f": 2.0})
    assert target == 66.0 and s.manual_target_f == 66.0
    assert s.mode == "manual" and s.power_on is True and s.paused is False


def test_apply_nudge_temp_falls_back_to_last_target_then_default():
    s = _FakeState()
    s.last_target_f = 68.0
    cs.apply_nudge_temp(s, {"delta_f": 1.0})
    assert s.manual_target_f == 69.0

    s2 = _FakeState()  # neither manual nor last target known -> hardcoded fallback base
    cs.apply_nudge_temp(s2, {"delta_f": 0.0})
    assert s2.manual_target_f == cs.NUDGE_FALLBACK_F


def test_apply_nudge_temp_still_clamped():
    s = _FakeState()
    s.manual_target_f = 109.0
    cs.apply_nudge_temp(s, {"delta_f": 50.0})
    assert s.manual_target_f == cs.TEMP_MAX_F


def test_build_wake_dict_defaults_from_cfg_tunables():
    cfg = _FakeCfg()
    d = cs.build_wake_dict(cfg, {"wake_time": "06:15", "night_type": "work"})
    assert d["wake_time"] == "06:15"
    assert d["window_min"] == 25            # defaulted from cfg.tunables
    assert d["vibration_power"] == 40        # defaulted from cfg.tunables
    assert d["night_type"] == "work"

    d2 = cs.build_wake_dict(cfg, {"wake_time": "05:00", "window_min": 10,
                                  "vibration_power": 0, "night_type": None})
    assert d2["window_min"] == 10            # explicit override wins
    assert d2["vibration_power"] == 0        # explicit 0 is respected, not treated as falsy-default
    assert d2["night_type"] == "auto"        # missing/None night_type defaults to "auto"


def test_apply_clear_wake_resets_wake_and_context():
    d = _FakeDaemon()
    d.wake = {"wake_time": "06:00"}
    cs.apply_clear_wake(d)
    assert d.wake is None
    assert d.context.required_wake_time is None
    assert d.context.night_type is None
    assert d.context.is_short_sleep_day is None


# ---- drift guard: both daemons actually delegate to the shared helpers ------------------------
def test_both_daemons_use_the_shared_command_spec_module():
    """Regression guard: if either daemon stops importing command_spec (e.g. someone re-inlines
    the state logic during a future edit), this fails loudly instead of silently drifting again."""
    import inspect

    import live_daemon
    import run_daemon

    assert run_daemon.cs is cs
    assert live_daemon.cs is cs

    sync_src = inspect.getsource(run_daemon.DashboardDaemon._apply_commands)
    async_src = inspect.getsource(live_daemon.LiveDashboardDaemon._apply_commands)
    for helper in ("apply_stop_state", "apply_pause", "apply_start_or_resume",
                   "apply_power_on_state", "apply_power_off_state", "apply_away_on_state",
                   "apply_away_off_state", "apply_safe_default_state", "apply_set_mode",
                   "apply_set_temp", "apply_nudge_temp", "build_wake_dict", "apply_clear_wake"):
        assert f"cs.{helper}" in sync_src, f"run_daemon no longer calls cs.{helper}"
        assert f"cs.{helper}" in async_src, f"live_daemon no longer calls cs.{helper}"


@pytest.mark.parametrize("delta", [-1.5, 0, 3.25])
def test_nudge_and_set_temp_agree_with_daemon_clamp_helpers(delta):
    """The daemons' own ``_clamp_temp`` wrappers must stay consistent with the shared clamp."""
    import live_daemon
    import run_daemon

    assert run_daemon.DashboardDaemon._clamp_temp(None, 60 + delta) == cs.clamp_temp(60 + delta)
    assert live_daemon.LiveDashboardDaemon._clamp_temp(60 + delta) == cs.clamp_temp(60 + delta)
