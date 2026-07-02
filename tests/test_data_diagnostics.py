"""Tests for the data + learning + config health doctor (``sleepctl.diagnostics``).

Covers: an empty database never crashes and reports NEEDS_DATA; a seeded database populates
every check and moves past NEEDS_DATA; an out-of-bounds learned setpoint is caught as a
``fail``; and the CLI ``doctor`` command runs end-to-end and prints a report without error.
"""

from __future__ import annotations

import io
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.diagnostics import data_diagnostics
from sleepctl.models import NightSummary, SetpointProfile
from sleepctl.storage.repository import Repository

_REQUIRED_KEYS = {"id", "title", "status", "detail", "remedy"}
_VALID_STATUSES = {"ok", "warn", "fail", "info"}


def _night(date, deep=90.0, rem=100.0, wake=1, eff=0.9, hrv=60.0, outcome=0.6):
    total = deep + rem + 220
    return NightSummary(
        date=date, total_sleep_min=total, deep_min=deep, rem_min=rem,
        light_min=total - deep - rem, wake_events=wake, waso_min=15.0,
        sleep_efficiency=eff, avg_hr=58.0, avg_hrv=hrv, outcome_score=outcome,
    )


def test_empty_db_needs_data_and_never_crashes():
    repo = Repository(":memory:")
    cfg = AppConfig.default()
    report = data_diagnostics(repo, cfg)
    repo.close()

    assert report["verdict"] == "NEEDS_DATA"
    assert isinstance(report["headline"], str) and report["headline"]
    assert isinstance(report["checks"], list) and len(report["checks"]) >= 8
    for c in report["checks"]:
        assert _REQUIRED_KEYS.issubset(c.keys())
        assert c["status"] in _VALID_STATUSES


def test_data_diagnostics_never_raises_with_no_cfg():
    """cfg=None must fall back to AppConfig.default() internally, never raise."""
    repo = Repository(":memory:")
    report = data_diagnostics(repo, None)
    repo.close()
    assert report["verdict"] in ("HEALTHY", "DEGRADED", "NEEDS_DATA")


def test_seeded_db_populates_checks_and_moves_past_needs_data():
    repo = Repository(tempfile.mktemp(suffix=".db"))
    cfg = AppConfig.default()

    start = datetime(2026, 6, 20)
    for i in range(6):
        date = (start + timedelta(days=i)).date().isoformat()
        repo.save_night_summary(_night(date, wake=1 if i < 3 else 2, outcome=0.5 + 0.02 * i))

    sp = cfg.default_setpoints()
    sp.version = 1
    sp.source = "policy"
    repo.save_setpoints(sp)

    report = data_diagnostics(repo, cfg)
    repo.close()

    assert report["verdict"] != "NEEDS_DATA"
    by_id = {c["id"]: c for c in report["checks"]}
    assert set(by_id) == {
        "db", "data_volume", "data_completeness", "learner_maturity",
        "calibration", "setpoints_sane", "config_sane", "outcome_trend",
    }
    # db check should see real row counts now
    assert "nightly_summaries=6" in by_id["db"]["detail"]
    # setpoints saved and within bounds -> ok
    assert by_id["setpoints_sane"]["status"] == "ok"
    # outcome_score is trending across the 6 nights -> should have a trend reading
    assert "outcome_score trend" in by_id["outcome_trend"]["detail"]


def test_setpoint_out_of_bounds_is_flagged_as_fail():
    repo = Repository(tempfile.mktemp(suffix=".db"))
    cfg = AppConfig.default()

    bad = SetpointProfile(
        neutral_f=70.0, deep_bias_f=999.0,  # wildly out of the (58, 78) knob bound
        rem_warm_offset_f=1.5, wake_ramp_f=74.0, composite_bed_weight=0.75,
        version=1, source="ml",
    )
    repo.save_setpoints(bad)

    report = data_diagnostics(repo, cfg)
    repo.close()

    by_id = {c["id"]: c for c in report["checks"]}
    assert by_id["setpoints_sane"]["status"] == "fail"
    assert "deep_bias_f" in by_id["setpoints_sane"]["detail"]
    assert report["verdict"] == "DEGRADED"


def test_cli_doctor_runs_and_prints_without_error(monkeypatch):
    """Smoke test: invoke the CLI's doctor command directly (argparse Namespace), both in
    plain-text and --json mode, and confirm it prints a report without raising."""
    from sleepctl.cli import build_parser

    db_path = tempfile.mktemp(suffix=".db")
    repo = Repository(db_path)
    repo.save_night_summary(_night("2026-06-30"))
    repo.close()

    # Ensure no real dashboard API is contacted / no token confusion in this test env.
    monkeypatch.delenv("DIAG_TOKEN", raising=False)

    parser = build_parser()

    args = parser.parse_args(["doctor", "--db", db_path])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    out = buf.getvalue()
    assert rc in (0, 1)
    assert "LIVE RUNTIME" in out
    assert "Data/learning health" in out

    args_json = parser.parse_args(["doctor", "--db", db_path, "--json"])
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        rc2 = args_json.func(args_json)
    out_json = buf2.getvalue()
    assert rc2 in (0, 1)
    import json as _json
    parsed = _json.loads(out_json)
    assert parsed["verdict"] in ("HEALTHY", "DEGRADED", "NEEDS_DATA")
    assert "live_runtime" in parsed
    assert "checks" in parsed
