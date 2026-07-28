"""The two readiness checks: personal calibration, and awakening-pre-emption timing.

Both exist to make a SILENT condition visible. Nothing is broken when calibration is missing or
when pre-emption is timing-limited -- every other check stays green -- which is exactly why they
need their own line in the battery.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import diagnostics


@pytest.fixture()
def repo(tmp_path):
    from sleepctl.storage.repository import Repository
    from app import db as app_db

    r = Repository(str(tmp_path / "readiness.db"), check_same_thread=False)
    r.conn.executescript(app_db._DASHBOARD_DDL)
    app_db._apply_migrations(r.conn)
    r.conn.commit()
    yield r
    r.close()


# ------------------------------------------------------------------ calibration
def test_calibration_reports_all_three_missing_on_a_fresh_box(repo):
    c = diagnostics._check_calibration(repo)
    assert c["status"] == "info"
    assert "3 of 3 missing" in c["detail"]
    for expected in ("self-test", "comfort sweep", "check-ins"):
        assert expected in c["detail"]
    assert "self-test" in c["remedy"]


def test_calibration_never_degrades_the_overall_verdict(repo, tmp_path):
    """A setup step the user can only do in bed must not pin the dashboard to DEGRADED."""
    report = diagnostics.run_diagnostics(repo, run_dir=str(tmp_path))
    cal = next(c for c in report["checks"] if c["id"] == "calibration")
    assert cal["status"] == "info", "calibration must never be warn/fail"


def test_calibration_counts_what_is_present(repo):
    repo.conn.execute(
        "INSERT INTO thermal_calibration (ts, cool_lag_min, cool_f_per_min, source)"
        " VALUES (?,?,?,?)", (datetime.now().isoformat(), 6.0, 0.4, "self_test"))
    for i in range(5):
        repo.conn.execute(
            "INSERT INTO context (date, subjective_quality) VALUES (?,?)",
            (f"2026-07-0{i + 1}", 7.0))
    repo.conn.commit()

    c = diagnostics._check_calibration(repo)
    assert "1 of 3 missing" in c["detail"]
    assert "comfort sweep" in c["detail"]           # the one still missing
    assert "thermal self-test" in c["detail"]       # listed under "have"
    assert "5 morning check-ins" in c["detail"]


def test_calibration_goes_ok_when_all_three_land(repo):
    now = datetime.now().isoformat()
    repo.conn.execute(
        "INSERT INTO thermal_calibration (ts, cool_lag_min, cool_f_per_min, source)"
        " VALUES (?,?,?,?)", (now, 6.0, 0.4, "self_test"))
    repo.conn.execute(
        "INSERT INTO comfort_profile (ts, neutral_f, source) VALUES (?,?,?)",
        (now, 69.0, "sweep"))
    for i in range(5):
        repo.conn.execute("INSERT INTO context (date, subjective_quality) VALUES (?,?)",
                          (f"2026-07-0{i + 1}", 7.0))
    repo.conn.commit()

    c = diagnostics._check_calibration(repo)
    assert c["status"] == "ok" and c["remedy"] is None


def test_calibration_survives_a_broken_repo():
    class Boom:
        class conn:
            @staticmethod
            def execute(*a, **k):
                raise RuntimeError("db gone")

        def get_thermal_calibration(self):
            raise RuntimeError("nope")

        def get_comfort_profile(self):
            raise RuntimeError("nope")

    c = diagnostics._check_calibration(Boom())
    assert c["status"] == "info" and "3 of 3 missing" in c["detail"]


# ------------------------------------------------------------------ pre-emption timing
def _seed_failed_precools(repo, n, arrival_at, wake_at):
    """n pre-cools where the bed only starts moving at ``arrival_at`` and a wake lands at ``wake_at``."""
    now = datetime.now()
    for k in range(n):
        start = now - timedelta(hours=8 - k)
        repo.log_precool_event(start.date().isoformat(), start, "circadian", 12.0, 20.0)
        for i in range(-5, 30):
            t = start + timedelta(minutes=i)
            temp = 72.0 if i < arrival_at else 72.0 - 0.6 * (i - arrival_at + 1)
            repo.conn.execute(
                "INSERT INTO raw_samples (ts, night_date, bed_temp_f, wake_event) VALUES (?,?,?,?)",
                (t.isoformat(), start.date().isoformat(), temp, 1 if i == wake_at else 0))
    repo.conn.commit()
    repo.resolve_precool_events()


def test_timing_check_is_info_with_no_data(repo):
    c = diagnostics._check_prevention_timing(repo)
    assert c["status"] == "info"


def test_timing_limited_is_surfaced_as_a_warning_with_a_lead_remedy(repo):
    _seed_failed_precools(repo, n=5, arrival_at=12, wake_at=4)
    c = diagnostics._check_prevention_timing(repo)
    assert c["status"] == "warn"
    assert "BEFORE the bed had moved" in c["detail"]
    assert "lead" in c["remedy"]


def test_dose_limited_is_not_a_warning(repo):
    """The lead is fine here -- flagging it would send the user to fix the wrong knob."""
    _seed_failed_precools(repo, n=5, arrival_at=1, wake_at=20)
    c = diagnostics._check_prevention_timing(repo)
    assert c["status"] == "ok"
    assert "AFTER the bed had arrived" in c["detail"]


def test_a_bed_that_never_moves_fails_and_blames_the_water_loop(repo):
    now = datetime.now()
    for k in range(5):
        start = now - timedelta(hours=8 - k)
        repo.log_precool_event(start.date().isoformat(), start, "circadian", 12.0, 20.0)
        for i in range(-5, 30):
            t = start + timedelta(minutes=i)
            repo.conn.execute(
                "INSERT INTO raw_samples (ts, night_date, bed_temp_f, wake_event) VALUES (?,?,?,?)",
                (t.isoformat(), start.date().isoformat(), 72.0, 1 if i == 6 else 0))
    repo.conn.commit()
    repo.resolve_precool_events()

    c = diagnostics._check_prevention_timing(repo)
    assert c["status"] == "fail"
    assert "water loop" in c["remedy"]


def test_both_checks_are_registered_in_the_battery(repo, tmp_path):
    report = diagnostics.run_diagnostics(repo, run_dir=str(tmp_path))
    ids = {c["id"] for c in report["checks"]}
    assert {"calibration", "prevention_timing"} <= ids
