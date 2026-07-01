"""Data-quality gate: the pure ``assess_data_quality`` scoring function, plus its wiring into
``SleepController.decide`` (down-weighted confidence + biased-toward-HOLD on bad data, and a
same-as-before path on good data)."""

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.controller.data_quality import assess_data_quality
from sleepctl.models import ContextRecord, CorrectionAction, SensorFrame, SleepStage


def _good_frame(now, **overrides):
    defaults = dict(
        timestamp=now,
        stage=SleepStage.LIGHT,
        stage_confidence=0.9,
        heart_rate=58.0,
        hrv=65.0,
        respiratory_rate=14.0,
        movement=0.05,
        presence=True,
        bed_temp_f=70.0,
        room_temp_f=68.0,
        data_age_seconds=5.0,
    )
    defaults.update(overrides)
    return SensorFrame(**defaults)


# --------------------------------------------------------------------- pure scoring function
def test_good_frame_scores_near_one_with_no_reasons():
    cfg = AppConfig.default()
    now = datetime(2026, 6, 24, 2, 0)
    dq = assess_data_quality(_good_frame(now), cfg, now)
    assert dq.score == 1.0
    assert dq.reasons == []
    assert dq.top_reason is None


def test_stale_ish_data_docks_score():
    cfg = AppConfig.default()
    now = datetime(2026, 6, 24, 2, 0)
    # Below the hard stale_data_seconds guard, but above telemetry_stale_seconds.
    frame = _good_frame(now, data_age_seconds=cfg.tunables.telemetry_stale_seconds + 5)
    dq = assess_data_quality(frame, cfg, now)
    assert dq.score < 1.0
    assert "data_stale" in dq.reasons


def test_unknown_age_docks_score():
    cfg = AppConfig.default()
    now = datetime(2026, 6, 24, 2, 0)
    frame = _good_frame(now, data_age_seconds=None)
    dq = assess_data_quality(frame, cfg, now)
    assert dq.score < 1.0
    assert "data_age_unknown" in dq.reasons


def test_high_movement_docks_score():
    cfg = AppConfig.default()
    now = datetime(2026, 6, 24, 2, 0)
    frame = _good_frame(now, movement=0.45)
    dq = assess_data_quality(frame, cfg, now)
    assert dq.score < 1.0
    assert "high_movement" in dq.reasons


def test_missing_vitals_docks_score():
    cfg = AppConfig.default()
    now = datetime(2026, 6, 24, 2, 0)
    frame = _good_frame(now, heart_rate=None, hrv=None, respiratory_rate=None)
    dq = assess_data_quality(frame, cfg, now)
    assert dq.score < 1.0
    assert any(r.startswith("missing_vitals:") for r in dq.reasons)


def test_uncertain_presence_docks_score():
    cfg = AppConfig.default()
    now = datetime(2026, 6, 24, 2, 0)
    frame = _good_frame(now, presence=None)
    dq = assess_data_quality(frame, cfg, now)
    assert dq.score < 1.0
    assert "presence_unknown" in dq.reasons


def test_absent_presence_docks_more_than_uncertain():
    cfg = AppConfig.default()
    now = datetime(2026, 6, 24, 2, 0)
    unknown = assess_data_quality(_good_frame(now, presence=None), cfg, now)
    absent = assess_data_quality(_good_frame(now, presence=False), cfg, now)
    assert absent.score < unknown.score
    assert "presence_absent" in absent.reasons


def test_low_stage_confidence_docks_score():
    cfg = AppConfig.default()
    now = datetime(2026, 6, 24, 2, 0)
    frame = _good_frame(now, stage_confidence=0.1)
    dq = assess_data_quality(frame, cfg, now)
    assert dq.score < 1.0
    assert "low_stage_confidence" in dq.reasons


def test_score_never_negative_even_when_everything_is_bad():
    cfg = AppConfig.default()
    now = datetime(2026, 6, 24, 2, 0)
    frame = _good_frame(
        now, data_age_seconds=None, movement=0.9, heart_rate=None, hrv=None,
        respiratory_rate=None, presence=False, stage_confidence=None,
    )
    dq = assess_data_quality(frame, cfg, now)
    assert 0.0 <= dq.score <= 1.0


def test_worst_offender_is_top_reason():
    cfg = AppConfig.default()
    now = datetime(2026, 6, 24, 2, 0)
    # presence_absent (0.4 penalty) should outrank stage_confidence_unknown (0.15 penalty).
    frame = _good_frame(now, presence=False, stage_confidence=None)
    dq = assess_data_quality(frame, cfg, now)
    assert dq.top_reason == "presence_absent"


