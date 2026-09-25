"""Structural constraints on the hypnogram, from the nights that needed them.

    2026-08-29    0.0% REM
    2026-08-30   69.0% REM, 0.4% deep -- REM/AWAKE flipping every 1-2 minutes for hours
    2026-08-31   16.3% REM, 14.2% deep, with DEEP scored 2 minutes after bed entry
"""

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.hypnogram import (HypnogramConstraint, architecture_plausible,
                                           constrain)
from sleepctl.models import SleepStage

T0 = datetime(2026, 8, 30, 21, 37)


def _hc():
    return HypnogramConstraint()


def test_rem_before_sleep_onset_is_not_rem():
    """2026-08-30 scored REM from 22:13 against a sleep-onset latency of 77.7 minutes."""
    v = _hc().apply(SleepStage.REM, 0.7, T0, AppConfig(), sleep_onset_time=None)
    assert v.stage is SleepStage.LIGHT
    assert v.reason == "before_sleep_onset"


def test_deep_two_minutes_after_onset_is_not_deep():
    """2026-08-31: bed entry 21:24, DEEP at 21:26."""
    v = _hc().apply(SleepStage.DEEP, 0.6, T0 + timedelta(minutes=2), AppConfig(),
                    sleep_onset_time=T0)
    assert v.stage is SleepStage.LIGHT
    assert v.reason == "deep_too_early_after_onset"


def test_rem_at_a_plausible_latency_is_left_alone():
    v = _hc().apply(SleepStage.REM, 0.7, T0 + timedelta(minutes=95), AppConfig(),
                    sleep_onset_time=T0)
    assert v.stage is SleepStage.REM
    assert v.reason is None
    assert v.confidence == 0.7


def _awake(hc, cfg, start, minutes):
    """Adopt AWAKE on every 30 s tick for ``minutes``; returns the last awake tick."""
    t = start
    for k in range(int(minutes * 2) + 1):
        t = start + timedelta(seconds=30 * k)
        hc.observe(SleepStage.AWAKE, t, cfg)
    return t


def test_sleep_does_not_resume_in_rem_after_an_awakening():
    """The rule that ends the R A R A oscillation: re-entry runs through light sleep."""
    hc, cfg = _hc(), AppConfig()
    t = _awake(hc, cfg, T0 + timedelta(minutes=120), 3)
    v = hc.apply(SleepStage.REM, 0.7, t + timedelta(minutes=1), cfg, sleep_onset_time=T0)
    assert v.stage is SleepStage.LIGHT
    assert v.reason == "no_light_sleep_since_awakening"


def test_rem_is_allowed_again_once_light_sleep_has_been_re_established():
    hc, cfg = _hc(), AppConfig()
    t = _awake(hc, cfg, T0 + timedelta(minutes=120), 3)
    hc.observe(SleepStage.LIGHT, t + timedelta(minutes=1), cfg)
    late = t + timedelta(minutes=1 + cfg.tunables.reentry_light_min + 1)
    assert hc.apply(SleepStage.REM, 0.7, late, cfg, T0).stage is SleepStage.REM


def test_a_lone_movement_burst_is_not_an_awakening():
    """The accelerometer override reads a movement burst as ~3 AWAKE ticks. Treating each as an
    awakening relabelled 4,507 EEG-scored REM ticks on held-out BIDSleep nights."""
    hc, cfg = _hc(), AppConfig()
    t = _awake(hc, cfg, T0 + timedelta(minutes=120), 1)
    v = hc.apply(SleepStage.REM, 0.7, t + timedelta(seconds=30), cfg, sleep_onset_time=T0)
    assert v.stage is SleepStage.REM and v.reason is None


def test_brief_awakenings_that_keep_coming_back_still_count():
    """2026-08-30: REM/AWAKE every 1-2 minutes for hours. A brief AWAKE that follows another
    within minutes is the oscillation this rule exists for, not a lone movement."""
    hc, cfg = _hc(), AppConfig()
    t = _awake(hc, cfg, T0 + timedelta(minutes=120), 1)
    hc.observe(SleepStage.REM, t + timedelta(seconds=30), cfg)
    t = _awake(hc, cfg, t + timedelta(minutes=2), 0.5)
    v = hc.apply(SleepStage.REM, 0.7, t + timedelta(seconds=30), cfg, sleep_onset_time=T0)
    assert v.stage is SleepStage.LIGHT
    assert v.reason == "no_light_sleep_since_awakening"


