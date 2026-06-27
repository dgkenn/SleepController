"""Nightly intelligence report: synthesis + explainability."""

import tempfile
from datetime import datetime

import pytest

from sleepctl.models import (Intervention, NightSummary, ControllerState, CorrectionAction,
                             SetpointProfile)
from sleepctl.night_report import build_night_report
from sleepctl.storage.repository import Repository


@pytest.fixture
def repo():
    r = Repository(tempfile.mktemp(suffix=".db"))
    yield r
    r.close()


def _night(date, tst=400, deep=80, rem=90, wake=2, waso=18, eff=92, hrv=55):
    return NightSummary(date=date, total_sleep_min=tst, deep_min=deep, rem_min=rem,
                        light_min=tst - deep - rem, wake_events=wake, waso_min=waso,
                        sleep_efficiency=eff, avg_hrv=hrv, outcome_score=0.7)


def test_report_empty_repo_is_graceful(repo):
    rep = build_night_report(repo)
    assert rep["have_data"] is False
    assert "Not enough data" in rep["narrative"]


def test_report_synthesizes_sections_and_explains_actions(repo):
    for d in ("2026-06-24", "2026-06-25", "2026-06-26"):
        repo.save_night_summary(_night(d))
    repo.save_setpoints(SetpointProfile(neutral_f=70.0, deep_bias_f=-2.0, rem_warm_offset_f=0.5,
                                        wake_ramp_f=1.0, composite_bed_weight=0.5,
                                        version=3, source="ml"))
    # the controller's actions, with WHY — the explainability layer
    repo.log_intervention(Intervention(
        timestamp=datetime(2026, 6, 26, 3, 30), state=ControllerState.MAINTENANCE,
        action=CorrectionAction.COOLER, magnitude_f=-1.5,
        reason="running_warm pre-empt", held=True), night_date="2026-06-26")
    repo.log_intervention(Intervention(
        timestamp=datetime(2026, 6, 26, 4, 10), state=ControllerState.MAINTENANCE,
        action=CorrectionAction.COOLER, magnitude_f=-1.0,
        reason="circadian_nadir pre-empt", held=True), night_date="2026-06-26")

    rep = build_night_report(repo)
    assert rep["have_data"] is True
    # all the synthesis sections are present
    for key in ("readiness", "what_happened", "what_i_did", "preemption",
                "what_i_learned", "suggestions", "narrative"):
        assert key in rep
    # explainability: the actions and their reasons surface
    did = rep["what_i_did"]
    assert did["n_actions"] == 2 and did["held"] == 2
    reasons = {r["reason"] for r in did["top_reasons"]}
    assert "running_warm pre-empt" in reasons
    # learned setpoint is reported with provenance
    sp = rep["what_i_learned"]["setpoint"]
    assert sp["version"] == 3 and sp["source"] == "ml"
    # narrative mentions the readiness band and that it made adjustments
    assert "adjustment" in rep["narrative"].lower()