# ---------------------------------------------------------------- wiring into SleepController
def _advance_to_maintenance(controller, cfg, now):
    """Feed enough good frames to reach MAINTENANCE, returning the next timestamp + recent list."""
    recent = []
    ctx = ContextRecord(date=now.date().isoformat())
    for i in range(40):
        frame = _good_frame(now, stage=SleepStage.LIGHT if i > 15 else SleepStage.AWAKE)
        controller.decide(frame, ctx, recent, now)
        recent.append(frame)
        now += timedelta(minutes=1)
    return now, recent, ctx


def test_good_data_gate_is_a_noop_on_confidence_and_reason():
    """Do-no-harm floor: on a fully trustworthy frame the gate must not change anything —
    normal control on good-data nights stays exactly as before this feature."""
    cfg = AppConfig.default()
    controller = SleepController(cfg)
    now = datetime(2026, 6, 24, 0, 0)
    now, recent, ctx = _advance_to_maintenance(controller, cfg, now)
    frame = _good_frame(now)
    decision = controller.decide(frame, ctx, recent, now)
    dq = decision.log_payload["data_quality"]
    assert dq["score"] == 1.0
    assert dq["reasons"] == []
    assert "data_quality" not in decision.reason
    assert decision.action is not None  # normal decision produced, not a forced hold


def test_low_quality_frame_forces_hold_and_downweights_confidence():
    cfg = AppConfig.default()
    controller = SleepController(cfg)
    now = datetime(2026, 6, 24, 0, 0)
    now, recent, ctx = _advance_to_maintenance(controller, cfg, now)
    last_target = controller._last_target_f

    # Below the hard hold-score floor: high movement + missing vitals + unknown stage conf.
    bad_frame = _good_frame(
        now, movement=0.5, heart_rate=None, hrv=None, respiratory_rate=None,
        stage_confidence=None,
    )
    decision = controller.decide(bad_frame, ctx, recent, now)
    assert decision.action is CorrectionAction.HOLD
    assert decision.target_temp_f == round(last_target, 2)
    assert decision.confidence <= 0.3
    assert "data quality low" in decision.reason
    dq = decision.log_payload["data_quality"]
    assert dq["score"] < cfg.tunables.data_quality_hold_score
    assert dq["top_reason"] is not None


def test_borderline_quality_downweights_confidence_without_forcing_hold():
    cfg = AppConfig.default()
    controller = SleepController(cfg)
    now = datetime(2026, 6, 24, 0, 0)
    now, recent, ctx = _advance_to_maintenance(controller, cfg, now)

    # Above the hard hold floor but below the downweight floor (e.g. missing vitals + a touch
    # of movement) -- must NOT force a hold, but confidence should be reduced vs a perfectly
    # clean frame, and the reason should mention the data-quality discount.
    borderline = _good_frame(now, hrv=None, respiratory_rate=None, movement=0.25)
    decision = controller.decide(borderline, ctx, recent, now)
    dq = decision.log_payload["data_quality"]
    assert cfg.tunables.data_quality_hold_score <= dq["score"] < cfg.tunables.data_quality_downweight_score
    assert decision.confidence < 0.9
    assert "data_quality=" in decision.reason


def test_confirmed_bed_exit_is_not_gated_even_with_low_score():
    """Bed-exit (presence=False) is high-confidence, actionable info -- the state machine must
    still be allowed to advance to IDLE, not get frozen by the low data-quality score that
    an empty bed naturally produces (no vitals, no stage confidence)."""
    cfg = AppConfig.default()
    controller = SleepController(cfg)
    now = datetime(2026, 6, 24, 0, 0)
    now, recent, ctx = _advance_to_maintenance(controller, cfg, now)

    exit_frame = _good_frame(
        now, presence=False, heart_rate=None, hrv=None, respiratory_rate=None,
        stage_confidence=None, stage=SleepStage.AWAKE,
    )
    decision = controller.decide(exit_frame, ctx, recent, now)
    dq = decision.log_payload["data_quality"]
    assert dq["score"] < cfg.tunables.data_quality_hold_score  # score IS low...
    assert "data quality low" not in decision.reason           # ...but not gated on it


def test_data_quality_summary_reports_gating_state():
    cfg = AppConfig.default()
    controller = SleepController(cfg)
    now = datetime(2026, 6, 24, 0, 0)
    now, recent, ctx = _advance_to_maintenance(controller, cfg, now)

    good = controller.data_quality_summary()
    assert good["gating"] is False

    bad_frame = _good_frame(now, movement=0.5, heart_rate=None, hrv=None,
                            respiratory_rate=None, stage_confidence=None)
    controller.decide(bad_frame, ctx, recent, now)
    bad = controller.data_quality_summary()
    assert bad["gating"] is True
    assert bad["top_reason"] is not None
