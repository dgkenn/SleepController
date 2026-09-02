"""The wake-risk score, split into what the sleeper is doing and what the clock says.

Measured on 2026-08-29: `light_stage` fired on 99% of pre-empting maintenance ticks, and
`circadian_nadir` and `back_half_of_night` on 42% each. The arithmetic was the problem -- the
clock terms summed to 0.60 against a 0.5 threshold, so timing alone was sufficient, while the
two strongest evidence terms both need a bed temperature this account never reports.
"""

from datetime import datetime

from sleepctl.config import AppConfig
from sleepctl.controller.wake_risk import WakeRiskAssessor
from sleepctl.models import SensorFrame, SleepStage


def _frame(hr=None, movement=None, stage=SleepStage.LIGHT, rr=None):
    return SensorFrame(timestamp=datetime(2026, 8, 30, 3, 0), heart_rate=hr,
                       movement=movement, stage=stage, respiratory_rate=rr)


def _assessor():
    return WakeRiskAssessor(AppConfig())


class _AlwaysVulnerable:
    """A profile for which every clock-based reason is true at once."""
    warm_temp_threshold_f = None

    def near_recurring_time(self, now):        return True
    def near_cycle_boundary(self, mso):        return True
    def in_back_half(self, mso):               return True
    def in_circadian_danger_zone(self, now):   return True
    def next_window_eta(self, now, mso):       return (None, None)


def test_the_clock_alone_can_no_longer_trigger_a_preempt():
    a = _assessor()
    a.profile = _AlwaysVulnerable()
    now = datetime(2026, 8, 30, 3, 0)
    risk = a.assess(_frame(), [], now, minutes_since_onset=240.0)
    # Every timing reason fires...
    for r in ("light_stage", "recurring_wake_window", "cycle_boundary",
              "back_half_of_night", "circadian_nadir"):
        assert r in risk.reasons
    # ...and together they still cannot reach the threshold.
    assert risk.evidence_score == 0.0
    assert risk.context_score <= AppConfig().tunables.wake_risk_context_cap + 1e-9
    assert risk.preempt is False


def test_real_evidence_plus_a_vulnerable_window_does_trigger():
    """The intended behaviour is preserved: timing MODULATES a real signal over the line."""
    a = _assessor()
    a.profile = _AlwaysVulnerable()
    now = datetime(2026, 8, 30, 3, 0)
    risk = a.assess(_frame(hr=80.0), [], now, sleep_hr_baseline=68.0,
                    minutes_since_onset=240.0)
    assert "hr_creep" in risk.reasons
    assert risk.evidence_score > 0
    assert risk.preempt is True


def test_context_is_capped_not_merely_summed():
    a = _assessor()
    a.profile = _AlwaysVulnerable()
    risk = a.assess(_frame(), [], datetime(2026, 8, 30, 3, 0), minutes_since_onset=240.0)
    # 0.10 + 0.20 + 0.10 + 0.08 + 0.12 = 0.60 raw, before the cap.
    assert risk.score <= AppConfig().tunables.wake_risk_context_cap + 1e-9


def test_deep_sleep_is_never_pre_empted():
    a = _assessor()
    a.profile = _AlwaysVulnerable()
    risk = a.assess(_frame(hr=90.0, stage=SleepStage.DEEP), [],
                    datetime(2026, 8, 30, 3, 0), sleep_hr_baseline=68.0,
                    minutes_since_onset=240.0)
    assert risk.preempt is False


def test_the_anticipatory_pre_cool_is_still_exempt():
    """It exists to act BEFORE there is evidence; its bound is a duty cycle, not a threshold."""
    class _Anticipatory(_AlwaysVulnerable):
        def next_window_eta(self, now, mso):   return (10.0, "recurring")

    class _Lead:
        def lead_for(self, wtype):             return 30.0

    a = _assessor()
    a.profile = _Anticipatory()
    a.lead_profile = _Lead()
    risk = a.assess(_frame(), [], datetime(2026, 8, 30, 3, 0), minutes_since_onset=240.0)
    assert risk.anticipatory is True
    assert risk.preempt is True
    assert risk.evidence_score == 0.0


def test_movement_counts_as_evidence():
    a = _assessor()
    a.profile = _AlwaysVulnerable()
    risk = a.assess(_frame(movement=0.9), [], datetime(2026, 8, 30, 3, 0),
                    minutes_since_onset=240.0)
    assert "restless" in risk.reasons
    assert risk.preempt is True
