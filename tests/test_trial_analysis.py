"""Tests for trial analysis.

The properties worth pinning are the ones that stop a null result being read as a win: only
locked nights count, strata are respected, and thin/confounded data says so loudly.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta

from sleepctl.eval import trial, trial_analysis
from sleepctl.storage.repository import Repository


@contextmanager
def _repo():
    d = tempfile.mkdtemp()
    try:
        r = Repository(os.path.join(d, "t.db"))
        try:
            yield r
        finally:
            r.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _night(repo, date, policy, night_type, *, quality=6.0, grog=4.0, perf=6.0,
           lock=True, version="v1"):
    repo.conn.execute(
        "INSERT INTO trial_assignments (night_date, policy, block_id, block_index, night_type,"
        " controller_version, seed, assigned_ts, outcome_locked) VALUES (?,?,?,?,?,?,?,?,?)",
        (date, policy, f"{night_type}:0", 0, night_type, version, "s", date,
         1 if lock else 0))
    repo.conn.execute(
        "INSERT INTO context (date, subjective_quality, grogginess, daytime_performance) "
        "VALUES (?,?,?,?)", (date, quality, grog, perf))
    repo.conn.commit()


def _dates(n, start="2026-09-01"):
    d0 = datetime.fromisoformat(start)
    return [(d0 + timedelta(days=i)).date().isoformat() for i in range(n)]


# ------------------------------------------------------------------ eligibility
def test_only_locked_nights_are_analyzed():
    """An unlocked night was either not rated yet, or rated after the arm was revealed. Either
    way it cannot contribute to the endpoint."""
    with _repo() as repo:
        ds = _dates(4)
        _night(repo, ds[0], trial.POLICY_A_STATIC, "work", lock=True)
        _night(repo, ds[1], trial.POLICY_B_REACTIVE, "work", lock=False)
        res = trial_analysis.analyze(repo.conn)
        assert res["n_nights"] == 1
        assert trial.POLICY_B_REACTIVE not in res["per_policy"]


def test_a_night_with_no_outcome_recorded_is_excluded():
    with _repo() as repo:
        d = _dates(1)[0]
        repo.conn.execute(
            "INSERT INTO trial_assignments (night_date, policy, night_type, outcome_locked) "
            "VALUES (?,?,?,1)", (d, trial.POLICY_A_STATIC, "work"))
        repo.conn.commit()
        assert trial_analysis.analyze(repo.conn)["n_nights"] == 0


# ------------------------------------------------------------------ effect direction
def test_a_real_improvement_shows_up_as_a_negative_grogginess_contrast():
    """Lower grogginess is better, so a genuinely better arm must produce a NEGATIVE contrast."""
    with _repo() as repo:
        ds = _dates(20)
        for i, d in enumerate(ds[:10]):
            _night(repo, d, trial.POLICY_A_STATIC, "work", grog=6.0)
        for i, d in enumerate(ds[10:]):
            _night(repo, d, trial.POLICY_C_STABILIZED, "work", grog=3.0)
        res = trial_analysis.analyze(repo.conn)
        c = res["contrasts"][f"{trial.POLICY_C_STABILIZED}_vs_{trial.POLICY_A_STATIC}"]
        assert c["diff"] < 0
        assert abs(c["diff"] - (-3.0)) < 1e-6


def test_no_difference_produces_a_contrast_near_zero():
    with _repo() as repo:
        ds = _dates(20)
        for d in ds[:10]:
            _night(repo, d, trial.POLICY_A_STATIC, "work", grog=5.0)
        for d in ds[10:]:
            _night(repo, d, trial.POLICY_C_STABILIZED, "work", grog=5.0)
        res = trial_analysis.analyze(repo.conn)
        c = res["contrasts"][f"{trial.POLICY_C_STABILIZED}_vs_{trial.POLICY_A_STATIC}"]
        assert abs(c["diff"]) < 1e-9


def test_a_shift_imbalance_cannot_masquerade_as_a_policy_effect():
    """THE reason contrasts are stratum-weighted. Here the arms are identical within each
    stratum and only the STRATUM differs -- a raw pooled comparison would invent an effect."""
    with _repo() as repo:
        ds = _dates(40)
        i = 0
        # work nights are worse for BOTH arms; off nights better for BOTH arms
        for _ in range(10):
            _night(repo, ds[i], trial.POLICY_A_STATIC, "work", grog=7.0); i += 1
            _night(repo, ds[i], trial.POLICY_C_STABILIZED, "work", grog=7.0); i += 1
        for _ in range(10):
            _night(repo, ds[i], trial.POLICY_A_STATIC, "off", grog=2.0); i += 1
            _night(repo, ds[i], trial.POLICY_C_STABILIZED, "off", grog=2.0); i += 1
        res = trial_analysis.analyze(repo.conn)
        c = res["contrasts"][f"{trial.POLICY_C_STABILIZED}_vs_{trial.POLICY_A_STATIC}"]
        assert abs(c["diff"]) < 1e-9, c


# ------------------------------------------------------------------ honesty
def test_thin_arms_are_flagged_as_not_interpretable():
    with _repo() as repo:
        ds = _dates(4)
        _night(repo, ds[0], trial.POLICY_A_STATIC, "work")
        _night(repo, ds[1], trial.POLICY_C_STABILIZED, "work")
        res = trial_analysis.analyze(repo.conn)
        assert any("fewer than 6 nights" in w for w in res["warnings"])


def test_a_code_change_inside_the_trial_is_flagged_as_confounding():
    """A controller change mid-trial confounds the arms with time -- silently pooling across it
    would attribute a code effect to a policy."""
    with _repo() as repo:
        ds = _dates(20)
        for d in ds[:10]:
            _night(repo, d, trial.POLICY_A_STATIC, "work", version="v1")
        for d in ds[10:]:
            _night(repo, d, trial.POLICY_C_STABILIZED, "work", version="v2")
        res = trial_analysis.analyze(repo.conn)
        assert any("controller versions" in w for w in res["warnings"])


def test_composite_requires_every_component():
    """A composite quietly built from 2 of 3 components is not the pre-registered endpoint."""
    refs = {"subjective_quality": {"mean": 5.0, "sd": 1.0},
            "grogginess": {"mean": 5.0, "sd": 1.0},
            "daytime_performance": {"mean": 5.0, "sd": 1.0}}
    assert trial_analysis.composite_y(
        {"subjective_quality": 6.0, "grogginess": 4.0, "daytime_performance": 6.0}, refs) is not None
    assert trial_analysis.composite_y(
        {"subjective_quality": 6.0, "grogginess": None, "daytime_performance": 6.0}, refs) is None


def test_zero_spread_reference_does_not_divide_by_zero():
    refs = {k: {"mean": 5.0, "sd": 0.0} for k in
            ("subjective_quality", "grogginess", "daytime_performance")}
    assert trial_analysis.composite_y(
        {"subjective_quality": 5.0, "grogginess": 5.0, "daytime_performance": 5.0}, refs) is None


def test_empty_trial_reports_cleanly():
    with _repo() as repo:
        res = trial_analysis.analyze(repo.conn)
        assert res["n_nights"] == 0
        assert res["warnings"]
        assert "TRIAL ANALYSIS" in trial_analysis.format_report(res)
