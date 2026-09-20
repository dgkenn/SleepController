"""Feature-1 upgrades: signed learnable settle nudge, evidence weighting, instability gain."""

import tempfile
from datetime import datetime, timedelta

import pytest

from sleepctl.config import AppConfig
from sleepctl.controller.precursor import PrecursorDetector
from sleepctl.controller.thermal import ThermalController
from sleepctl.learning.settle import learn_settle_nudge
from sleepctl.models import NightObjective, SensorFrame, SleepStage, ThermalIntent
from sleepctl.storage.repository import Repository


# ---- signed settle nudge --------------------------------------------------
def _cooling_allowed(cfg):
    from dataclasses import replace
    return replace(cfg, tunables=replace(cfg.tunables, settle_cooling_allowed=True,
                                         maintenance_settle_nudge_f=-1.0))


def test_settle_nudge_signed_and_clamped():
    cfg = _cooling_allowed(AppConfig.default())
    th = ThermalController(cfg)
    neutral = th.target_for(ThermalIntent.NEUTRAL, NightObjective.OPTIMIZE, hot_sleeper=True)
    cool = th.target_for(ThermalIntent.SETTLE_COOL, NightObjective.OPTIMIZE, hot_sleeper=True)
    assert cool < neutral  # with cooling allowed the -1.0 default is a cool settle
    th.set_settle_nudge(1.5)  # learn the WARM direction
    warm = th.target_for(ThermalIntent.SETTLE_COOL, NightObjective.OPTIMIZE, hot_sleeper=True)
    assert warm > neutral
    th.set_settle_nudge(-10)  # beyond cap
    assert abs(th.settle_nudge_f) <= cfg.tunables.maintenance_settle_cap_f + 1e-9


def test_the_default_settle_holds_neutral_and_never_learns_to_cool():
    """2026-09-19: the settle cooled this user to 68 F and 68 F woke them cold. The shipped
    default is therefore "hold neutral": no cooling nudge, and a learner that asks for one is
    clamped to zero. Warming is still learnable."""
    cfg = AppConfig.default()
    assert cfg.tunables.settle_cooling_allowed is False
    assert cfg.tunables.maintenance_settle_nudge_f == 0.0
    th = ThermalController(cfg)
    neutral = th.target_for(ThermalIntent.NEUTRAL, NightObjective.OPTIMIZE, hot_sleeper=True)
    settle = th.target_for(ThermalIntent.SETTLE_COOL, NightObjective.OPTIMIZE, hot_sleeper=True)
    assert settle == neutral
    th.set_settle_nudge(-1.0)
    assert th.settle_nudge_f == 0.0
    th.set_settle_nudge(0.5)
    assert th.settle_nudge_f == 0.5


def test_settle_learner_flips_when_cooling_fails():
    cfg = _cooling_allowed(AppConfig.default())
    repo = Repository(tempfile.mktemp(suffix=".db"))
    try:
        # 8 resolved pre-cools that did NOT prevent the awakening -> cooling isn't working
        for i in range(8):
            repo.conn.execute(
                "INSERT INTO precool_events (night_date, ts, window_type, lead_used_min, "
                "eta_min, prevented, resolved) VALUES (?,?,?,?,?,0,1)",
                ("2026-06-25", f"2026-06-26T0{i}:00:00", "circadian", 10, 5))
        repo.conn.commit()
        nudge = learn_settle_nudge(repo, cfg)
        assert nudge > 0  # flipped from the cool default toward warm exploration
    finally:
        repo.close()


