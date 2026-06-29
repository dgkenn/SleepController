"""Revealed-preference personalization of the perfect-sleep weights (evidence prior + your data)."""

import tempfile

import pytest

from sleepctl.benchmarks import NightMode, targets_for
from sleepctl.learning.perfect_weights import learn_perfect_weights, personalized_targets
from sleepctl.models import ContextRecord, NightSummary
from sleepctl.storage.repository import Repository


@pytest.fixture
def repo():
    r = Repository(tempfile.mktemp(suffix=".db"))
    yield r
    r.close()


def test_thin_data_returns_the_evidence_prior(repo):
    prior = targets_for(NightMode.NORMAL).weights
    assert learn_perfect_weights(repo, NightMode.NORMAL) == prior


def _seed_deep_drives_quality(repo, n=18):
    # construct nights where felt quality tracks DEEP sleep (and nothing else moves much)
    for i in range(n):
        deep = 60 + (i % 6) * 12          # deep varies 60..120
        date = f"2026-05-{i+1:02d}"
        repo.save_night_summary(NightSummary(
            date=date, total_sleep_min=470, deep_min=deep, rem_min=95,
            sleep_efficiency=0.92, sleep_onset_latency_min=12, wake_events=2, waso_min=14))
        repo.save_context(ContextRecord(date=date, subjective_quality=float(deep)))  # tracks deep


def test_metric_that_predicts_felt_quality_is_upweighted(repo):
    _seed_deep_drives_quality(repo)
    prior = targets_for(NightMode.NORMAL).weights
    learned = learn_perfect_weights(repo, NightMode.NORMAL)
    # deep predicts how this person feels -> its weight rises relative to the prior share
    assert learned["deep"] / sum(learned.values()) > prior["deep"] / sum(prior.values())


def test_weights_stay_positive_normalized_and_continuity_floored(repo):
    _seed_deep_drives_quality(repo)
    prior = targets_for(NightMode.NORMAL).weights
    learned = learn_perfect_weights(repo, NightMode.NORMAL)
    assert all(v > 0 for v in learned.values())
    assert sum(learned.values()) == pytest.approx(sum(prior.values()), abs=1e-3)
    # maintenance metrics keep at least 60% of their prior share -> never learned away
    for k in ("waso", "awakenings"):
        assert learned[k] >= 0.6 * prior[k] - 1e-9


def test_personalized_targets_wraps_weights(repo):
    _seed_deep_drives_quality(repo)
    t = personalized_targets(repo, NightMode.NORMAL)
    assert t.weights == learn_perfect_weights(repo, NightMode.NORMAL)
    # personalized_targets now also learns the ideal LEVELS from the morning survey: this seed has
    # deep driving felt quality, so the personal deep ideal moves up (bounded near evidence).
    base = targets_for(NightMode.NORMAL).deep_pct_ideal
    assert base <= t.deep_pct_ideal <= base + 0.045
