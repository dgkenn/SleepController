"""Gym-vs-sleep advisor: resident-framed (only-window value vs clinical-readiness floor)."""

from datetime import datetime, timedelta

from sleepctl.gym_advisor import GymConfig, gym_decision
from sleepctl.models import NightSummary


def _nights(n, total_min):
    return [NightSummary(date=f"2026-06-{10+i}", total_sleep_min=total_min) for i in range(n)]


# A fixed morning: normal alarm 06:30, 75-min-earlier gym alarm = 05:15.
NOW = datetime(2026, 6, 29, 4, 0)
WAKE = datetime(2026, 6, 29, 6, 30)


def _onset_for(proj_gym_h):
    """Bedtime/onset that yields proj_gym_h of sleep by the 05:15 early alarm."""
    early = WAKE - timedelta(minutes=75)
    return early - timedelta(hours=proj_gym_h)


def test_disabled_returns_off():
    d = gym_decision(NOW, WAKE, _nights(7, 540), cfg=GymConfig(enabled=False))
    assert d.recommend == "off"


def test_go_when_sleep_is_adequate_only_window():
    # ~6.5 h projected, no debt -> the only-window value carries it to GO.
    cfg = GymConfig(enabled=True, lean="balanced")
    d = gym_decision(NOW, WAKE, _nights(7, 540), cfg=cfg, sleep_onset=_onset_for(6.5))
    assert d.recommend == "go"
    assert d.projected_gym_sleep_h == 6.5
    assert any("only window" in r.lower() for r in d.reasons)


def test_sleep_in_below_safe_floor():
    # ~4.5 h projected -> under the 6 h floor -> protect sleep regardless of the window.
    cfg = GymConfig(enabled=True, lean="balanced")
    d = gym_decision(NOW, WAKE, _nights(7, 540), cfg=cfg, sleep_onset=_onset_for(4.5))
    assert d.recommend == "sleep_in"
    assert any("floor" in r.lower() for r in d.reasons)


def test_heavy_debt_flips_to_sleep_in_even_with_ok_projection():
    # adequate single-night projection, but deep cumulative debt -> recovery wins.
    cfg = GymConfig(enabled=True, lean="balanced")
    d = gym_decision(NOW, WAKE, _nights(7, 300), cfg=cfg, sleep_onset=_onset_for(6.5))
    assert d.recommend == "sleep_in"
    assert any("debt" in r.lower() for r in d.reasons)


def test_lean_changes_the_borderline_call():
    # ~6.0 h projected, no debt: push -> go, protect -> sleep_in.
    nights = _nights(7, 540)
    onset = _onset_for(6.0)
    push = gym_decision(NOW, WAKE, nights, cfg=GymConfig(enabled=True, lean="push"),
                        sleep_onset=onset)
    protect = gym_decision(NOW, WAKE, nights, cfg=GymConfig(enabled=True, lean="protect"),
                           sleep_onset=onset)
    assert push.recommend == "go"
    assert protect.recommend == "sleep_in"


def test_demanding_shift_pulls_toward_sleep():
    cfg = GymConfig(enabled=True, lean="balanced")
    onset = _onset_for(6.4)
    calm = gym_decision(NOW, WAKE, _nights(7, 540), cfg=cfg, sleep_onset=onset,
                        day_demanding=False)
    busy = gym_decision(NOW, WAKE, _nights(7, 540), cfg=cfg, sleep_onset=onset,
                        day_demanding=True)
    assert busy.go_score < calm.go_score


def test_rest_day_when_not_a_scheduled_gym_day():
    not_today = [(NOW.weekday() + 1) % 7]
    d = gym_decision(NOW, WAKE, _nights(7, 540),
                     cfg=GymConfig(enabled=True, gym_days=not_today))
    assert d.recommend == "rest_day"


def test_higher_projection_scores_higher():
    cfg = GymConfig(enabled=True, lean="balanced")
    lo = gym_decision(NOW, WAKE, _nights(7, 540), cfg=cfg, sleep_onset=_onset_for(6.0))
    hi = gym_decision(NOW, WAKE, _nights(7, 540), cfg=cfg, sleep_onset=_onset_for(8.0))
    assert hi.go_score > lo.go_score
    assert hi.recommend == "go"


def test_config_roundtrip():
    cfg = GymConfig(enabled=True, early_offset_min=75, lean="protect", gym_days=[0, 2, 4])
    assert GymConfig.from_dict(cfg.to_dict()).to_dict() == cfg.to_dict()
