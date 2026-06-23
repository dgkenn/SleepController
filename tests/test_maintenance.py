"""Sleep-maintenance subsystem: prevent (wake-risk), detect (graded arousal), handle."""

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.arousal import ArousalDetector, ArousalLevel
from sleepctl.controller.maintenance import MaintenanceRoutine, WakeRecoveryRoutine
from sleepctl.controller.wake_risk import WakeProfile, WakeRiskAssessor
from sleepctl.ml.reward import reward_from_outcomes
from sleepctl.models import NightObjective, SensorFrame, SleepStage, ThermalIntent


def _f(ts, stage, hr=55, move=0.05, rr=14.0, hrv=60, presence=True, bed=70.0):
    return SensorFrame(timestamp=ts, stage=stage, stage_confidence=0.8, heart_rate=hr,
                       hrv=hrv, respiratory_rate=rr, movement=move, presence=presence,
                       bed_temp_f=bed, room_temp_f=67.0)


# ---- prevention: wake-risk assessor ---------------------------------------
def test_wake_risk_rises_on_precursors_and_warmth():
    cfg = AppConfig.default()
    a = WakeRiskAssessor(cfg)
    t0 = datetime(2026, 6, 24, 3, 0)
    recent = [_f(t0 - timedelta(minutes=i), SleepStage.LIGHT, hr=54) for i in range(10)][::-1]
    calm = a.assess(_f(t0, SleepStage.LIGHT, hr=54, move=0.05, bed=70.0),
                    recent, t0, target_temp_f=70.0, sleep_hr_baseline=54)
    # HR creeping, restless, and running warm vs target -> high risk + pre-empt
    hot = a.assess(_f(t0, SleepStage.LIGHT, hr=60, move=0.35, bed=72.0),
                   recent, t0, target_temp_f=70.0, sleep_hr_baseline=54)
    assert hot.score > calm.score
    assert hot.preempt is True


def test_wake_risk_never_preempts_in_deep_sleep():
    cfg = AppConfig.default()
    a = WakeRiskAssessor(cfg)
    t0 = datetime(2026, 6, 24, 2, 0)
    recent = [_f(t0, SleepStage.DEEP, hr=52) for _ in range(8)]
    r = a.assess(_f(t0, SleepStage.DEEP, hr=62, move=0.5, bed=73.0), recent, t0,
                 target_temp_f=70.0, sleep_hr_baseline=52)
    assert r.preempt is False  # protect deep sleep — never jolt it


def test_wake_profile_recurring_time_window():
    prof = WakeProfile(awakening_minutes=[180])  # 03:00
    assert prof.near_recurring_time(datetime(2026, 6, 24, 3, 10)) is True
    assert prof.near_recurring_time(datetime(2026, 6, 24, 6, 0)) is False


def test_evidence_preset_seeds_structural_vulnerabilities():
    p = WakeProfile.evidence_default()
    assert p.source == "preset" and p.awakening_minutes == []
    # circadian core-temp nadir zone (~3:30-5:30 a.m.)
    assert p.in_circadian_danger_zone(datetime(2026, 6, 24, 4, 30)) is True
    assert p.in_circadian_danger_zone(datetime(2026, 6, 24, 1, 0)) is False
    # cycle boundary near ~90 min after onset
    assert p.near_cycle_boundary(92) is True
    assert p.near_cycle_boundary(45) is False
    # back half after ~3 cycles
    assert p.in_back_half(300) is True and p.in_back_half(60) is False


def test_circadian_and_cycle_raise_risk_even_without_personal_history():
    cfg = AppConfig.default()
    a = WakeRiskAssessor(cfg)  # uses the evidence preset by default
    recent = [_f(datetime(2026, 6, 24, 4, 30), SleepStage.REM, hr=56) for _ in range(8)]
    # 4:30 a.m. (circadian zone), ~back-half, near a cycle boundary, running a bit warm
    r = a.assess(_f(datetime(2026, 6, 24, 4, 30), SleepStage.REM, hr=58, bed=71.6),
                 recent, datetime(2026, 6, 24, 4, 30), target_temp_f=70.0,
                 sleep_hr_baseline=55, minutes_since_onset=300)
    assert "circadian_nadir" in r.reasons and "back_half_of_night" in r.reasons


# ---- detection: graded arousal --------------------------------------------
def test_arousal_grades_micro_vs_awakening_vs_bed_exit():
    cfg = AppConfig.default()
    det = ArousalDetector(cfg)
    t0 = datetime(2026, 6, 24, 2, 30)
    recent = [_f(t0 - timedelta(minutes=i), SleepStage.DEEP, hr=52, move=0.03) for i in range(10)][::-1]
    # calm deep sleep -> none
    calm = det.assess(_f(t0, SleepStage.DEEP, hr=52, move=0.03), recent, t0,
                      sleep_hr_baseline=52, sleep_hrv_baseline=60)
    assert calm.level is ArousalLevel.NONE
    # a single HR surge + movement -> micro (transient)
    det.reset()
    micro = det.assess(_f(t0, SleepStage.LIGHT, hr=62, move=0.45), recent, t0,
                       sleep_hr_baseline=52, sleep_hrv_baseline=60)
    assert micro.level in (ArousalLevel.MICRO, ArousalLevel.AWAKENING)
    # sustained AWAKE -> awakening
    det.reset()
    res = None
    for i in range(5):
        res = det.assess(_f(t0 + timedelta(minutes=i), SleepStage.AWAKE, hr=64, move=0.5),
                         recent, t0 + timedelta(minutes=i),
                         sleep_hr_baseline=52, sleep_hrv_baseline=60)
    assert res.level is ArousalLevel.AWAKENING
    # bed exit
    exit_ = det.assess(_f(t0, SleepStage.AWAKE, presence=False), recent, t0)
    assert exit_.level is ArousalLevel.OUT_OF_BED


# ---- handling: routines ----------------------------------------------------
def test_maintenance_preempts_with_cooling_in_light_not_deep():
    cfg = AppConfig.default()
    m = MaintenanceRoutine(cfg)
    t0 = datetime(2026, 6, 24, 3, 0)
    # light sleep + preempt -> settle cool; without -> stabilize
    assert m.step(_f(t0, SleepStage.LIGHT), NightObjective.OPTIMIZE, preempt_cool=True) \
        is ThermalIntent.SETTLE_COOL
    assert m.step(_f(t0, SleepStage.LIGHT), NightObjective.OPTIMIZE, preempt_cool=False) \
        is ThermalIntent.STABILIZE
    # deep sleep is never disturbed by a pre-empt; it keeps the deep-bias cool
    assert m.step(_f(t0, SleepStage.DEEP), NightObjective.OPTIMIZE, preempt_cool=True) \
        is ThermalIntent.DEEP_BIAS_COOL


def test_recovery_actively_cools_to_resettle_then_holds():
    cfg = AppConfig.default()
    r = WakeRecoveryRoutine(cfg)
    t0 = datetime(2026, 6, 24, 3, 5)
    assert r.step(_f(t0, SleepStage.AWAKE)) is ThermalIntent.SETTLE_COOL
    assert r.step(_f(t0, SleepStage.DEEP)) is ThermalIntent.STABILIZE


# ---- ML: reward penalises slow re-settling --------------------------------
def test_reward_penalises_resettle_latency():
    cfg = AppConfig.default()
    base = {"wake_events": 1, "deep_pct": 0.2, "sleep_efficiency": 0.9}
    fast = reward_from_outcomes({**base, "resettle_latency_min": 3}, cfg)
    slow = reward_from_outcomes({**base, "resettle_latency_min": 25}, cfg)
    assert fast > slow
