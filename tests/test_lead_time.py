"""Lead-time learning: how far ahead of a vulnerable window to start pre-cooling."""

from datetime import datetime

from sleepctl.config import AppConfig
from sleepctl.controller.wake_risk import WakeProfile, WakeRiskAssessor
from sleepctl.learning.lead_time import LeadTimeProfile
from sleepctl.models import SensorFrame, SleepStage


def _f(stage=SleepStage.LIGHT, hr=55, move=0.05, bed=70.0):
    return SensorFrame(timestamp=datetime(2026, 6, 24, 4, 0), stage=stage,
                       stage_confidence=0.8, heart_rate=hr, hrv=60,
                       respiratory_rate=14.0, movement=move, presence=True,
                       bed_temp_f=bed, room_temp_f=67.0)


def test_lead_profile_scales_with_response_lag_and_window():
    fast = LeadTimeProfile.from_lag(6.0)
    slow = LeadTimeProfile.from_lag(18.0)
    # a slower thermal response -> needs to start cooling earlier
    assert slow.lead_for("cycle_boundary") > fast.lead_for("cycle_boundary")
    # gradual circadian window needs more lead than a sudden cycle boundary
    assert fast.lead_for("circadian") > fast.lead_for("cycle_boundary")


def test_next_window_eta_finds_nearest():
    p = WakeProfile.evidence_default()
    # ~10 min before a cycle boundary (80 min into a 90-min cycle)
    eta, wtype = p.next_window_eta(datetime(2026, 6, 24, 2, 0), minutes_since_onset=80)
    assert wtype == "cycle_boundary" and 8 <= eta <= 12


def test_anticipatory_precool_when_window_within_lead():
    cfg = AppConfig.default()
    lead = LeadTimeProfile.from_lag(12.0)  # cycle_boundary lead ~14 min
    a = WakeRiskAssessor(cfg, lead_profile=lead)
    recent = [_f() for _ in range(6)]
    # 80 min into the cycle -> boundary in ~10 min, inside the ~14-min lead -> pre-cool now
    r = a.assess(_f(), recent, datetime(2026, 6, 24, 2, 0), target_temp_f=70.0,
                 sleep_hr_baseline=55, minutes_since_onset=80)
    assert any("anticipatory" in s for s in r.reasons)
    assert r.preempt is True


def test_no_anticipation_without_lead_profile():
    cfg = AppConfig.default()
    a = WakeRiskAssessor(cfg)  # no lead profile attached
    recent = [_f() for _ in range(6)]
    r = a.assess(_f(), recent, datetime(2026, 6, 24, 2, 0), target_temp_f=70.0,
                 sleep_hr_baseline=55, minutes_since_onset=80)
    assert not any("anticipatory" in s for s in r.reasons)
