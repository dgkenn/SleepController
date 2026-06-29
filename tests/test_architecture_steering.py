"""In-night architecture steering ("nudge me deeper"): trajectory math, the bounded decision
rule, the maintenance wiring, the controller's accrual + veto, and the steer-event ledger."""

from datetime import datetime, timedelta

from sleepctl.benchmarks import NightMode, targets_for
from sleepctl.config import AppConfig
from sleepctl.controller.architecture import ArchitectureSteering, IdealTrajectory
from sleepctl.controller.controller import SleepController
from sleepctl.controller.maintenance import MaintenanceRoutine
from sleepctl.models import NightObjective, SensorFrame, SleepStage, ThermalIntent


def _frame(ts, stage, presence=True, bed=70.0, move=0.05):
    return SensorFrame(timestamp=ts, stage=stage, stage_confidence=0.8, heart_rate=54,
                       hrv=60, respiratory_rate=14.0, movement=move, presence=presence,
                       bed_temp_f=bed, room_temp_f=67.0)


# ---- ideal trajectory shape -----------------------------------------------
def test_ideal_trajectory_is_front_loaded_deep_back_loaded_rem():
    traj = IdealTrajectory(deep_total_min=90, rem_total_min=100, est_sleep_min=420)
    # endpoints
    assert traj.deep_by(0) == 0.0 and abs(traj.deep_by(420) - 90) < 1e-6
    assert traj.rem_by(0) == 0.0 and abs(traj.rem_by(420) - 100) < 1e-6
    # by mid-night: MOST deep is already accrued (front-loaded), LESS than half REM (back-loaded)
    assert traj.deep_by(210) > 45            # >50% of deep by the halfway mark
    assert traj.rem_by(210) < 50             # <50% of REM by the halfway mark


# ---- the decision rule -----------------------------------------------------
def _steer(cfg=None):
    return ArchitectureSteering(cfg or AppConfig.default())


def test_deepen_fires_when_light_and_behind_the_deep_curve_and_risk_low():
    s = _steer()
    tgt = targets_for(NightMode.NORMAL)
    d = s.evaluate(minutes_since_onset=30, est_sleep_min=420, deep_min_so_far=2.0,
                   rem_min_so_far=4.0, current_stage=SleepStage.LIGHT, targets=tgt, risk_low=True)
    assert d.maneuver == "deepen" and d.deepen is True
    assert d.deep_deficit_min > 8.0


def test_no_deepen_when_risk_not_low():
    s = _steer()
    tgt = targets_for(NightMode.NORMAL)
    d = s.evaluate(minutes_since_onset=30, est_sleep_min=420, deep_min_so_far=2.0,
                   rem_min_so_far=4.0, current_stage=SleepStage.LIGHT, targets=tgt, risk_low=False)
    assert d.maneuver == "hold"   # maintenance first — never steer into a brewing arousal


def test_no_deepen_when_on_the_deep_curve():
    s = _steer()
    tgt = targets_for(NightMode.NORMAL)
    # plenty of deep already -> no deficit -> hold
    d = s.evaluate(minutes_since_onset=30, est_sleep_min=420, deep_min_so_far=40.0,
                   rem_min_so_far=4.0, current_stage=SleepStage.LIGHT, targets=tgt, risk_low=True)
    assert d.maneuver == "hold" and d.on_deep_curve is True


def test_no_deepen_late_in_the_night():
    s = _steer()
    tgt = targets_for(NightMode.NORMAL)
    # deep is barely steerable late -> even a deficit at 90% of the night does NOT deepen
    d = s.evaluate(minutes_since_onset=400, est_sleep_min=420, deep_min_so_far=10.0,
                   rem_min_so_far=80.0, current_stage=SleepStage.LIGHT, targets=tgt, risk_low=True)
    assert d.maneuver == "hold"


def test_never_pulls_out_of_rem_or_deep_to_chase_deep():
    s = _steer()
    tgt = targets_for(NightMode.NORMAL)
    # In DEEP early we DEFEND (hold), never acquire; in REM early we just hold (REM defend is
    # back-half). Either way we never emit a 'deepen' that would pull you out of a good state.
    deep = s.evaluate(minutes_since_onset=30, est_sleep_min=420, deep_min_so_far=2.0,
                      rem_min_so_far=4.0, current_stage=SleepStage.DEEP, targets=tgt, risk_low=True)
    assert deep.maneuver == "defend_deep" and deep.deepen is False
    rem = s.evaluate(minutes_since_onset=30, est_sleep_min=420, deep_min_so_far=2.0,
                     rem_min_so_far=4.0, current_stage=SleepStage.REM, targets=tgt, risk_low=True)
    assert rem.deepen is False


