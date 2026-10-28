"""The Pod's water reading as a room thermometer, after hours switched off (2026-10-28)."""
from datetime import datetime, timedelta

from sleepctl.controller.calibration import level_to_fahrenheit
from sleepctl.controller.room_from_water import RoomFromWater
from sleepctl.models import ContextRecord
from sleepctl.storage.repository import Repository

T0 = datetime(2026, 10, 3, 12, 0)


def _feed(est, levels, on=False, step_min=1, start=T0):
    t = start
    for lv in levels:
        est.observe(t, lv, on)
        t += timedelta(minutes=step_min)
    return t


def test_a_side_off_for_hours_reads_the_room():
    est = RoomFromWater()
    end = _feed(est, [-60] * (7 * 60))                 # 7 h off, water flat at -60
    assert est.estimate(end) == round(level_to_fahrenheit(-60), 1)   # ~67.6 F
    assert est.reason == "settled"


def test_the_slow_afternoon_cool_down_is_not_mistaken_for_the_room():
    """2026-10-03: warmed to ~0 by 16:00, then drifted ~1.4 levels/hour -- still ~13 F above
    the room eight hours later. Never settled, so no estimate."""
    est = RoomFromWater()
    levels = [int(round(1 - 1.4 * m / 60)) for m in range(8 * 60)]
    end = _feed(est, levels)
    assert est.estimate(end) is None and "drifting" in est.reason


def test_running_or_unknown_power_never_counts_as_off():
    est = RoomFromWater()
    end = _feed(est, [-68] * (8 * 60), on=True)        # held at -68 by a schedule: not the room
    assert est.estimate(end) is None
    est2 = RoomFromWater()
    t = T0
    for _ in range(8 * 60):
        est2.observe(t, -68, None)
        t += timedelta(minutes=1)
    assert est2.estimate(t) is None and "unknown" in est2.reason


def test_the_estimate_is_held_through_the_night_then_expires():
    est = RoomFromWater()
    end = _feed(est, [-55] * (7 * 60))
    got = est.estimate(end)
    _feed(est, [-30] * 60, on=True, start=end)          # the Pod runs overnight
    assert est.estimate(end + timedelta(hours=9)) == got
    assert est.estimate(end + timedelta(hours=17)) is None


def test_the_night_context_keeps_the_room_estimate(tmp_path):
    repo = Repository(str(tmp_path / "c.db"))
    repo.save_context(ContextRecord(date="2026-10-28", room_temp_est_f=67.6))
    repo.save_context(ContextRecord(date="2026-10-28"))  # a later save without it keeps it
    assert repo.get_context("2026-10-28").room_temp_est_f == 67.6


def test_one_level_of_jitter_around_a_settled_room_still_reads():
    est = RoomFromWater()
    end = _feed(est, [-60 + (m % 3 == 0) - (m % 5 == 0) for m in range(7 * 60)])
    assert est.estimate(end) is not None
