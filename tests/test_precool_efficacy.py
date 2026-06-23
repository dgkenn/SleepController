"""Closed-loop pre-cool efficacy ledger + latency-aware thermal control."""

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.thermal import ThermalController
from sleepctl.learning.lead_time import build_lead_time_profile
from sleepctl.models import NightObjective, SleepStage, ThermalIntent
from sleepctl.storage.repository import Repository


def _log_wake(repo, ts):
    from sleepctl.models import SensorFrame
    f = SensorFrame(timestamp=ts, stage=SleepStage.AWAKE, heart_rate=64, movement=0.5,
                    bed_temp_f=72.0, room_temp_f=67.0, presence=True, data_age_seconds=10)
    repo.log_sample(f, "maintenance", True, "2026-06-20")


def test_precool_event_resolves_and_efficacy_computes():
    repo = Repository(":memory:")
    base = datetime.now() - timedelta(hours=2)
    # event A: pre-cool, then NO awakening in the window -> prevented
    repo.log_precool_event("2026-06-20", base, "cycle_boundary", 14.0, 10.0)
    # event B: pre-cool, then an awakening occurs in the window -> not prevented
    repo.log_precool_event("2026-06-20", base + timedelta(minutes=40), "cycle_boundary",
                           14.0, 10.0)
    _log_wake(repo, base + timedelta(minutes=45))  # inside event B's window

    resolved = repo.resolve_precool_events()
    assert resolved == 2
    eff = repo.precool_efficacy()["cycle_boundary"]
    assert eff["n"] == 2 and eff["prevented"] == 1 and eff["rate"] == 0.5


def test_low_prevention_lengthens_lead():
    repo = Repository(":memory:")
    base = datetime.now() - timedelta(hours=3)
    # six events at the SAME window, all followed by an awakening -> 0% prevention
    for i in range(6):
        t = base + timedelta(minutes=20 * i)
        repo.log_precool_event("2026-06-20", t, "cycle_boundary", 12.0, 8.0)
        _log_wake(repo, t + timedelta(minutes=5))
    repo.resolve_precool_events()
    prof = build_lead_time_profile(repo)
    # the learner should push the cycle-boundary lead ABOVE the response-lag floor
    assert prof.leads["cycle_boundary"] > prof.response_lag_min
    assert prof.source == "blended"


def test_thermal_loop_is_latency_aware_no_overshoot():
    cfg = AppConfig.default()
    th = ThermalController(cfg)
    th.set_response_lag(12.0)
    t0 = datetime(2026, 6, 24, 1, 0)
    # Cold bed far from a cool target -> first correction is allowed (slew-limited).
    f1, _ = th.resolve(ThermalIntent.DEEP_BIAS_COOL, NightObjective.OPTIMIZE, True,
                       last_target_f=72.0, bed_temp_f=78.0, ambient_temp_f=70.0, now=t0)
    # One minute later (within the 12-min lag) the bed hasn't responded; a fresh full
    # correction would stack -> damping must make the second step much smaller.
    f2, _ = th.resolve(ThermalIntent.DEEP_BIAS_COOL, NightObjective.OPTIMIZE, True,
                       last_target_f=f1, bed_temp_f=78.0, ambient_temp_f=70.0,
                       now=t0 + timedelta(minutes=1))
    step1 = abs(f1 - 72.0)
    step2 = abs(f2 - f1)
    assert step2 < step1  # damped while the previous command is in-flight

    # After the lag elapses, full corrections resume.
    f3, _ = th.resolve(ThermalIntent.DEEP_BIAS_COOL, NightObjective.OPTIMIZE, True,
                       last_target_f=f2, bed_temp_f=78.0, ambient_temp_f=70.0,
                       now=t0 + timedelta(minutes=15))
    assert abs(f3 - f2) > step2
