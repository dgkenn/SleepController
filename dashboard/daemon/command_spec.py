"""Shared command STATE semantics for the two dashboard control daemons.

``run_daemon.DashboardDaemon`` (sync, simulator) and ``live_daemon.LiveDashboardDaemon`` (async,
real Pod) both drain the same command queue (see ``app.bridge.VALID_COMMANDS``) and must agree on
what each command means for daemon-side state (power/paused/away/mode/manual target/wake dict/
session bookkeeping). Historically that ``if t == "...": ...`` chain was hand-duplicated in both
``_apply_commands`` methods, which drifted more than once as commands were added.

This module is the single source of truth for the parts of that chain that are IDENTICAL between
the two daemons — pure state transitions with no device I/O. Each daemon still owns its own device
I/O (the sync simulator actuator vs the async real/simulated client calls); these helpers only
compute/mutate the plain state fields (``power_on``, ``paused``, ``away``, ``mode``,
``manual_target_f``, ``wake`` dict, ``session_mode``/``nap_plan``/``nap_deadline``) that both
daemons expose under the same names.

Commands intentionally NOT covered here (left per-daemon because they need device I/O or have
subtly different logic):
  - set_wake: gym-advisor + wake-window selection differ in how failures are handled between the
    two daemons (see each daemon's ``_apply_commands``); only the temp/power commands' pure
    clamping logic is shared.
  - self_test / self_test_cancel / comfort_cal_* / prime: real vs simulated device batteries.
  - induce_sleep / start_nap / end_session: call each daemon's own ``_start_induce`` /
    ``_start_nap`` / ``_end_session``, which are already per-daemon (nap/onset session setup);
    not part of command dispatch itself.

Nothing here changes command names, payloads, or observable behavior — it's a pure refactor.
"""

from __future__ import annotations

from typing import Optional, Protocol

# Re-exported for convenience so callers don't need to import app.bridge just for the type set.
try:
    from app.bridge import VALID_COMMANDS  # noqa: F401
except Exception:  # pragma: no cover - bridge always importable in practice
    VALID_COMMANDS = {
        "start", "pause", "resume", "stop", "safe_default",
        "set_mode", "set_temp", "nudge_temp", "set_wake", "clear_wake",
        "power_on", "power_off", "away_on", "away_off", "prime",
        "induce_sleep", "start_nap", "end_session",
        "self_test", "self_test_cancel",
        "comfort_cal_start", "comfort_cal_rate", "comfort_cal_cancel",
    }

TEMP_MIN_F, TEMP_MAX_F = 55.0, 110.0
NUDGE_FALLBACK_F = 70.0  # base used for a nudge when neither manual nor last target is known yet


def clamp_temp(f, lo: float = TEMP_MIN_F, hi: float = TEMP_MAX_F) -> float:
    """Clamp a requested target (water °F) into the safe range. Shared by set_temp/nudge_temp
    in both daemons so the safety bound can never drift between them."""
    return max(lo, min(hi, float(f)))


class HasControlState(Protocol):
    """The minimal state surface both daemons expose under identical attribute names."""
    mode: str
    paused: bool
    power_on: bool
    away: bool
    manual_target_f: Optional[float]
    last_target_f: Optional[float]


# ---------------------------------------------------------------------------------- pure state ops
def apply_pause(state: HasControlState) -> None:
    """``pause``: hold the current target, no device action."""
    state.paused = True


def apply_start_or_resume(state: HasControlState) -> None:
    """``start`` / ``resume``: resume normal control."""
    state.paused = False


def apply_power_on_state(state: HasControlState) -> None:
    """State half of ``power_on`` (device on-call is per-daemon: sim no-op vs turn_on_side())."""
    state.power_on = True
    state.paused = False
    state.away = False


def apply_power_off_state(state: HasControlState) -> None:
    """State half of ``power_off`` (device off-call is per-daemon)."""
    state.power_on = False
    state.paused = True


def apply_stop_state(state: HasControlState) -> None:
    """State half of the emergency ``stop``: always hard-off, regardless of dry-run/simulate."""
    state.power_on = False
    state.paused = True


def apply_away_on_state(state: HasControlState) -> None:
    """State half of ``away_on`` (device call is per-daemon: sim sets level 0, live calls
    set_away_mode(True))."""
    state.away = True
    state.power_on = False


def apply_away_off_state(state: HasControlState) -> None:
    """State half of ``away_off`` (live also turns the side back on via the client; the
    simulator's control loop will re-drive it on the next tick)."""
    state.away = False
    state.power_on = True


def apply_safe_default_state(state: HasControlState) -> None:
    """State half of ``safe_default``: back to a known-good auto/on/no-manual-override state.
    Callers still separately persist ``cfg.default_setpoints()`` via ``repo.save_setpoints``."""
    state.paused = False
    state.power_on = True
    state.away = False
    state.manual_target_f = None
    state.mode = "auto"


def apply_set_mode(state: HasControlState, payload: dict) -> None:
    """``set_mode``: switch auto/manual/view."""
    state.mode = payload.get("mode", "auto")


def apply_set_temp(state: HasControlState, payload: dict) -> float:
    """``set_temp`` pure state: clamp + switch to manual/on/unpaused. Returns the clamped target
    so the caller can issue the matching device command (sync set_level vs await set_level)."""
    target = clamp_temp(payload.get("target_f"))
    state.manual_target_f = target
    state.mode = "manual"
    state.power_on = True
    state.paused = False
    return target


def apply_nudge_temp(state: HasControlState, payload: dict) -> float:
    """``nudge_temp`` pure state: relative +/- adjust off the manual target (falling back to the
    last effective target, then a hardcoded default), clamped. Returns the new clamped target."""
    base = state.manual_target_f if state.manual_target_f is not None \
        else (state.last_target_f if state.last_target_f is not None else NUDGE_FALLBACK_F)
    target = clamp_temp(base + float(payload.get("delta_f", 0)))
    state.manual_target_f = target
    state.mode = "manual"
    state.power_on = True
    state.paused = False
    return target


def apply_clear_wake(state) -> None:
    """``clear_wake``: drop the alarm + required-wake-time/night-type context fields. ``state``
    here is the daemon itself (needs both ``wake`` and ``context``)."""
    state.wake = None
    state.context.required_wake_time = None
    state.context.night_type = None
    state.context.is_short_sleep_day = None


def build_wake_dict(cfg, payload: dict) -> dict:
    """The plain ``wake`` dict both daemons build from a ``set_wake`` payload, defaulting
    window/vibration from ``cfg.tunables`` when the picker didn't send an explicit value."""
    return {
        "wake_time": payload.get("wake_time"),
        "window_min": payload.get("window_min") or cfg.tunables.wake_window_min,
        "vibration_power": payload.get("vibration_power")
        if payload.get("vibration_power") is not None
        else cfg.tunables.wake_vibration_power,
        "thermal_level": payload.get("thermal_level"),
        "night_type": payload.get("night_type") or "auto",
    }
