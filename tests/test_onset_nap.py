"""On-demand onset induction (warm->cool cascade) + nap strategy."""

from datetime import datetime

from sleepctl.config import AppConfig
from sleepctl.controller.induction import InductionRoutine
from sleepctl.controller.maintenance import MaintenanceRoutine
from sleepctl.controller.nap import NapStrategy, nap_strategy
from sleepctl.controller.thermal import ThermalController
from sleepctl.models import NightObjective, SensorFrame, SleepStage, ThermalIntent


def _frame(stage=SleepStage.AWAKE):
    return SensorFrame(timestamp=datetime(2026, 6, 24, 23, 0), stage=stage,
                       bed_temp_f=72.0, room_temp_f=68.0, presence=True, data_age_seconds=5)


# ---- onset induction: warm -> cool (warming speeds onset; never opens cold) --
def test_induction_warm_then_cool():
    cfg = AppConfig.default()
    ind = InductionRoutine(cfg)
    warm_min = cfg.tunables.induction_warm_pulse_min
    # opener -> gentle warm nudge (cutaneous warming speeds onset), NOT a cold blast
    assert ind.step(_frame(), NightObjective.OPTIMIZE, minutes_in_bed=1) \
        is ThermalIntent.ONSET_WARM
    # after the warm nudge -> consolidate cool
    assert ind.step(_frame(), NightObjective.OPTIMIZE,
                    minutes_in_bed=warm_min + 1) is ThermalIntent.INDUCTION_COOL


def test_induction_never_opens_cold():
    """Regression: pressing 'help me fall asleep' must not freeze the user with a cold opener."""
    cfg = AppConfig.default()
    ind = InductionRoutine(cfg)
    for m in (0.1, 1, 3, 5):
        assert ind.step(_frame(), NightObjective.OPTIMIZE, minutes_in_bed=m) \
            is not ThermalIntent.ONSET_COLD_SETTLE


def test_induction_without_warm_pulse_is_cool_only():
    cfg = AppConfig.default()
    ind = InductionRoutine(cfg)
    ind.set_warm_pulse_arm(False)
    # with the warm pulse disarmed the cascade cools from the very start (no cold opener either)
    assert ind.step(_frame(), NightObjective.OPTIMIZE, minutes_in_bed=1) \
        is ThermalIntent.INDUCTION_COOL


def test_induction_damage_control_compresses_the_warm_opener():
    cfg = AppConfig.default()
    ind = InductionRoutine(cfg)
    warm_min = cfg.tunables.induction_warm_pulse_min
    # On a short night the warm opener halves, so where a full night is still warming the short
    # night has already moved on to the deepening cool.
    mid = warm_min / 2.0 + 0.5
    assert ind.step(_frame(), NightObjective.DAMAGE_CONTROL, minutes_in_bed=mid) \
        is ThermalIntent.INDUCTION_COOL
    assert ind.step(_frame(), NightObjective.OPTIMIZE, minutes_in_bed=mid) \
        is ThermalIntent.ONSET_WARM


def test_onset_warm_target_is_above_neutral_and_capped():
    cfg = AppConfig.default()
    th = ThermalController(cfg)
    warm = th.target_for(ThermalIntent.ONSET_WARM, NightObjective.OPTIMIZE,
                         hot_sleeper=True, last_target_f=70.0)
    neutral = th.target_for(ThermalIntent.NEUTRAL, NightObjective.OPTIMIZE,
                            hot_sleeper=True, last_target_f=70.0)
    assert warm > neutral  # it's a WARM nudge despite the hot-sleeper cool bias
    # bounded by the comfort cap above the *uncapped* neutral
    assert warm <= cfg.tunables.neutral_temp_f + cfg.tunables.onset_warm_comfort_cap_f + 0.01


def test_cold_settle_target_is_really_cold_and_follows_override():
    cfg = AppConfig.default()
    th = ThermalController(cfg)
    cold = th.target_for(ThermalIntent.ONSET_COLD_SETTLE, NightObjective.OPTIMIZE,
                         hot_sleeper=True, last_target_f=70.0)
    neutral = th.target_for(ThermalIntent.NEUTRAL, NightObjective.OPTIMIZE,
                            hot_sleeper=True, last_target_f=70.0)
    # It tracks the profile's really-cold opener target (no extra hot-sleeper cool bias applied).
    assert abs(cold - th.profile.onset_cold_settle_f) < 0.01
    assert cold < neutral - 3.0    # clearly, genuinely cold
    # The learned override drives the target.
    th.set_onset_cold_settle(58.0)
    cold2 = th.target_for(ThermalIntent.ONSET_COLD_SETTLE, NightObjective.OPTIMIZE,
                          hot_sleeper=True, last_target_f=70.0)
    assert abs(cold2 - 58.0) < 0.01


