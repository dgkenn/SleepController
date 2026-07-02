"""3AM WAKE targeted analysis: the controller-side pre-emption hook.

Verifies the additive, gated integration at ``SleepController.decide``'s existing pre-empt
union (wake-risk OR precursor OR micro-arousal OR -- new -- a high-confidence personal
recurring wake window): it only ever ADDS a vote, never fires below the confidence/nights
gate or when disabled, and any nudge it does apply still runs through the existing slew-limit
/ variability-cap / 55-110°F clamp (it reuses the SETTLE_COOL thermal path verbatim)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.models import ControllerState, SensorFrame, SleepStage, ThermalIntent


def _calm_frame(ts, bed=70.0, room=67.0):
    return SensorFrame(timestamp=ts, stage=SleepStage.LIGHT, stage_confidence=0.85,
                       heart_rate=54, hrv=62, respiratory_rate=14, movement=0.05,
                       presence=True, bed_temp_f=bed, room_temp_f=room, data_age_seconds=5.0)


def _make_controller(cfg=None):
    cfg = cfg or AppConfig.default()
    ctrl = SleepController(cfg)
    ctrl.sm.state = ControllerState.MAINTENANCE
    ctrl._last_target_f = 70.0
    return ctrl


def _high_conf_window(nights_observed=12, confidence=0.8, start=180, end=210):
    return {"window": {"label": "03:00–03:30", "bin_start_min": start, "bin_end_min": end,
                       "stage_exited": "rem", "nights_observed": nights_observed,
                       "confidence": confidence, "confidence_label": "high"}}


def _run(ctrl, now, recent=None):
    if ctrl._sleep_onset_time is None:
        ctrl._sleep_onset_time = now - timedelta(hours=3)
    if ctrl._bed_entry_time is None:
        ctrl._bed_entry_time = now - timedelta(hours=3, minutes=10)
    recent = recent or [_calm_frame(now - timedelta(minutes=i)) for i in range(10, 0, -1)]
    frame = _calm_frame(now)
    return ctrl.decide(frame, None, recent, now)


def test_high_confidence_window_preempts_within_lead_time():
    cfg = AppConfig.default()
    ctrl = _make_controller(cfg)
    ctrl.set_wake_window_report([_high_conf_window()])
    now = datetime(2026, 6, 24, 2, 45)  # inside [02:40, 03:30) with the default 20-min lead
    decision = _run(ctrl, now)

    assert ctrl.last_wake_window_preempt is not None
    assert ctrl.last_wake_window_preempt["label"] == "03:00–03:30"
    assert decision.thermal_intent is ThermalIntent.SETTLE_COOL
    # slew guard: never moves more than max_step_f from the last commanded value in one tick
    assert abs(decision.target_temp_f - 70.0) <= cfg.tunables.max_step_f + 1e-6
    # the universal device-range clamp
    assert 55.0 <= decision.target_temp_f <= 110.0


def test_nudge_never_exceeds_the_configured_cap_over_many_ticks():
    """Even letting the controller run many ticks inside the window (so slew has time to
    accumulate), the resolved target never overshoots what SETTLE_COOL's own comfort cap
    allows -- the variability cap + settle cap bound the cumulative move, not just one step."""
    cfg = AppConfig.default()
    ctrl = _make_controller(cfg)
    ctrl.set_wake_window_report([_high_conf_window()])
    t0 = datetime(2026, 6, 24, 2, 41)
    last_target = 70.0
    ctrl._last_target_f = last_target
    for i in range(12):
        now = t0 + timedelta(minutes=i)
        decision = _run(ctrl, now)
        last_target = decision.target_temp_f
    neutral = cfg.tunables.neutral_temp_f + cfg.tunables.hot_sleeper_cool_bias_f
    cap = cfg.tunables.maintenance_settle_cap_f
    assert neutral - cap - 0.5 <= last_target <= neutral + cap + 0.5  # small feedback slack
    assert 55.0 <= last_target <= 110.0


def test_silent_below_confidence_threshold():
    cfg = AppConfig.default()
    ctrl = _make_controller(cfg)
    ctrl.set_wake_window_report([_high_conf_window(confidence=0.2)])
    now = datetime(2026, 6, 24, 2, 45)
    decision = _run(ctrl, now)
    assert ctrl.last_wake_window_preempt is None
    assert decision.thermal_intent is not ThermalIntent.SETTLE_COOL


def test_silent_below_min_nights():
    cfg = AppConfig.default()
    ctrl = _make_controller(cfg)
    ctrl.set_wake_window_report([_high_conf_window(nights_observed=1)])
    now = datetime(2026, 6, 24, 2, 45)
    decision = _run(ctrl, now)
    assert ctrl.last_wake_window_preempt is None
    assert decision.thermal_intent is not ThermalIntent.SETTLE_COOL


def test_disabled_flag_is_a_hard_off():
    cfg = AppConfig.default()
    cfg.tunables.wake_window_preempt_enabled = False
    ctrl = _make_controller(cfg)
    ctrl.set_wake_window_report([_high_conf_window()])
    now = datetime(2026, 6, 24, 2, 45)
    decision = _run(ctrl, now)
    assert ctrl.last_wake_window_preempt is None
    assert decision.thermal_intent is not ThermalIntent.SETTLE_COOL


def test_no_report_attached_is_a_no_op():
    cfg = AppConfig.default()
    ctrl = _make_controller(cfg)
    assert ctrl.wake_window_report is None
    now = datetime(2026, 6, 24, 2, 45)
    decision = _run(ctrl, now)
    assert ctrl.last_wake_window_preempt is None
    assert decision.thermal_intent is not ThermalIntent.SETTLE_COOL


def test_outside_the_window_is_silent_even_at_high_confidence():
    cfg = AppConfig.default()
    ctrl = _make_controller(cfg)
    ctrl.set_wake_window_report([_high_conf_window()])
    now = datetime(2026, 6, 24, 1, 0)  # well before the 02:40 lead start
    decision = _run(ctrl, now)
    assert ctrl.last_wake_window_preempt is None
    assert decision.thermal_intent is not ThermalIntent.SETTLE_COOL


def test_never_preempts_out_of_deep_sleep():
    """Defense in depth: even with the wake-window vote true, MaintenanceRoutine itself never
    lets a preempt_cool signal disturb DEEP sleep (see MaintenanceRoutine.step)."""
    cfg = AppConfig.default()
    ctrl = _make_controller(cfg)
    ctrl.set_wake_window_report([_high_conf_window()])
    now = datetime(2026, 6, 24, 2, 45)
    ctrl._sleep_onset_time = now - timedelta(hours=3)
    ctrl._bed_entry_time = now - timedelta(hours=3, minutes=10)
    ctrl._last_target_f = 70.0
    recent = [_calm_frame(now - timedelta(minutes=i)) for i in range(10, 0, -1)]
    deep_frame = SensorFrame(timestamp=now, stage=SleepStage.DEEP, stage_confidence=0.85,
                             heart_rate=52, hrv=68, respiratory_rate=13, movement=0.02,
                             presence=True, bed_temp_f=66.0, room_temp_f=65.0,
                             data_age_seconds=5.0)
    decision = ctrl.decide(deep_frame, None, recent, now)
    assert decision.thermal_intent is ThermalIntent.DEEP_BIAS_COOL  # never SETTLE_COOL in deep
