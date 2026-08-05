"""Reconstructing a NightSummary from our OWN persisted frames.

The Eight Sleep adapters' ``fetch_night_summary`` is a stub returning an all-``None`` summary
(the nightly metrics are membership-gated), so the daemon's close-out threw every night, was
swallowed by ``_skip``, and left ``nightly_summaries`` permanently empty — starving every
learner, efficacy trial and report downstream of it. See ``sleepctl.loop.night_rollup``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta

from sleepctl.loop.night_rollup import merge_night_summary, reconstruct_night_summary
from sleepctl.models import NightSummary
from sleepctl.storage.repository import Repository


@contextmanager
def _repo():
    """Windows keeps the sqlite file open, so the connection must close before teardown."""
    tmp = tempfile.mkdtemp()
    repo = Repository(os.path.join(tmp, "sleepctl.db"))
    try:
        yield repo
    finally:
        repo.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _write(repo, ts, *, stage, state, hr=58.0, hrv=60.0, movement=0.02, level=-50):
    repo.conn.execute(
        "INSERT INTO raw_samples (ts, night_date, stage, stage_confidence, heart_rate, hrv,"
        " respiratory_rate, movement, presence, bed_temp_f, room_temp_f, commanded_level,"
        " controller_state, wake_event, data_age_seconds)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts.isoformat(), "2026-06-23", stage, 0.7, hr, hrv, 14.0, movement, 1,
         72.0, 68.0, level, state, 0, 5.0),
    )


def _night(repo, *, stages, start=None, step_min=1.0, latency_min=10.0):
    """Write ``latency_min`` of INDUCTION then one MAINTENANCE sample per entry in ``stages``."""
    start = start or datetime(2026, 6, 23, 23, 0)
    t = start
    while t < start + timedelta(minutes=latency_min):
        _write(repo, t, stage="awake", state="induction")
        t += timedelta(minutes=step_min)
    for s in stages:
        _write(repo, t, stage=s, state="maintenance")
        t += timedelta(minutes=step_min)
    repo.conn.commit()


def test_empty_night_returns_a_bare_summary():
    """No rows must degrade to the old stub's contract, never a confidently wrong summary."""
    with _repo() as repo:
        ns = reconstruct_night_summary(repo, "2026-06-23")
        assert ns.date == "2026-06-23"
        assert ns.total_sleep_min is None and ns.deep_min is None and ns.wake_events is None


def test_stage_minutes_and_efficiency_from_persisted_frames():
    with _repo() as repo:
        # 60 light + 30 deep + 20 rem + 10 awake = 120 min of sleep period, after 10 min latency
        _night(repo, stages=["light"] * 60 + ["deep"] * 30 + ["rem"] * 20 + ["awake"] * 10)
        ns = reconstruct_night_summary(repo, "2026-06-23")

        assert ns.sleep_onset_latency_min == 10.0
        assert ns.light_min == 60.0 and ns.deep_min == 30.0 and ns.rem_min == 20.0
        assert ns.waso_min == 10.0
        assert ns.total_sleep_min == 110.0          # awake is NOT sleep
        # in bed 130 min (10 latency + 120 scored) -> 110/130
        assert abs(ns.sleep_efficiency - 110.0 / 130.0) < 0.02
        assert ns.avg_hr == 58.0 and ns.avg_hrv == 60.0


def test_paired_ticks_are_not_double_counted():
    """The daemon emits a control tick AND a telemetry tick, so samples arrive in close pairs and
    the MEDIAN inter-sample gap is about double the true mean spacing. Crediting every sample a
    uniform median-gap epoch scored a real 385-minute night as 732 minutes of sleep; each sample
    must instead be credited the actual gap to the next one."""
    with _repo() as repo:
        t = datetime(2026, 6, 23, 23, 0)
        # 60 minutes of MAINTENANCE, emitted as pairs 2 s apart once a minute
        for _ in range(60):
            _write(repo, t, stage="light", state="maintenance")
            _write(repo, t + timedelta(seconds=2), stage="light", state="maintenance")
            t += timedelta(minutes=1)
        repo.conn.commit()

        ns = reconstruct_night_summary(repo, "2026-06-23")
        span = (ns.wake_time - ns.bedtime).total_seconds() / 60.0
        assert 58.0 <= ns.total_sleep_min <= span + 1.0, (
            f"scored {ns.total_sleep_min} min of sleep across a {span:.1f} min night")
        assert ns.sleep_efficiency <= 1.0


def test_awakenings_come_from_sustained_awake_runs():
    """``raw_samples.wake_event`` is written 0 on every row (it was never wired up), so counting
    its rising edges always yielded 0 even on nights the arousal detector scored 10/10 against
    the user's own report. Sustained AWAKE runs are the standard WASO definition."""
    with _repo() as repo:
        # four separate awakenings: three multi-minute, plus one lasting a single 1-minute
        # sample, which DOES clear the >= 1 min bar and is a real brief awakening
        stages = (["light"] * 20 + ["awake"] * 5 + ["light"] * 20 + ["awake"] * 4
                  + ["light"] * 20 + ["awake"] * 6 + ["light"] * 10 + ["awake"] + ["light"] * 5)
        _night(repo, stages=stages)
        assert reconstruct_night_summary(repo, "2026-06-23").wake_events == 4


def test_sub_minute_arousals_are_not_counted_as_awakenings():
    """A brief stir is a micro-arousal, not a WASO awakening."""
    with _repo() as repo:
        t = datetime(2026, 6, 23, 23, 0)
        for i in range(120):  # 20 min of sleep sampled every 10 s
            _write(repo, t, stage=("awake" if i in (40, 80) else "light"), state="maintenance")
            t += timedelta(seconds=10)
        repo.conn.commit()
        assert reconstruct_night_summary(repo, "2026-06-23").wake_events == 0


def test_post_wake_morning_samples_are_excluded():
    """Scoring must stop at final wake. Samples keep landing under the same ``night_date`` all
    morning, and counting them let a night read ~9% 'awake' when the user reported ~8 awakenings
    (and, before the controller's out-of-bed guard, filed fake DEEP under that night too)."""
    with _repo() as repo:
        _night(repo, stages=["light"] * 60, latency_min=0.0)
        # user is up: hours of IDLE samples under the SAME night_date
        t = datetime(2026, 6, 24, 7, 0)
        for _ in range(240):
            _write(repo, t, stage="deep", state="idle")
            t += timedelta(minutes=1)
        repo.conn.commit()

        ns = reconstruct_night_summary(repo, "2026-06-23")
        assert ns.deep_min == 0.0, "counted post-wake morning samples as deep sleep"
        assert ns.total_sleep_min == 60.0


def test_merge_prefers_a_richer_upstream_field():
    base = NightSummary(date="2026-06-23", total_sleep_min=100.0, deep_min=20.0)
    merged = merge_night_summary(base, NightSummary(date="2026-06-23", deep_min=45.0))
    assert merged.deep_min == 45.0          # upstream wins where it has a value
    assert merged.total_sleep_min == 100.0  # reconstruction survives where upstream is None


def test_merge_with_the_stub_adapter_changes_nothing():
    """Today's adapters return ``NightSummary(date=...)`` with every field None."""
    base = NightSummary(date="2026-06-23", total_sleep_min=123.0)
    assert merge_night_summary(base, NightSummary(date="2026-06-23")).total_sleep_min == 123.0