def test_stands_down_for_the_wakeup_trajectory_near_the_deadline():
    s = _steer()
    tgt = targets_for(NightMode.NORMAL)
    # Same deep deficit + light + risk low, but the wake deadline is close -> DO NOT deepen; hand
    # off to the wake-up ramp so we don't wake you out of freshly-induced deep sleep (inertia).
    near = s.evaluate(minutes_since_onset=30, est_sleep_min=420, deep_min_so_far=2.0,
                      rem_min_so_far=4.0, current_stage=SleepStage.LIGHT, targets=tgt,
                      risk_low=True, minutes_to_wake=40)
    assert near.maneuver == "hold" and "wake" in near.reason
    # plenty of time left -> deepen normally
    far = s.evaluate(minutes_since_onset=30, est_sleep_min=420, deep_min_so_far=2.0,
                     rem_min_so_far=4.0, current_stage=SleepStage.LIGHT, targets=tgt,
                     risk_low=True, minutes_to_wake=300)
    assert far.maneuver == "deepen"


def test_defends_the_favorable_state_you_are_in():
    s = _steer()
    tgt = targets_for(NightMode.NORMAL)
    # In deep -> defend_deep (keep you here).
    deep = s.evaluate(minutes_since_onset=60, est_sleep_min=420, deep_min_so_far=40.0,
                      rem_min_so_far=10.0, current_stage=SleepStage.DEEP, targets=tgt, risk_low=True)
    assert deep.maneuver == "defend_deep"
    # In back-half REM -> defend_rem.
    rem = s.evaluate(minutes_since_onset=300, est_sleep_min=420, deep_min_so_far=80.0,
                     rem_min_so_far=40.0, current_stage=SleepStage.REM, targets=tgt, risk_low=True)
    assert rem.maneuver == "defend_rem"
    # But wake-prevention still outranks defending: a brewing arousal -> hold (settle owns it).
    risky = s.evaluate(minutes_since_onset=60, est_sleep_min=420, deep_min_so_far=40.0,
                       rem_min_so_far=10.0, current_stage=SleepStage.DEEP, targets=tgt,
                       risk_low=False)
    assert risky.maneuver == "hold"


def test_rem_unblock_is_off_by_default_and_gated_on():
    tgt = targets_for(NightMode.NORMAL)
    # back third, deep on-curve, REM behind, light, risk low -> still HOLD while disabled
    off = _steer().evaluate(minutes_since_onset=320, est_sleep_min=420, deep_min_so_far=200.0,
                            rem_min_so_far=1.0, current_stage=SleepStage.LIGHT, targets=tgt,
                            risk_low=True)
    assert off.maneuver == "hold"
    cfg = AppConfig.default()
    cfg.tunables.steer_rem_unblock_enabled = True
    on = _steer(cfg).evaluate(minutes_since_onset=320, est_sleep_min=420, deep_min_so_far=200.0,
                              rem_min_so_far=1.0, current_stage=SleepStage.LIGHT, targets=tgt,
                              risk_low=True)
    assert on.maneuver == "rem_warm"


def test_disabled_steering_always_holds():
    cfg = AppConfig.default()
    cfg.tunables.inight_steering_enabled = False
    d = _steer(cfg).evaluate(minutes_since_onset=30, est_sleep_min=420, deep_min_so_far=2.0,
                             rem_min_so_far=4.0, current_stage=SleepStage.LIGHT,
                             targets=targets_for(NightMode.NORMAL), risk_low=True)
    assert d.maneuver == "hold"