# ---- power-nap keeps the bed light ---------------------------------------
def test_power_nap_keeps_light_no_deep_cooling():
    cfg = AppConfig.default()
    m = MaintenanceRoutine(cfg)
    # normally deep sleep -> deep-bias cool; in keep_light it must NOT drive deep cooling
    assert m.step(_frame(SleepStage.DEEP), NightObjective.OPTIMIZE) is ThermalIntent.DEEP_BIAS_COOL
    assert m.step(_frame(SleepStage.DEEP), NightObjective.OPTIMIZE, keep_light=True) \
        is ThermalIntent.STABILIZE


# ---- nap strategy selection (literature-backed) --------------------------
def test_nap_strategy_power_cycle_trap():
    cfg = AppConfig.default()
    assert nap_strategy(20, now_hour=14, cfg=cfg).strategy is NapStrategy.POWER
    assert nap_strategy(90, now_hour=14, cfg=cfg).strategy is NapStrategy.CYCLE
    assert nap_strategy(45, now_hour=14, cfg=cfg).strategy is NapStrategy.TRAP


def test_power_nap_keeps_light_and_caps():
    p = nap_strategy(20, now_hour=14)
    assert p.keep_light is True and p.target_sleep_min == 20


def test_cycle_nap_targets_one_cycle_and_buffers_inertia():
    p = nap_strategy(100, now_hour=14)
    assert p.strategy is NapStrategy.CYCLE
    assert p.target_sleep_min == 90 and p.keep_light is False
    assert p.inertia_buffer_min >= 15  # advise a buffer before anything critical


def test_late_day_nap_flagged():
    early = nap_strategy(20, now_hour=13)
    late = nap_strategy(20, now_hour=18)
    assert early.late_day is False and late.late_day is True
    assert "tonight" in late.advice.lower()


# ------------------------------------- onset needs a TRANSITION, not just a compatible state
def _awake_in_bed_frame(now, hr=76.0, hrv=20.0):
    """Someone lying awake and still: the exact physiology measured 2026-08-06."""
    from sleepctl.models import SensorFrame, SleepStage
    return SensorFrame(timestamp=now, stage=SleepStage.LIGHT, stage_confidence=0.7,
                       presence=True, heart_rate=hr, hrv=hrv, movement=0.02)


def test_lying_awake_and_still_does_not_confirm_onset():
    """The failure this guards: the two most easily satisfied signals -- ``asleep_stage`` (the
    stager's LIGHT label, itself largely derived from a quiet HR) and ``stillness`` -- are
    precisely the pair quiet wakefulness produces. The user lay awake in bed for an hour with a
    DEAD FLAT heart rate (77,76,75,76,76,75) and onset was 'confirmed' at 21:41.
    """
    from datetime import datetime, timedelta
    from sleepctl.config import AppConfig
    from sleepctl.controller.sleep_onset import SleepOnsetDetector

    cfg = AppConfig.default()
    det = SleepOnsetDetector(cfg)
    now = datetime(2026, 8, 6, 21, 20)
    recent = []
    confirmed = None
    for i in range(45):                      # 45 min of awake-in-bed, no trend in any signal
        f = _awake_in_bed_frame(now)
        confirmed = det.evaluate(f, recent, now, bed_entry_time=datetime(2026, 8, 6, 21, 20))
        recent.append(f)
        now += timedelta(minutes=1)
    assert confirmed is None, "confirmed onset on someone lying awake and still"


def test_a_real_onset_still_confirms():
    """The guard must not block genuine onset: a sustained HR decline is a transition signal."""
    from datetime import datetime, timedelta
    from sleepctl.config import AppConfig
    from sleepctl.controller.sleep_onset import SleepOnsetDetector

    cfg = AppConfig.default()
    det = SleepOnsetDetector(cfg)
    now = datetime(2026, 8, 6, 23, 0)
    recent, confirmed = [], None
    for i in range(45):
        hr = max(64.0, 78.0 - i * 0.8)       # a real, progressive onset decline
        f = _awake_in_bed_frame(now, hr=hr, hrv=20.0 + i * 0.6)
        confirmed = det.evaluate(f, recent, now, bed_entry_time=datetime(2026, 8, 6, 23, 0))
        recent.append(f)
        now += timedelta(minutes=1)
        if confirmed:
            break
    assert confirmed is not None, "a genuine onset with a clear HR decline failed to confirm"
    assert any(s in SleepOnsetDetector.TRANSITION_SIGNALS for s in confirmed.signals)
