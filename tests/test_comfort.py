"""In-bed comfort mapping: the sweep collects ratings and derives neutral + comfort band."""

from sleepctl.controller.comfort import (
    ComfortCalibration, build_comfort_profile, steps_around)


def test_sweep_collects_and_advances():
    c = ComfortCalibration(steps_f=[64, 68, 72, 76])
    seen = []
    for rating in (-2, -1, 1, 2):
        seen.append(c.current_target_f())
        c.rate(rating)
    assert seen == [64, 68, 72, 76]
    assert c.done and c.current_target_f() is None


def test_neutral_interpolates_zero_crossing():
    # rating goes -1 at 68 -> +1 at 72, so "just right" interpolates to 70
    p = build_comfort_profile([{"f": 68, "rating": -1}, {"f": 72, "rating": 1}])
    assert p.neutral_f == 70.0
    assert p.cool_edge_f == 68 and p.warm_edge_f == 72


def test_all_too_warm_picks_coolest_acceptable():
    p = build_comfort_profile([{"f": 64, "rating": 0}, {"f": 68, "rating": 1},
                               {"f": 72, "rating": 2}])
    assert p.neutral_f == 64.0          # the just-right one
    assert p.warm_edge_f == 68 and p.cool_edge_f == 64  # 72 (too warm) excluded from band


def test_rating_is_clamped():
    c = ComfortCalibration(steps_f=[70])
    c.rate(9)                            # out of range -> clamped to +2
    assert c.ratings[0]["rating"] == 2


def test_cancel_ends_the_sweep():
    c = ComfortCalibration(steps_f=[64, 68])
    c.cancel()
    assert c.done and c.cancelled


def test_steps_around_centers_on_neutral():
    s = steps_around(70.0, spread_f=6.0, n=4)
    assert s[0] == 64.0 and s[-1] == 76.0 and len(s) == 4
    assert steps_around(None)            # falls back to defaults