# ---- maintenance wiring ----------------------------------------------------
def test_maintenance_deepen_drives_the_deep_bias_cool():
    cfg = AppConfig.default()
    m = MaintenanceRoutine(cfg)
    t0 = datetime(2026, 6, 24, 1, 30)
    # light + deepen -> drive toward the deep setpoint (cooler -> more deep)
    assert m.step(_frame(t0, SleepStage.LIGHT), NightObjective.OPTIMIZE, deepen=True) \
        is ThermalIntent.DEEP_BIAS_COOL
    # without deepen it just holds steady
    assert m.step(_frame(t0, SleepStage.LIGHT), NightObjective.OPTIMIZE, deepen=False) \
        is ThermalIntent.STABILIZE
    # a power nap must NEVER be deepened (keep it light to avoid grogginess)
    assert m.step(_frame(t0, SleepStage.LIGHT), NightObjective.OPTIMIZE, deepen=True,
                  keep_light=True) is ThermalIntent.STABILIZE
    # PRECEDENCE: wake-prevention outranks deepening — if both fire, settle (don't deepen into a
    # brewing disturbance). Defense-in-depth: the routine enforces it even if a caller passes both.
    assert m.step(_frame(t0, SleepStage.LIGHT), NightObjective.OPTIMIZE, deepen=True,
                  preempt_cool=True) is ThermalIntent.SETTLE_COOL


# ---- controller accrual + veto + edge-triggered logging --------------------
def test_controller_accrues_architecture_and_logs_a_deepen_edge():
    cfg = AppConfig.default()
    ctrl = SleepController(cfg)
    ctrl.set_night_targets(targets_for(NightMode.NORMAL), est_sleep_min=420)
    onset = datetime(2026, 6, 24, 0, 30)
    ctrl._sleep_onset_time = onset
    # accrue ~6 min of LIGHT across ticks
    for i in range(6):
        ctrl._accrue_architecture(onset + timedelta(minutes=i), SleepStage.LIGHT)
    assert ctrl._arch_light_min > 4.0 and ctrl._arch_deep_min == 0.0

    now = onset + timedelta(minutes=30)
    light = _frame(now, SleepStage.LIGHT)
    deepen = ctrl._evaluate_steering(now, light, wake_detected=False, minutes_in_bed=35)
    assert deepen is True
    assert ctrl._deepen_active is True
    assert ctrl.pending_steer_event is not None         # edge-triggered ledger entry
    assert ctrl.pending_steer_event["maneuver"] == "deepen"
    assert ctrl.steering_summary()["active"] is True

    # a second consecutive tick still deepening must NOT re-log (edge-trigger only)
    ctrl.pending_steer_event = None
    ctrl._evaluate_steering(now + timedelta(minutes=1), light, wake_detected=False,
                            minutes_in_bed=36)
    assert ctrl.pending_steer_event is None

    # an awakening vetoes the maneuver
    assert ctrl._evaluate_steering(now + timedelta(minutes=2), light, wake_detected=True,
                                   minutes_in_bed=37) is False
    assert ctrl._deepen_active is False


def test_controller_holds_when_no_targets_set():
    ctrl = SleepController(AppConfig.default())
    onset = datetime(2026, 6, 24, 0, 30)
    ctrl._sleep_onset_time = onset
    now = onset + timedelta(minutes=30)
    assert ctrl._evaluate_steering(now, _frame(now, SleepStage.LIGHT),
                                   wake_detected=False, minutes_in_bed=35) is False


# ---- the steer-event ledger (the deepening-response training signal) -------
def test_steer_event_ledger_resolves_stage_response():
    import os
    import tempfile

    from sleepctl.storage.repository import Repository
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        repo = Repository(path)
        t0 = datetime.now() - timedelta(minutes=40)   # well in the past so the horizon has passed
        repo.log_steer_event("2026-06-23", t0, "deepen", "light",
                             deep_deficit_min=14.0, frac_of_night=0.1, horizon_min=20.0)
        # log a DEEP sample inside the horizon -> the maneuver "deepened" and caused no wake
        repo.log_sample(_frame(t0 + timedelta(minutes=10), SleepStage.DEEP), "deep", False,
                        "2026-06-23")
        n = repo.resolve_steer_events()
        assert n == 1
        eff = repo.steer_efficacy()
        # efficacy is split into the actuated arm vs the shadow/control arm
        assert eff["deepen"]["act"]["n"] == 1 and eff["deepen"]["act"]["deepened"] == 1
        assert eff["deepen"]["act"]["woke"] == 0
    finally:
        os.remove(path)
