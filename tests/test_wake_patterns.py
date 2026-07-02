"""3AM WAKE targeted analysis (sleepctl.analysis.wake_patterns): clustering, correlation,
the full report, and the controller pre-emption gate. All standalone -- no efficacy-trial
dependency, pure-python stats, deterministic (timestamps always supplied explicitly)."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta

import pytest

from sleepctl.analysis.wake_patterns import (
    _suggestion_for,
    cluster_awakenings,
    correlate_wakes,
    mean_diff_ci,
    odds_ratio,
    point_biserial,
    should_preempt_window,
    wake_analysis_report,
)
from sleepctl.config import AppConfig
from sleepctl.models import SensorFrame, SleepStage
from sleepctl.storage.repository import Repository


@pytest.fixture
def repo():
    r = Repository(tempfile.mktemp(suffix=".db"))
    yield r
    r.close()


def _register_night(repo, night_date: str) -> None:
    repo.conn.execute("INSERT OR REPLACE INTO nightly_summaries (date) VALUES (?)", (night_date,))
    repo.conn.commit()


def _night_ts(night_date: str, hour: int, minute: int = 0) -> datetime:
    """Map an hour-of-night to a real timestamp: hours < 12 roll onto the NEXT calendar date
    (matches how a night spanning midnight is tagged with one ``night_date`` label)."""
    d = datetime.fromisoformat(night_date)
    if hour < 12:
        d = d + timedelta(days=1)
    return d.replace(hour=hour, minute=minute)


def _bg_sample(repo, night_date, hour, minute=0, stage=SleepStage.LIGHT, bed=68.0, room=67.0,
              hrv=60.0, wake=False):
    ts = _night_ts(night_date, hour, minute)
    frame = SensorFrame(timestamp=ts, stage=stage, stage_confidence=0.85, heart_rate=55,
                        hrv=hrv, respiratory_rate=14, movement=0.05, presence=True,
                        bed_temp_f=bed, room_temp_f=room, data_age_seconds=5.0)
    repo.log_sample(frame, "maintenance", wake, night_date)
    return ts


def _decision(repo, night_date, ts, target_temp_f):
    repo.conn.execute(
        "INSERT INTO decisions (ts, night_date, target_temp_f) VALUES (?,?,?)",
        (ts.isoformat(), night_date, target_temp_f))
    repo.conn.commit()


def _seed_recurring_night(repo, night_date, wake=True, bed_setpoint=68.0, exit_stage=SleepStage.REM,
                          hour=3, minute=10):
    """One synthetic night: an evening baseline sample, an asleep-stage sample right before the
    target clock time (establishes ``stage_exited``), and (optionally) a wake_event at the
    target time. Also logs the bed setpoint (decisions.target_temp_f) ahead of the window."""
    _register_night(repo, night_date)
    onset_ts = _bg_sample(repo, night_date, 23, 0, stage=SleepStage.LIGHT, bed=bed_setpoint)
    pre_ts = _bg_sample(repo, night_date, hour, max(0, minute - 5), stage=exit_stage,
                        bed=bed_setpoint)
    _decision(repo, night_date, pre_ts - timedelta(minutes=1), bed_setpoint)
    if wake:
        _bg_sample(repo, night_date, hour, minute, stage=SleepStage.AWAKE, bed=bed_setpoint,
                  wake=True)
    else:
        _bg_sample(repo, night_date, hour, minute, stage=exit_stage, bed=bed_setpoint, wake=False)
    return onset_ts


# ------------------------------------------------------------------------- cluster_awakenings

def test_cluster_awakenings_empty_on_no_data(repo):
    assert cluster_awakenings(repo) == []


def test_cluster_awakenings_ignores_a_single_odd_night(repo):
    _seed_recurring_night(repo, "2026-06-01", wake=True)
    clusters = cluster_awakenings(repo, lookback_nights=10)
    assert clusters == []  # one occurrence is not yet "recurring"


def test_cluster_awakenings_surfaces_injected_3am_rem_window(repo):
    dates = [f"2026-06-{d:02d}" for d in range(1, 9)]  # 8 nights
    for i, d in enumerate(dates):
        # every night wakes ~03:10 out of REM; sprinkle a harmless one-off elsewhere on night 0
        _seed_recurring_night(repo, d, wake=True, exit_stage=SleepStage.REM)
    clusters = cluster_awakenings(repo, lookback_nights=10)
    assert clusters, "expected the recurring 03:00-03:30 window to surface"
    top = clusters[0]
    assert top.label.startswith("03:00")
    assert top.stage_exited == "rem"
    assert top.nights_woke == 8
    assert top.confidence_label in ("moderate", "high")


def test_cluster_awakenings_confidence_rises_with_more_evidence(repo):
    dates = [f"2026-06-{d:02d}" for d in range(1, 11)]  # 10 nights, every one wakes at 03:10
    for d in dates:
        _seed_recurring_night(repo, d, wake=True, exit_stage=SleepStage.REM)

    def _conf(n):
        cs = cluster_awakenings(repo, lookback_nights=n)
        assert cs
        return cs[0].confidence

    conf_3 = _conf(3)
    conf_5 = _conf(5)
    conf_8 = _conf(8)
    assert conf_3 < conf_5 < conf_8
    assert conf_8 <= 1.0


def test_cluster_awakenings_low_evidence_is_low_confidence(repo):
    # 2 wake nights out of 12 OBSERVED nights in the same clock bin -> thin, inconsistent
    # evidence -> low confidence (but still reported, not hidden).
    wake_dates = ["2026-06-01", "2026-06-02"]
    quiet_dates = [f"2026-06-{d:02d}" for d in range(3, 13)]
    for d in wake_dates:
        _seed_recurring_night(repo, d, wake=True)
    for d in quiet_dates:
        _seed_recurring_night(repo, d, wake=False)
    clusters = cluster_awakenings(repo, lookback_nights=20)
    assert clusters
    top = clusters[0]
    assert top.nights_woke == 2
    assert top.confidence < 0.35
    assert top.confidence_label == "low"


# ------------------------------------------------------------------------------ correlate_wakes

def test_correlate_wakes_recovers_warm_bed_association(repo):
    wake_dates = [f"2026-06-{d:02d}" for d in range(1, 6)]      # 5 wake nights, WARM setpoint
    quiet_dates = [f"2026-06-{d:02d}" for d in range(11, 16)]   # 5 quiet nights, COOL setpoint
    for d in wake_dates:
        _seed_recurring_night(repo, d, wake=True, bed_setpoint=73.0)
    for d in quiet_dates:
        _seed_recurring_night(repo, d, wake=False, bed_setpoint=66.0)

    clusters = cluster_awakenings(repo, lookback_nights=20)
    assert clusters
    cluster = clusters[0]
    corr = correlate_wakes(repo, cluster, lookback_nights=20)
    assert corr["drivers"]
    top = corr["drivers"][0]
    assert top["driver"] == "bed_setpoint_f"
    assert top["point_biserial_r"] > 0.5   # warm bed strongly co-occurs with waking
    assert top["mean_diff"]["mean_on_wake_nights"] > top["mean_diff"]["mean_otherwise"]


def test_correlate_wakes_empty_cluster_is_safe(repo):
    from sleepctl.analysis.wake_patterns import WakeCluster
    empty = WakeCluster(bin_index=6, bin_min=30, label="03:00–03:30", stage_exited="unknown")
    corr = correlate_wakes(repo, empty, lookback_nights=10)
    assert corr["n_nights"] == 0
    assert corr["drivers"] == []


# ------------------------------------------------------------------------------ pure stats

def test_point_biserial_direction_and_none_cases():
    values = [72, 73, 71, 74, 60, 61, 59, 62]
    labels = [1, 1, 1, 1, 0, 0, 0, 0]
    r = point_biserial(values, labels)
    assert r > 0.8
    assert point_biserial([1, 2], [1, 0]) is None      # too few points
    assert point_biserial([1, 2, 3, 4], [1, 1, 1, 1]) is None  # only one label present


def test_mean_diff_ci_reports_both_groups():
    values = [72, 73, 71, 74, 60, 61, 59, 62]
    labels = [1, 1, 1, 1, 0, 0, 0, 0]
    md = mean_diff_ci(values, labels)
    assert md["mean_on_wake_nights"] > md["mean_otherwise"]
    assert md["diff"] > 0
    assert md["ci90_low"] <= md["diff"] <= md["ci90_high"]


def test_odds_ratio_haldane_anscombe_never_divides_by_zero():
    # a zero cell must not raise or return inf
    orv = odds_ratio(a=5, b=0, c=1, d=5)
    assert orv > 1.0
    assert math_is_finite(orv)


def math_is_finite(x):
    import math
    return math.isfinite(x)


# --------------------------------------------------------------------------- wake_analysis_report

def test_wake_analysis_report_shape_empty(repo):
    report = wake_analysis_report(repo)
    assert report["n_recurring_windows"] == 0
    assert report["recurring_windows"] == []
    assert "note" in report


def test_wake_analysis_report_low_confidence_gets_no_suggestion(repo):
    wake_dates = ["2026-06-01", "2026-06-02"]
    quiet_dates = [f"2026-06-{d:02d}" for d in range(3, 20)]
    for d in wake_dates:
        _seed_recurring_night(repo, d, wake=True)
    for d in quiet_dates:
        _seed_recurring_night(repo, d, wake=False)
    report = wake_analysis_report(repo, lookback_nights=25, cfg=AppConfig.default())
    assert report["recurring_windows"]
    w = report["recurring_windows"][0]
    assert w["window"]["confidence_label"] == "low"
    assert w["suggestion"] is None


def test_wake_analysis_report_high_confidence_gets_bounded_suggestion(repo):
    cfg = AppConfig.default()
    cfg.tunables.wake_window_preempt_max_f = 0.4
    dates = [f"2026-06-{d:02d}" for d in range(1, 10)]
    for d in dates:
        _seed_recurring_night(repo, d, wake=True, bed_setpoint=73.0, exit_stage=SleepStage.REM)
    report = wake_analysis_report(repo, lookback_nights=15, cfg=cfg)
    w = report["recurring_windows"][0]
    assert w["window"]["confidence_label"] in ("moderate", "high")
    sugg = w["suggestion"]
    assert sugg is not None
    assert sugg["nudge_f"] <= cfg.tunables.wake_window_preempt_max_f + 1e-9
    assert sugg["action"] in ("cool", "warm")
    assert sugg["window_clock_time"].startswith("03:")


def test_suggestion_helper_respects_configured_cap_directly(repo):
    from sleepctl.analysis.wake_patterns import WakeCluster
    cluster = WakeCluster(bin_index=6, bin_min=30, label="03:00–03:30", stage_exited="rem",
                          nights_observed=10, nights_woke=8, confidence=0.9,
                          confidence_label="high")
    corr = {
        "drivers": [{
            "driver": "bed_setpoint_f", "label": "bed setpoint (target °F)", "type": "continuous",
            "point_biserial_r": 0.9, "n": 8,
            "mean_diff": {"mean_on_wake_nights": 80.0, "mean_otherwise": 60.0, "diff": 20.0,
                          "ci90_low": 15.0, "ci90_high": 25.0, "n_wake": 4, "n_no_wake": 4},
        }],
    }
    cfg = AppConfig.default()
    cfg.tunables.wake_window_preempt_max_f = 0.25
    sugg = _suggestion_for(cluster, corr, cfg)
    assert sugg is not None
    assert sugg["nudge_f"] <= 0.25 + 1e-9
    assert sugg["action"] == "cool"


# --------------------------------------------------------------------------- should_preempt_window

def _high_conf_window(nights_observed=12, confidence=0.8, start=180, end=210):
    return {"window": {"label": "03:00–03:30", "bin_start_min": start, "bin_end_min": end,
                       "stage_exited": "rem", "nights_observed": nights_observed,
                       "confidence": confidence, "confidence_label": "high"}}


def test_should_preempt_window_fires_inside_lead_window():
    cfg = AppConfig.default()
    windows = [_high_conf_window()]
    now = datetime(2026, 6, 24, 2, 45)  # inside [02:40, 03:30) with default 20-min lead
    hit = should_preempt_window(windows, now, cfg)
    assert hit is not None
    assert hit["label"] == "03:00–03:30"


def test_should_preempt_window_silent_before_lead_and_after_end():
    cfg = AppConfig.default()
    windows = [_high_conf_window()]
    assert should_preempt_window(windows, datetime(2026, 6, 24, 2, 0), cfg) is None
    assert should_preempt_window(windows, datetime(2026, 6, 24, 3, 45), cfg) is None


def test_should_preempt_window_gated_by_confidence():
    cfg = AppConfig.default()
    windows = [_high_conf_window(confidence=0.2)]
    now = datetime(2026, 6, 24, 2, 45)
    assert should_preempt_window(windows, now, cfg) is None


def test_should_preempt_window_gated_by_min_nights():
    cfg = AppConfig.default()
    windows = [_high_conf_window(nights_observed=1)]
    now = datetime(2026, 6, 24, 2, 45)
    assert should_preempt_window(windows, now, cfg) is None


def test_should_preempt_window_disabled_flag_is_a_hard_off():
    cfg = AppConfig.default()
    cfg.tunables.wake_window_preempt_enabled = False
    windows = [_high_conf_window()]
    now = datetime(2026, 6, 24, 2, 45)
    assert should_preempt_window(windows, now, cfg) is None


def test_should_preempt_window_empty_report_is_safe():
    cfg = AppConfig.default()
    now = datetime(2026, 6, 24, 2, 45)
    assert should_preempt_window(None, now, cfg) is None
    assert should_preempt_window([], now, cfg) is None
