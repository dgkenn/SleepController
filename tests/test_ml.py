"""Tests for the self-learning ML module: ridge core, reward, actions, selection,
the recommender's data/confidence gating, attribution, and export.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.ml.actions import ACTIONS, apply_action
from sleepctl.ml.dataset import build_feature_rows, export_csv
from sleepctl.ml.linalg import ridge_fit
from sleepctl.ml.recommend import recommend_action
from sleepctl.ml.reward import night_outcome_score, reward_from_outcomes
from sleepctl.models import ContextRecord, NightSummary, SetpointProfile
from sleepctl.storage.repository import Repository


# --------------------------------------------------------------------------- linalg
def test_ridge_recovers_known_linear_system():
    # y = 2*x0 - 1*x1 (no noise); ridge with tiny lambda should recover ~[2, -1]
    X = [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0], [2.0, 1.0], [1.0, 2.0]]
    y = [2 * a - b for a, b in X]
    w = ridge_fit(X, y, lam=1e-6)
    assert abs(w[0] - 2.0) < 0.05 and abs(w[1] + 1.0) < 0.05


# --------------------------------------------------------------------------- reward
def test_reward_maintenance_dominates():
    cfg = AppConfig.default()
    good = {"wake_events": 0, "deep_pct": 0.22, "avg_hrv": 70, "sleep_efficiency": 0.92,
            "total_sleep_min": 470}
    fragmented = {**good, "wake_events": 4}
    assert reward_from_outcomes(good, cfg) > reward_from_outcomes(fragmented, cfg)
    # the wake-event penalty should be large relative to other terms
    assert reward_from_outcomes(good, cfg) - reward_from_outcomes(fragmented, cfg) > 8


def test_reward_penalizes_churn_and_swing():
    cfg = AppConfig.default()
    base = {"wake_events": 1, "deep_pct": 0.2, "avg_hrv": 60, "sleep_efficiency": 0.88}
    assert reward_from_outcomes(base, cfg, churn=5) < reward_from_outcomes(base, cfg, churn=0)
    assert reward_from_outcomes(base, cfg, temp_swing_over_cap=4) < reward_from_outcomes(base, cfg)


# --------------------------------------------------------------------------- actions
def test_apply_action_bounded_and_versioned():
    p = AppConfig.default().default_setpoints()
    cooled = apply_action(p, next(a for a in ACTIONS if a.name == "strong_cool"))
    assert cooled.deep_bias_f == p.deep_bias_f - 2.0
    assert cooled.version == p.version + 1 and cooled.source == "ml"
    # no_change does not bump the version
    same = apply_action(p, next(a for a in ACTIONS if a.name == "no_change"))
    assert same.version == p.version
    # bounds respected even with a huge nominal delta
    cold = SetpointProfile(neutral_f=59, deep_bias_f=59, rem_warm_offset_f=0, wake_ramp_f=74,
                           composite_bed_weight=0.75)
    out = apply_action(cold, next(a for a in ACTIONS if a.name == "strong_cool"))
    assert out.deep_bias_f >= 58.0  # clamped to bound


# --------------------------------------------------------- recommender gating + learning
def _seed_nights(repo, n, deep_for_setpoint, start_version_deep=66.0, ctx_clean=True):
    """Seed n nights where cooler deep setpoint -> more deep sleep (a learnable signal)."""
    base = datetime(2026, 5, 1)
    for i in range(n):
        # alternate the deep setpoint so the model sees variation
        deep = start_version_deep - (i % 5)  # 66,65,64,63,62,66,...
        v = i
        repo.save_setpoints(SetpointProfile(
            neutral_f=70, deep_bias_f=deep, rem_warm_offset_f=1.5, wake_ramp_f=74,
            composite_bed_weight=0.75, version=v, source="seed"))
        date = (base + timedelta(days=i)).date().isoformat()
        deep_min = deep_for_setpoint(deep)
        ns = NightSummary(date=date, total_sleep_min=470, deep_min=deep_min, rem_min=110,
                          light_min=470 - deep_min - 110, wake_events=1, waso_min=15,
                          sleep_efficiency=0.9, avg_hrv=65, avg_respiratory_rate=13,
                          sleep_onset_latency_min=14, setpoint_version=v)
        repo.save_night_summary(ns)
        repo.save_context(ContextRecord(date=date, is_short_sleep_day=(not ctx_clean)))


def test_recommender_defers_without_enough_data():
    cfg = AppConfig.default()
    repo = Repository(":memory:")
    _seed_nights(repo, 5, lambda deep: 100)  # only 5 nights < min_nights
    assert recommend_action(repo, cfg.default_setpoints(), cfg) is None


def test_recommender_learns_cooling_helps_deep():
    cfg = AppConfig.default()
    repo = Repository(":memory:")
    # cooler deep setpoint => more deep sleep (strong, low-noise signal)
    _seed_nights(repo, 25, lambda deep: 160 - 6 * (deep - 62))
    chosen = recommend_action(repo, SetpointProfile(
        neutral_f=70, deep_bias_f=66, rem_warm_offset_f=1.5, wake_ramp_f=74,
        composite_bed_weight=0.75, version=99), cfg)
    assert chosen is not None
    # it should prefer a cooling action (or at least not warming) given the signal
    assert chosen.name in ("slight_cool", "strong_cool", "no_change")


def test_recommender_excludes_confounded_nights():
    cfg = AppConfig.default()
    repo = Repository(":memory:")
    # all nights flagged short-sleep (confounded) -> no clean data -> defer
    _seed_nights(repo, 25, lambda deep: 120, ctx_clean=False)
    assert recommend_action(repo, cfg.default_setpoints(), cfg) is None


# --------------------------------------------------------------------- nightly + export
def test_nightly_scores_and_logs_action():
    from sleepctl.loop.nightly import NightlyUpdater

    cfg = AppConfig.default()
    repo = Repository(":memory:")
    updater = NightlyUpdater(cfg, repo)
    night = NightSummary(date="2026-06-10", total_sleep_min=460, deep_min=110, rem_min=110,
                         wake_events=1, sleep_efficiency=0.9, avg_hrv=66, sleep_onset_latency_min=14)
    result = updater.run(night)
    # the night got an outcome_score and an action was logged
    assert repo.recent_nights(1)[0].outcome_score is not None
    assert len(repo.recent_actions(5)) == 1
    assert "chosen" in result


def test_features_and_phenotype():
    from sleepctl.ml.features import engineer_features
    from sleepctl.ml.phenotype import correlate_with_outcome

    cfg = AppConfig.default()
    repo = Repository(":memory:")
    from sleepctl.loop.nightly import NightlyUpdater

    up = NightlyUpdater(cfg, repo, use_ml=False)
    base = datetime(2026, 5, 1)
    for i in range(10):
        d = (base + timedelta(days=i)).date().isoformat()
        repo.save_context(ContextRecord(date=d, late_night_work=(i % 2 == 0)))
        # late-night-work nights are worse (more wake events)
        we = 3 if i % 2 == 0 else 1
        up.run(NightSummary(date=d, total_sleep_min=460, deep_min=100, rem_min=110,
                            wake_events=we, sleep_efficiency=0.9, avg_hrv=64,
                            sleep_onset_latency_min=14))
    feats = engineer_features(repo)
    assert any("late_night_work_flag" in f for f in feats.values())
    corr = correlate_with_outcome(repo)
    # late_night_work_flag should show a (negative) correlation with the reward
    names = [c[0] for c in corr]
    assert "late_night_work_flag" in names


def test_export_csv_has_header_and_rows(tmp_path):
    repo = Repository(":memory:")
    repo.save_setpoints(SetpointProfile(neutral_f=70, deep_bias_f=66, rem_warm_offset_f=1.5,
                                        wake_ramp_f=74, composite_bed_weight=0.75, version=0))
    repo.save_night_summary(NightSummary(date="2026-06-10", total_sleep_min=470, deep_min=110,
                                         rem_min=110, wake_events=1, setpoint_version=0))
    out = tmp_path / "feats.csv"
    n = export_csv(repo, str(out))
    assert n == 1
    with open(out) as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["date"] == "2026-06-10"
    assert "deep_pct" in rows[0] and "deep_bias_f" in rows[0]
