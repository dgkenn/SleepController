"""Personalized wake tuning: learn the user's grogginess curve -> window + lift bar."""

from sleepctl.learning.wake_tuning import WakeTuning, learn_wake_tuning


def test_too_few_nights_keeps_defaults():
    recs = [{"window_min": 30, "grogginess": 3, "forced": False} for _ in range(4)]
    t = learn_wake_tuning(recs, base_window=30, min_nights=8)
    assert t.is_personalized is False and t.window_min == 30


def test_wider_windows_groggier_narrows_window():
    # grogginess rises with window width -> learner narrows the window.
    recs = []
    for w, g in [(15, 1), (20, 2), (25, 3), (30, 4), (35, 5), (40, 6), (45, 7), (45, 7), (40, 6),
                 (35, 5)]:
        recs.append({"window_min": w, "grogginess": g, "forced": False})
    t = learn_wake_tuning(recs, base_window=30, min_nights=8)
    assert t.window_min < 30 and t.is_personalized is True


def test_forced_wakes_groggier_lowers_lift_bar():
    # forced (deep/deadline) wakes are much groggier than light wakes -> lower the lift bar so the
    # orchestrator catches light moments more readily.
    recs = []
    for _ in range(6):
        recs.append({"window_min": 30, "grogginess": 8, "forced": True})
        recs.append({"window_min": 30, "grogginess": 2, "forced": False})
    t = learn_wake_tuning(recs, base_window=30, base_liftable=0.45, min_nights=8)
    assert t.p_wake_liftable < 0.45 and t.is_personalized is True


def test_bounds_are_respected():
    recs = [{"window_min": 45, "grogginess": 10, "forced": True} for _ in range(20)]
    t = learn_wake_tuning(recs, base_window=30, min_nights=8)
    assert 10 <= t.window_min <= 45 and 0.30 <= t.p_wake_liftable <= 0.70