def test_every_awake_tick_counts_without_a_config():
    """Callers that pass no config keep the strict rule."""
    hc, cfg = _hc(), AppConfig()
    hc.observe(SleepStage.AWAKE, T0 + timedelta(minutes=120))
    v = hc.apply(SleepStage.REM, 0.7, T0 + timedelta(minutes=121), cfg, sleep_onset_time=T0)
    assert v.stage is SleepStage.LIGHT


def _asleep(hc, cfg, start, minutes, stage=SleepStage.LIGHT):
    t = start
    for k in range(int(minutes * 2) + 1):
        t = start + timedelta(seconds=30 * k)
        hc.observe(stage, t, cfg)
    return t


def test_deep_is_allowed_on_a_held_sleep_run_before_the_detector_confirms():
    """Waiting for the onset detector erased 2,415 held-out ticks that both the EEG and the
    stager called deep: first-cycle N3 arrives while the detector is still deliberating."""
    hc, cfg = _hc(), AppConfig()
    t = _asleep(hc, cfg, T0, cfg.tunables.provisional_onset_min + 1)
    v = hc.apply(SleepStage.DEEP, 0.6, t + timedelta(seconds=30), cfg, sleep_onset_time=None)
    assert v.stage is SleepStage.DEEP and v.reason is None


def test_deep_still_waits_for_a_held_sleep_run():
    hc, cfg = _hc(), AppConfig()
    t = _asleep(hc, cfg, T0, 3)
    v = hc.apply(SleepStage.DEEP, 0.6, t + timedelta(seconds=30), cfg, sleep_onset_time=None)
    assert v.stage is SleepStage.LIGHT and v.reason == "before_sleep_onset"


def test_rem_counts_its_latency_from_the_provisional_onset():
    hc, cfg = _hc(), AppConfig()
    t = _asleep(hc, cfg, T0, 15)
    v = hc.apply(SleepStage.REM, 0.7, t + timedelta(seconds=30), cfg, sleep_onset_time=None)
    assert v.stage is SleepStage.LIGHT and v.reason == "rem_too_early_after_onset"


def test_an_awakening_restarts_the_provisional_onset():
    hc, cfg = _hc(), AppConfig()
    t = _asleep(hc, cfg, T0, 15)
    t = _awake(hc, cfg, t + timedelta(seconds=30), 3)
    t = _asleep(hc, cfg, t + timedelta(seconds=30), 2)
    v = hc.apply(SleepStage.DEEP, 0.6, t + timedelta(seconds=30), cfg, sleep_onset_time=None)
    assert v.stage is SleepStage.LIGHT and v.reason == "before_sleep_onset"


def test_the_provisional_onset_can_be_switched_off():
    hc, cfg = _hc(), AppConfig()
    cfg.tunables.provisional_onset_min = 0.0
    t = _asleep(hc, cfg, T0, 30)
    v = hc.apply(SleepStage.DEEP, 0.6, t + timedelta(seconds=30), cfg, sleep_onset_time=None)
    assert v.stage is SleepStage.LIGHT and v.reason == "before_sleep_onset"


def test_an_awake_label_is_never_touched():
    """Wake responsiveness is the one thing this must not trade away."""
    hc, cfg = _hc(), AppConfig()
    hc.observe(SleepStage.AWAKE, T0)
    v = hc.apply(SleepStage.AWAKE, 0.9, T0 + timedelta(seconds=30), cfg, sleep_onset_time=None)
    assert v.stage is SleepStage.AWAKE
    assert v.reason is None
    assert v.confidence == 0.9


def test_light_passes_through_untouched():
    v = _hc().apply(SleepStage.LIGHT, 0.5, T0, AppConfig(), sleep_onset_time=None)
    assert v.stage is SleepStage.LIGHT and v.reason is None


def test_a_reclassified_epoch_loses_confidence():
    """The model said REM and we overruled it structurally; presenting LIGHT at full confidence
    would launder a disagreement into an observation."""
    v = _hc().apply(SleepStage.REM, 0.8, T0, AppConfig(), sleep_onset_time=None)
    assert v.confidence is not None and v.confidence < 0.8


def test_constraints_can_be_switched_off():
    cfg = AppConfig()
    cfg.tunables.hypnogram_constraints = False
    assert _hc().apply(SleepStage.REM, 0.7, T0, cfg, sleep_onset_time=None).stage is SleepStage.REM


def test_constrain_does_not_rewrite_the_stage_source():
    """`stage_source` names the estimator and is consumed as a fixed vocabulary."""
    hc = _hc()
    stage, conf, source = constrain((SleepStage.REM, 0.7, "model"), T0, AppConfig(), hc, None)
    assert stage is SleepStage.LIGHT
    assert source == "model"
    assert hc.last_reason == "before_sleep_onset"