def test_settle_learner_explores_warm_from_a_zero_base_when_prevention_fails():
    """With the default "hold neutral" settle there is no cool direction to flip from; a
    failing pre-emption record still moves the learner, and the only way it may go is warm."""
    from sleepctl.learning.settle import EXPLORE_WARM_F
    cfg = AppConfig.default()
    repo = Repository(tempfile.mktemp(suffix=".db"))
    try:
        for i in range(8):
            repo.conn.execute(
                "INSERT INTO precool_events (night_date, ts, window_type, lead_used_min, "
                "eta_min, prevented, resolved) VALUES (?,?,?,?,?,0,1)",
                ("2026-06-25", f"2026-06-26T0{i}:00:00", "circadian", 10, 5))
        repo.conn.commit()
        assert learn_settle_nudge(repo, cfg) == EXPLORE_WARM_F
    finally:
        repo.close()


def test_settle_learner_never_returns_a_cooling_nudge_while_cooling_is_disallowed():
    from dataclasses import replace
    cfg = AppConfig.default()
    cfg = replace(cfg, tunables=replace(cfg.tunables, maintenance_settle_nudge_f=-1.0))
    repo = Repository(tempfile.mktemp(suffix=".db"))
    try:
        for i in range(8):
            repo.conn.execute(
                "INSERT INTO precool_events (night_date, ts, window_type, lead_used_min, "
                "eta_min, prevented, resolved) VALUES (?,?,?,?,?,1,1)",
                ("2026-06-25", f"2026-06-26T0{i}:00:00", "circadian", 10, 5))
        repo.conn.commit()
        assert learn_settle_nudge(repo, cfg) == 0.0     # "working" cooling is still refused
    finally:
        repo.close()


def test_settle_learner_keeps_default_when_working():
    cfg = AppConfig.default()
    repo = Repository(tempfile.mktemp(suffix=".db"))
    try:
        for i in range(8):
            repo.conn.execute(
                "INSERT INTO precool_events (night_date, ts, window_type, lead_used_min, "
                "eta_min, prevented, resolved) VALUES (?,?,?,?,?,1,1)",
                ("2026-06-25", f"2026-06-26T0{i}:00:00", "circadian", 10, 5))
        repo.conn.commit()
        assert learn_settle_nudge(repo, cfg) == cfg.tunables.maintenance_settle_nudge_f
    finally:
        repo.close()


def test_settle_learner_default_without_evidence():
    cfg = AppConfig.default()
    repo = Repository(tempfile.mktemp(suffix=".db"))
    try:
        assert learn_settle_nudge(repo, cfg) == cfg.tunables.maintenance_settle_nudge_f
    finally:
        repo.close()


# ---- evidence weighting + instability gain --------------------------------
def _series(specs, stage=SleepStage.LIGHT):
    base = datetime(2026, 6, 27, 3, 0)
    return [SensorFrame(timestamp=base + timedelta(seconds=30 * i), stage=stage, presence=True,
                        **s) for i, s in enumerate(specs)]


def test_hrv_is_weighted_highest():
    cfg = AppConfig.default()
    # pure HRV decay -> score equals the HRV weight (highest single contributor)
    frames = _series([{"heart_rate": 55, "hrv": 64 - i * 1.0, "movement": 0.05,
                       "bed_temp_f": 88, "respiratory_rate": 14} for i in range(10)])
    a = PrecursorDetector(cfg).detect(frames[-1], frames[:-1], frames[-1].timestamp, 55, 64)
    assert "hrv_decay" in a.reasons
    assert abs(a.score - cfg.tunables.precursor_w_hrv) < 1e-6


def test_instability_lowers_threshold():
    cfg = AppConfig.default()
    det = PrecursorDetector(cfg)
    # a borderline single-signal score (bed warming = w_bed below threshold), but a window full
    # of movement bursts raises instability and should pull the pre-empt threshold down.
    specs = [{"heart_rate": 55, "hrv": 60, "movement": 0.4,
              "bed_temp_f": 86 + i * 0.2, "respiratory_rate": 14} for i in range(10)]
    frames = _series(specs)
    a = det.detect(frames[-1], frames[:-1], frames[-1].timestamp, 55, 60)
    assert a.signals.get("instability", 0) > 0.5
