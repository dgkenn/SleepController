"""Phase 2 learners: deepening + lightening causal response (n-of-1 control), the wake-causation
failure-mode audit (base-rate controlled, confounder-aware), and the personalized awakening-precursor
trajectory learner — plus the controller's shadow logging and the precursor detector personalization."""

import os
import tempfile
from datetime import datetime, timedelta

from sleepctl.benchmarks import NightMode, targets_for
from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.controller.precursor import PrecursorDetector
from sleepctl.learning.deepening import (
    learn_deepening, learn_lightening, next_steer_mode)
from sleepctl.learning.wake_causation import (
    awakening_precursor_profile, wake_causation_audit, suspect_maneuvers)
from sleepctl.models import (
    ControllerState, CorrectionAction, Intervention, NightSummary, SensorFrame, SleepStage)
from sleepctl.storage.repository import Repository


def _recs(n_act, act_succ, act_wake, n_ctrl, ctrl_succ, ctrl_wake, key="deepened", nt="normal"):
    out = []
    for i in range(n_act):
        out.append({"applied": 1, key: 1 if i < act_succ else 0,
                    "caused_wake": 1 if i < act_wake else 0, "night_type": nt})
    for i in range(n_ctrl):
        out.append({"applied": 0, key: 1 if i < ctrl_succ else 0,
                    "caused_wake": 1 if i < ctrl_wake else 0, "night_type": nt})
    return out


# ---- deepening-response causal policy --------------------------------------
def test_deepening_learns_it_works_and_keeps_it():
    p = learn_deepening(_recs(10, 8, 0, 8, 3, 0))
    assert p.enabled and p.is_personalized and p.lift > 0.3


def test_deepening_disables_when_it_wakes_you():
    # Same deep lift, but actuating wakes you 4/10 vs 0/8 control -> do-no-harm DISABLE.
    p = learn_deepening(_recs(10, 8, 4, 8, 3, 0))
    assert p.enabled is False and "awakening" in p.rationale


def test_deepening_disables_when_no_lift_over_baseline():
    # Cooling deepens 4/10 but you reach deep 4/8 (50%) naturally anyway -> no causal benefit.
    p = learn_deepening(_recs(10, 4, 0, 8, 4, 0))
    assert p.enabled is False and "doesn't beat" in p.rationale


def test_deepening_thin_data_keeps_evidence_default_on():
    p = learn_deepening(_recs(3, 2, 0, 2, 1, 0))
    assert p.enabled is True and p.is_personalized is False


def test_exploration_schedules_control_nights():
    pol = learn_deepening(_recs(10, 8, 0, 8, 3, 0))      # enabled, low confidence
    modes = [next_steer_mode(pol, i) for i in range(8)]
    assert "observe" in modes and "act" in modes
    # a disabled policy keeps observing (gather the base rate), never actuates
    dis = learn_deepening(_recs(10, 8, 4, 8, 3, 0))
    assert all(next_steer_mode(dis, i) == "observe" for i in range(8))


# ---- lightening-response (symmetric) ---------------------------------------
def test_lightening_uses_the_same_causal_core_on_rem_target():
    p = learn_lightening(_recs(10, 8, 0, 8, 3, 0, key="succeeded"))
    assert p.enabled and "REM" in p.rationale


# ---- controller shadow logging (the n-of-1 control arm) --------------------
def _frame(ts, stage, presence=True):
    return SensorFrame(timestamp=ts, stage=stage, stage_confidence=0.85, heart_rate=52, hrv=60,
                       respiratory_rate=14.0, movement=0.04, presence=presence, bed_temp_f=70.0,
                       room_temp_f=67.0, data_age_seconds=0)


def test_controller_observe_night_logs_shadow_and_does_not_cool():
    ctrl = SleepController(AppConfig.default())
    ctrl.set_night_targets(targets_for(NightMode.NORMAL), est_sleep_min=420)
    ctrl.set_steer_policy(actuate=False)                 # a control/observe night
    onset = datetime(2026, 6, 24, 0, 30)
    ctrl._sleep_onset_time = onset
    now = onset + timedelta(minutes=30)
    out = ctrl._evaluate_steering(now, _frame(now, SleepStage.LIGHT),
                                  wake_detected=False, minutes_in_bed=35)
    assert out is False                                  # did NOT actuate (no cooling)
    assert ctrl.pending_steer_event is not None
    assert ctrl.pending_steer_event["applied"] == 0      # logged as the control arm
    summ = ctrl.steering_summary()
    assert summ["observing"] is True and summ["active"] is False


# ---- wake-causation failure-mode audit -------------------------------------
def _seed_audit_db(path):
    repo = Repository(path)
    nd = "2026-06-20"
    repo.save_night_summary(NightSummary(date=nd, total_sleep_min=420))
    t = datetime(2026, 6, 20, 2, 0)
    for i in range(120):
        wake = i in (40, 80)
        stage = SleepStage.AWAKE if wake else (SleepStage.DEEP if i % 3 == 0 else SleepStage.LIGHT)
        hr = 66 if wake else 52
        move = 0.5 if wake else 0.05
        # build a 6-min HR/move creep before each awakening (the precursor signal)
        for wk in (40, 80):
            if wk - 6 <= i < wk:
                hr = 52 + (i - (wk - 6)) * 2
                move = 0.05 + (i - (wk - 6)) * 0.05
        repo.log_sample(_frame(t, stage), "maintenance", wake, nd)
        # overwrite the just-logged sample's hr/move via a fresh frame is simpler: re-log not needed
        if i == 38:
            repo.log_intervention(Intervention(
                timestamp=t, state=ControllerState.MAINTENANCE, action=CorrectionAction.COOLER,
                magnitude_f=1.0, reason="maintenance -> deep_bias_cool"), nd)
        t += timedelta(minutes=1)
    return repo