# ---------------------------------------------------------------- architecture plausibility
def test_the_2026_08_30_architecture_is_rejected():
    """336 min REM against 2 min deep -- which the steerer read as a 216-minute REM surplus."""
    ok, why = architecture_plausible(deep_min=2.0, rem_min=336.1, light_min=79.0)
    assert ok is False
    assert why is not None and why.startswith("rem_fraction")


def test_a_normal_architecture_is_accepted():
    ok, why = architecture_plausible(deep_min=61.2, rem_min=70.7, light_min=254.8)
    assert ok is True and why is None


def test_too_little_sleep_to_judge_is_not_called_implausible():
    """Early in a night every fraction is noise; blocking the steerer on that would disable it
    exactly when it has the most night left to act on."""
    ok, why = architecture_plausible(deep_min=0.0, rem_min=30.0, light_min=10.0)
    assert ok is True and why is None


def test_deep_does_not_flip_back_one_minute_after_a_light_bout_began():
    """2026-09-08 04:14-04:24: deep/light every 1-5 minutes on single low-HR ticks."""
    hc = _hc()
    cfg = AppConfig()
    onset = T0 - timedelta(hours=6)
    t = T0
    hc.observe(SleepStage.DEEP, t)                       # a real deep bout, then it ends
    t += timedelta(minutes=1)
    v = hc.apply(SleepStage.LIGHT, 0.6, t, cfg, sleep_onset_time=onset); hc.observe(v.stage, t)
    t += timedelta(minutes=1)
    v = hc.apply(SleepStage.DEEP, 0.45, t, cfg, sleep_onset_time=onset)
    assert v.stage is SleepStage.LIGHT and v.reason == "deep_reentry_too_soon"
    hc.observe(v.stage, t)
    t += timedelta(minutes=3)
    v = hc.apply(SleepStage.DEEP, 0.6, t, cfg, sleep_onset_time=onset)
    assert v.stage is SleepStage.DEEP, "after 3+ min of light a new deep bout is allowed"


def test_a_continuing_deep_bout_is_never_interrupted_by_the_reentry_rule():
    hc = _hc()
    onset = T0 - timedelta(hours=2)
    hc.observe(SleepStage.DEEP, T0)
    v = hc.apply(SleepStage.DEEP, 0.6, T0 + timedelta(minutes=1), AppConfig(), sleep_onset_time=onset)
    assert v.stage is SleepStage.DEEP and v.reason is None


def test_a_fresh_constraint_does_not_apply_the_reentry_rule():
    v = _hc().apply(SleepStage.DEEP, 0.6, T0 + timedelta(hours=2), AppConfig(),
                    sleep_onset_time=T0)
    assert v.stage is SleepStage.DEEP


def test_a_pre_onset_relabel_keeps_the_sleep_evidence_above_the_onset_floor():
    """2026-09-07: DEEP at 0.45 before onset became LIGHT at 0.27, under the detector's 0.4."""
    v = _hc().apply(SleepStage.DEEP, 0.45, T0, AppConfig(), sleep_onset_time=None)
    assert v.stage is SleepStage.LIGHT and v.reason == "before_sleep_onset"
    assert v.confidence >= 0.4
    v = _hc().apply(SleepStage.DEEP, 0.3, T0, AppConfig(), sleep_onset_time=None)
    assert v.confidence < 0.4        # a label that was already under the floor stays under it


def test_the_controller_hands_its_config_to_the_constraint(monkeypatch):
    """Without the config the constraint falls back to the strict rule, so the live path must
    pass it for the awakening definition above to apply at all."""
    from sleepctl.controller.controller import SleepController
    from sleepctl.models import ContextRecord, SensorFrame

    seen = []
    real = HypnogramConstraint.observe

    def spy(self, stage, now, cfg=None):
        seen.append(cfg)
        return real(self, stage, now, cfg)

    monkeypatch.setattr(HypnogramConstraint, "observe", spy)
    cfg = AppConfig.default()
    c = SleepController(cfg)
    now = datetime(2026, 6, 23, 23, 0)
    frame = SensorFrame(timestamp=now, stage=SleepStage.UNKNOWN, presence=True,
                        heart_rate=58.0, hrv=62.0, movement=0.03,
                        bed_temp_f=72.0, room_temp_f=68.0, data_age_seconds=20)
    c.decide(frame, ContextRecord(date="2026-06-23"), [], now)
    assert seen and seen[-1] is cfg
