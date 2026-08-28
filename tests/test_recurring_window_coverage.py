"""The recurring-wake-window signal saturates for exactly the people it is meant to help.

Each learned recurring time claims 2*cluster_window_min minutes (50 by default), and the times
are learned FROM the user's own awakenings -- so waking more produces more windows, which tile
the night until the signal is permanently on and stops discriminating.

Measured on 2026-08-27: `recurring_wake_window` was present on 294 of 294 pre-empting ticks,
and its +0.20 supplied the deciding margin on ~227 of them (only 67 would have reached the 0.5
risk threshold without it).
"""
from datetime import datetime

from sleepctl.controller.wake_risk import WakeProfile


def _p(times, **kw):
    return WakeProfile(awakening_minutes=list(times), **kw)


def test_total_coverage_is_bounded_however_many_windows_are_learned():
    for n in (4, 8, 12, 30):
        p = _p(range(0, n * 45, 45))
        assert 2 * p.effective_cluster_half_width() * n <= p.max_recurring_coverage_min + 1e-6


def test_a_few_windows_keep_their_full_width():
    """The cap must not penalise a user with a genuinely sharp, well-identified wake time."""
    p = _p([180, 300])
    assert p.effective_cluster_half_width() == float(p.cluster_window_min)


def test_many_windows_shrink_rather_than_disappear():
    """Shrinking keeps the signal targeted; deleting windows would throw away real evidence."""
    p = _p(range(0, 12 * 60, 60))
    h = p.effective_cluster_half_width()
    assert 5.0 <= h < p.cluster_window_min


def test_a_profile_too_diffuse_to_identify_anything_switches_the_signal_off():
    """If the budget cannot afford every window even at the minimum useful width, the profile
    has stopped naming moments and become a description of a restless sleeper. Contributing
    +0.20 on that basis is worse than contributing nothing."""
    p = _p(range(0, 1440, 20))          # absurdly many learned times
    assert p.effective_cluster_half_width() == 0.0
    assert p.near_recurring_time(datetime(2026, 8, 27, 3, 0)) is False


def test_a_realistic_number_of_windows_keeps_a_usable_width():
    p = _p(range(0, 8 * 60, 60))
    assert p.effective_cluster_half_width() >= 5.0


def test_no_learned_times_means_the_signal_never_fires():
    p = _p([])
    assert p.near_recurring_time(datetime(2026, 8, 27, 3, 0)) is False


def test_a_saturating_profile_no_longer_matches_the_whole_night():
    """The actual regression: with many learned times the old +-25 min windows covered nearly
    every minute. Bounded, most of the night must fall OUTSIDE them."""
    p = _p(range(0, 8 * 60, 60))        # 8 learned times, hourly
    hits = sum(1 for m in range(0, 1440, 5)
               if p.near_recurring_time(datetime(2026, 8, 27, m // 60, m % 60)))
    assert hits / (1440 / 5) < 0.5


def test_a_time_inside_a_bounded_window_still_matches():
    p = _p([180, 300])
    assert p.near_recurring_time(datetime(2026, 8, 27, 3, 10)) is True
    assert p.near_recurring_time(datetime(2026, 8, 27, 4, 10)) is False