def test_wake_audit_computes_base_rate_and_flags_only_proactive():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _seed_audit_db(path)
        repo = Repository(path)
        audit = wake_causation_audit(repo, horizon_min=10, nights=5)
        assert audit["base_wake_rate"] is not None         # controls for "would've woken anyway"
        # a reactive settle would be confounded; here the cooling adjustment is proactive
        assert "deep_bias_cool" in audit["maneuvers"]
        # suspect set only ever contains non-confounded maneuvers
        for k in suspect_maneuvers(repo, horizon_min=10, nights=5):
            assert audit["maneuvers"][k]["confounded"] is False
    finally:
        os.remove(path)


def test_settle_is_labelled_confounded_not_blamed():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        repo = Repository(path)
        nd = "2026-06-21"
        repo.save_night_summary(NightSummary(date=nd, total_sleep_min=420))
        t = datetime(2026, 6, 21, 2, 0)
        for i in range(60):
            wake = (i % 10 == 9)                          # a wake every 10 min
            repo.log_sample(_frame(t, SleepStage.AWAKE if wake else SleepStage.LIGHT),
                            "maintenance", wake, nd)
            if i % 10 == 8:                               # a settle fires right before each wake
                repo.log_intervention(Intervention(
                    timestamp=t, state=ControllerState.MAINTENANCE, action=CorrectionAction.COOLER,
                    magnitude_f=0.5, reason="maintenance -> settle_cool"), nd)
            t += timedelta(minutes=1)
        audit = wake_causation_audit(repo, horizon_min=5, nights=5)
        assert audit["maneuvers"]["settle_cool"]["confounded"] is True
        assert audit["maneuvers"]["settle_cool"]["suspect"] is False   # never auto-blamed
    finally:
        os.remove(path)


# ---- personalized awakening-precursor trajectory ---------------------------
def test_precursor_profile_learns_predictive_signals():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        repo = Repository(path)
        nd = "2026-06-22"
        repo.save_night_summary(NightSummary(date=nd, total_sleep_min=420))
        t = datetime(2026, 6, 22, 1, 0)
        for i in range(160):
            wake = i in (40, 80, 120)
            hr, hrv, move = 52, 60, 0.05
            for wk in (40, 80, 120):
                if wk - 6 <= i < wk:                      # HR up, HRV down, movement up before wake
                    k = i - (wk - 6)
                    hr, hrv, move = 52 + k * 2, 60 - k * 3, 0.05 + k * 0.05
                if i == wk:
                    hr, move = 66, 0.5
            f = SensorFrame(timestamp=t, stage=SleepStage.AWAKE if wake else SleepStage.LIGHT,
                            stage_confidence=0.8, heart_rate=hr, hrv=hrv, respiratory_rate=14,
                            movement=move, presence=True, bed_temp_f=70, room_temp_f=67,
                            data_age_seconds=0)
            repo.log_sample(f, "maintenance", wake, nd)
            t += timedelta(minutes=1)
        prof = awakening_precursor_profile(repo, lead_min=6, nights=5, min_events=2)
        assert prof["n_awakenings"] >= 2
        assert "hr_slope" in prof["predictive_signals"]
        assert "hrv_slope" in prof["predictive_signals"]
        # comprehensive movement / tossing-turning features are present and predictive here
        assert "move_slope" in prof["features"] and "move_burst" in prof["features"]
        assert "move_slope" in prof["predictive_signals"]
        assert prof["is_personalized"] is True
        # and the detector actually tunes its HR / HRV / restlessness triggers to YOU
        det = PrecursorDetector(AppConfig.default())
        base_move = det.move_rise_slope
        det.personalize(prof)
        assert det.personalized is True
        assert det.move_rise_slope != base_move        # tossing-turning trigger personalized
    finally:
        os.remove(path)


def test_precursor_feature_taxonomy_is_comprehensive():
    # the learner must expose the full autonomic + movement + thermal feature set
    from sleepctl.learning.wake_causation import _PRECURSOR_FEATURES
    for f in ("hr_slope", "hr_mean", "hrv_slope", "hrv_mean", "rr_slope", "rr_cv",
              "move_mean", "move_slope", "move_max", "move_burst", "bed_slope", "bed_mean"):
        assert f in _PRECURSOR_FEATURES


def test_detector_ignores_an_unpersonalized_profile():
    det = PrecursorDetector(AppConfig.default())
    before = (det.hr_creep_slope, det.hrv_decay_slope)
    det.personalize({"is_personalized": False, "features": {}})
    assert (det.hr_creep_slope, det.hrv_decay_slope) == before and det.personalized is False
