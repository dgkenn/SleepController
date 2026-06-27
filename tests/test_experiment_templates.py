"""Curated n-of-1 templates + a-priori power estimate."""

import tempfile

import pytest

from sleepctl.experiment_templates import (create_from_template, estimate_nights_needed,
                                           list_templates, template)
from sleepctl.experiments import assign_arm
from sleepctl.models import NightSummary
from sleepctl.storage.repository import Repository


@pytest.fixture
def repo():
    r = Repository(tempfile.mktemp(suffix=".db"))
    yield r
    r.close()


def test_templates_are_valid_specs():
    keys = {t["key"] for t in list_templates()}
    assert {"cooler_deep", "warm_prophylaxis_am", "stability_lockdown"} <= keys
    for t in list_templates():
        spec = template(t["key"])
        assert spec["metric"] and spec["arm_a"]["label"] and spec["arm_b"]["label"]


def test_create_from_template_launches_a_real_experiment(repo):
    exp = create_from_template(repo, "warm_prophylaxis_am", period=3, washout=1)
    assert exp["status"] == "active" and exp["metric"] == "wake_events"
    # the schedule actually assigns counterbalanced arms
    slots = [assign_arm(repo, exp["id"], f"2026-07-{d:02d}") for d in range(1, 9)]
    assert "a" in slots and "b" in slots and "washout" in slots


def test_power_estimate_scales_with_variability(repo):
    # noisy wake-event history -> needs more nights to detect a 1-event effect
    for i, we in enumerate([0, 4, 1, 5, 0, 6, 2, 3, 1, 5]):
        repo.save_night_summary(NightSummary(date=f"2026-06-{10+i:02d}", wake_events=we))
    est = estimate_nights_needed(repo, "wake_events", target_effect=1.0)
    assert est["sd"] is not None and est["nights_per_arm"] >= 3
    assert est["total_nights"] == (est["suggested_period"] * 2 + est["washout"] * 2) * est["suggested_cycles"]
    assert est["suggested_cycles"] >= 2


def test_power_estimate_handles_thin_history(repo):
    est = estimate_nights_needed(repo, "wake_events", target_effect=1.0)
    assert est["nights_per_arm"] is None and "not enough history" in est["note"]
