"""Learning the personal awakening phenotype from logged wake events.

``build_wake_profile`` is what turns "you tend to wake around 3am" into a recurring window the
lead-time learner pre-cools ahead of. It is called on every daemon startup and nightly close-out,
and had no tests. Its conservatism is the point: a phantom recurring time would make the
controller cool the bed every night at an hour the user does not actually wake, which is a way to
CAUSE awakenings rather than prevent them.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from sleepctl.ml.wake_profile import build_wake_profile
from sleepctl.storage.repository import Repository


@pytest.fixture()
def repo(tmp_path):
    r = Repository(str(tmp_path / "wp.db"))
    yield r
    r.close()


def _wake(repo, when: datetime, bed_temp=None):
    repo.conn.execute(
        "INSERT INTO raw_samples (ts, night_date, bed_temp_f, wake_event) VALUES (?,?,?,1)",
        (when.isoformat(), when.date().isoformat(), bed_temp))
    repo.conn.commit()


def _nightly_wake_at(repo, hour, minute, nights, bed_temp=None, start_day=1):
    """The same clock time on ``nights`` distinct nights."""
    for i in range(nights):
        _wake(repo, datetime(2026, 7, start_day + i, hour, minute), bed_temp)


# ------------------------------------------------------------------ the conservative floor
def test_no_history_returns_the_evidence_preset(repo):
    prof = build_wake_profile(repo)
    assert prof.source == "preset"
    assert prof.awakening_minutes == []
    assert prof.warm_temp_threshold_f is None


def test_a_single_night_never_creates_a_recurring_time(repo):
    """One odd night must not become a nightly pre-cool. This is the guard that matters most."""
    _nightly_wake_at(repo, 3, 15, nights=1)
    assert build_wake_profile(repo).awakening_minutes == []


def test_two_nights_at_the_same_time_is_enough(repo):
    _nightly_wake_at(repo, 3, 15, nights=2)
    prof = build_wake_profile(repo)
    assert prof.awakening_minutes, "a repeated clock-time should be learned"
    assert any(abs(m - 195) <= 30 for m in prof.awakening_minutes), prof.awakening_minutes


def test_the_night_threshold_is_configurable(repo):
    _nightly_wake_at(repo, 3, 15, nights=2)
    assert build_wake_profile(repo, min_cluster_nights=3).awakening_minutes == []
    assert build_wake_profile(repo, min_cluster_nights=2).awakening_minutes


def test_many_wakes_on_ONE_night_do_not_count_as_recurring(repo):
    """Distinct NIGHTS, not distinct samples — a single restless night is not a pattern."""
    base = datetime(2026, 7, 1, 3, 0)
    for i in range(20):
        _wake(repo, base + timedelta(seconds=30 * i))
    assert build_wake_profile(repo).awakening_minutes == []


def test_wakes_scattered_across_the_night_do_not_cluster(repo):
    for i, hour in enumerate([23, 0, 1, 2, 3, 4, 5]):
        _wake(repo, datetime(2026, 7, 1 + i, hour, 5))
    assert build_wake_profile(repo).awakening_minutes == []


# ------------------------------------------------------------------ binning
def test_nearby_times_land_in_the_same_bin(repo):
    """3:05 and 3:25 are the same vulnerability, not two."""
    _wake(repo, datetime(2026, 7, 1, 3, 5))
    _wake(repo, datetime(2026, 7, 2, 3, 25))
    prof = build_wake_profile(repo)
    assert len(prof.awakening_minutes) == 1


def test_recurring_times_are_sorted(repo):
    _nightly_wake_at(repo, 5, 0, nights=2, start_day=1)
    _nightly_wake_at(repo, 1, 0, nights=2, start_day=10)
    mins = build_wake_profile(repo).awakening_minutes
    assert mins == sorted(mins)


# ------------------------------------------------------------------ the warm threshold
def test_the_warm_threshold_needs_enough_samples(repo):
    for i in range(4):
        _wake(repo, datetime(2026, 7, 1 + i, 3, 0), bed_temp=76.0)
    assert build_wake_profile(repo).warm_temp_threshold_f is None


def test_the_warm_threshold_is_a_lower_quartile_not_a_mean(repo):
    """It answers 'even at my COOLER awakenings the bed was at least this warm', so one very hot
    night must not drag the threshold up out of usefulness."""
    temps = [72.0, 73.0, 74.0, 75.0, 76.0, 95.0]
    for i, t in enumerate(temps):
        _wake(repo, datetime(2026, 7, 1 + i, 3, 0), bed_temp=t)
    thr = build_wake_profile(repo).warm_temp_threshold_f
    assert thr is not None
    assert thr < sum(temps) / len(temps), "an outlier hot night must not set the threshold"
    assert 72.0 <= thr <= 74.0, thr


def test_missing_bed_temperatures_are_skipped_not_counted_as_zero(repo):
    for i in range(6):
        _wake(repo, datetime(2026, 7, 1 + i, 3, 0), bed_temp=None)
    assert build_wake_profile(repo).warm_temp_threshold_f is None


# ------------------------------------------------------------------ what is preserved
def test_structural_vulnerabilities_survive_learning(repo):
    """The cycle/circadian windows are evidence, not personal history — learning personal times
    must never drop them, or the controller loses its baseline vigilance."""
    from sleepctl.controller.wake_risk import WakeProfile

    preset = WakeProfile.evidence_default()
    _nightly_wake_at(repo, 3, 15, nights=3, bed_temp=76.0)
    prof = build_wake_profile(repo)
    assert prof.cycle_len_min == preset.cycle_len_min
    assert prof.cycle_boundary_window_min == preset.cycle_boundary_window_min
    assert prof.back_half_after_cycle == preset.back_half_after_cycle
    assert prof.circadian_window == preset.circadian_window


def test_source_reports_whether_anything_was_actually_learned(repo):
    assert build_wake_profile(repo).source == "preset"
    _nightly_wake_at(repo, 3, 15, nights=3, bed_temp=76.0)
    assert build_wake_profile(repo).source != "preset"


# ------------------------------------------------------------------ robustness
def test_a_broken_repo_degrades_to_the_preset(repo):
    class Boom:
        class conn:
            @staticmethod
            def execute(*a, **k):
                raise RuntimeError("db gone")

    assert build_wake_profile(Boom()).source == "preset"


def test_unparseable_timestamps_are_skipped(repo):
    repo.conn.execute(
        "INSERT INTO raw_samples (ts, night_date, bed_temp_f, wake_event) VALUES (?,?,?,1)",
        ("not-a-timestamp", "2026-07-01", 75.0))
    repo.conn.commit()
    _nightly_wake_at(repo, 3, 15, nights=2)
    prof = build_wake_profile(repo)          # must not raise
    assert prof.awakening_minutes
