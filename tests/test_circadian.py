"""Tests for the circadian phase model: habitual phase estimate, phase-shift detection under a
rotating schedule, graceful fallback with few nights, and the wake-maintenance zone."""

from datetime import datetime, timedelta

from sleepctl.controller.circadian import (
    MIN_NIGHTS_FOR_ESTIMATE,
    estimate_circadian,
    wake_maintenance_zone_from_midpoint,
)
from sleepctl.controller.sleep_plan import bedtime_guidance, plan_night
from sleepctl.models import NightSummary


class _FakeRepo:
    """Minimal stand-in for sleepctl.storage.repository.Repository — only the method
    estimate_circadian actually calls."""

    def __init__(self, nights):
        self._nights = nights

    def recent_nights(self, n):
        return self._nights[-n:]


def _steady_nights(n, bed_hour=23, bed_min=0, wake_hour=7, wake_min=0, start=None):
    """n consecutive nights with a fixed bedtime/wake time (oldest-first, like recent_nights).

    Wake is on the next calendar day only when the wake clock-time is <= the bed clock-time
    (i.e. the usual overnight case); a same-day-later wake (e.g. bed 09:00 -> wake 17:00, a
    night-shift sleeper's daytime sleep) stays same-day.
    """
    start = start or datetime(2026, 6, 1)
    bed_clock = bed_hour * 60 + bed_min
    wake_clock = wake_hour * 60 + wake_min
    wake_day_offset = 0 if wake_clock > bed_clock else 1
    nights = []
    for i in range(n):
        d = start + timedelta(days=i)
        bt = d.replace(hour=bed_hour, minute=bed_min)
        wt = (d + timedelta(days=wake_day_offset)).replace(hour=wake_hour, minute=wake_min)
        tst = (wt - bt).total_seconds() / 60.0
        nights.append(NightSummary(date=d.date().isoformat(), bedtime=bt, wake_time=wt,
                                   total_sleep_min=tst))
    return nights


# --------------------------------------------------------------------------- habitual estimate

def test_habitual_midpoint_from_steady_schedule():
    nights = _steady_nights(10, bed_hour=23, wake_hour=7)  # midpoint = 03:00
    est = estimate_circadian(_FakeRepo(nights))
    assert est.habitual_midpoint_clock == "03:00"
    assert est.habitual_sleep_start_clock == "23:00"
    assert est.habitual_sleep_end_clock == "07:00"
    assert est.n_nights_habitual == 10
    assert est.confidence > 0.5  # regular schedule, plenty of nights -> confident


def test_phase_shift_detected_after_rotating_onto_nights():
    # 10 steady nights (bed 23:00 -> wake 07:00, midpoint 03:00), then 3 night-shift nights
    # (bed 08:00 -> wake 16:00, midpoint 12:00): a ~9 h later phase shift.
    steady = _steady_nights(10, bed_hour=23, wake_hour=7)
    shifted = _steady_nights(3, bed_hour=8, wake_hour=16,
                             start=steady[-1].bedtime.replace(hour=0) + timedelta(days=1))
    est = estimate_circadian(_FakeRepo(steady + shifted))
    assert est.recent_midpoint_clock == "12:00"
    assert est.phase_shift_hours is not None
    assert est.phase_shift_hours > 5.0  # clearly later than habit
    assert "later" in est.note


def test_phase_shift_near_zero_for_a_stable_schedule():
    nights = _steady_nights(10, bed_hour=23, wake_hour=7)
    est = estimate_circadian(_FakeRepo(nights))
    assert abs(est.phase_shift_hours) < 0.5
    assert "no significant phase shift" in est.note


# --------------------------------------------------------------------------- graceful fallback

def test_no_nights_falls_back_gracefully():
    est = estimate_circadian(_FakeRepo([]))
    assert est.habitual_midpoint_clock is None
    assert est.confidence == 0.0
    assert est.wake_maintenance_zone is None
    assert "Not enough sleep history" in est.note


def test_one_usable_night_falls_back_gracefully():
    nights = _steady_nights(1)
    est = estimate_circadian(_FakeRepo(nights))
    assert est.n_nights_habitual == 0  # below MIN_NIGHTS_FOR_ESTIMATE
    assert est.confidence == 0.0


def test_minimum_nights_produces_an_estimate():
    nights = _steady_nights(MIN_NIGHTS_FOR_ESTIMATE)
    est = estimate_circadian(_FakeRepo(nights))
    assert est.habitual_midpoint_clock is not None
    assert est.n_nights_habitual == MIN_NIGHTS_FOR_ESTIMATE


def test_none_repo_does_not_raise():
    est = estimate_circadian(None)
    assert est.confidence == 0.0


def test_repo_that_raises_falls_back_instead_of_propagating():
    class _BrokenRepo:
        def recent_nights(self, n):
            raise RuntimeError("db unavailable")

    est = estimate_circadian(_BrokenRepo())
    assert est.confidence == 0.0
    assert est.habitual_midpoint_clock is None


def test_nights_missing_bedtime_or_wake_are_skipped_not_crashed():
    nights = _steady_nights(3)
    nights.append(NightSummary(date="2026-06-20"))  # no bedtime/wake/tst at all
    est = estimate_circadian(_FakeRepo(nights))
    assert est.n_nights_habitual == 3  # the empty night contributed nothing, didn't crash


# --------------------------------------------------------------------------- wake-maintenance zone

def test_wake_maintenance_zone_precedes_habitual_sleep_onset():
    # Midpoint 03:00, 8 h span -> onset 23:00. WMZ ends 60 min before onset (22:00) and spans
    # 150 min, so it should run ~19:30-22:00.
    wmz = wake_maintenance_zone_from_midpoint(180, typical_sleep_span_min=480)  # 03:00 = 180 min
    assert wmz.start_clock == "19:30"
    assert wmz.end_clock == "22:00"


def test_wake_maintenance_zone_contains_checks_times_inside_and_outside():
    wmz = wake_maintenance_zone_from_midpoint(180, typical_sleep_span_min=480)
    inside = datetime(2026, 6, 1, 20, 30)
    outside_morning = datetime(2026, 6, 1, 9, 0)
    assert wmz.contains(inside)
    assert not wmz.contains(outside_morning)


def test_estimate_includes_wake_maintenance_zone_when_confident():
    nights = _steady_nights(10, bed_hour=23, wake_hour=7)
    est = estimate_circadian(_FakeRepo(nights))
    assert est.wake_maintenance_zone is not None
    d = est.wake_maintenance_zone.to_dict()
    assert "start_clock" in d and "end_clock" in d


def test_night_shift_worker_gets_a_shifted_wake_maintenance_zone():
    # A habitual night-shift sleeper (bed 09:00 -> wake 17:00, midpoint 13:00) should get a WMZ
    # in the morning/midday, not the evening.
    nights = _steady_nights(10, bed_hour=9, wake_hour=17)
    est = estimate_circadian(_FakeRepo(nights))
    assert est.habitual_midpoint_clock == "13:00"
    wmz = est.wake_maintenance_zone
    assert wmz is not None
    # habitual onset 09:00, WMZ ends 60 min earlier (08:00) and spans 150 min -> 05:30-08:00.
    assert wmz.start_clock == "05:30"
    assert wmz.end_clock == "08:00"


# --------------------------------------------------------------------------- wiring hook:
# sleep_plan.bedtime_guidance / plan_night ground their guidance in the circadian estimate
# when a repo is supplied (additive — both still work with repo=None, unchanged behavior).

def test_bedtime_guidance_without_repo_has_no_circadian_fields():
    nights = _steady_nights(10, bed_hour=23, wake_hour=7)
    g = bedtime_guidance(datetime(2026, 6, 15, 7, 0), nights, need_min=480)
    assert g.wake_maintenance_zone is None
    assert g.circadian_note is None


def test_bedtime_guidance_with_repo_grounds_note_in_wake_maintenance_zone():
    # Habitual bed 23:00 -> wake 07:00 (midpoint 03:00, WMZ ~19:30-22:00). Ask for early lights
    # -out (e.g. required wake pulled earlier) so the recommended in-bed time lands in the WMZ.
    nights = _steady_nights(10, bed_hour=23, wake_hour=7)
    repo = _FakeRepo(nights)
    # need_min=480 + wake at 05:00 -> asleep-by 21:00, in-bed ~20:48 (12 min onset) -> inside WMZ.
    g = bedtime_guidance(datetime(2026, 6, 15, 5, 0), nights, need_min=480, onset_min=12,
                         repo=repo)
    assert g.wake_maintenance_zone is not None
    assert g.circadian_note is not None
    assert "wake-maintenance zone" in g.circadian_note


def test_bedtime_guidance_repo_failure_does_not_break_the_plan():
    class _BrokenRepo:
        def recent_nights(self, n):
            raise RuntimeError("boom")

    nights = _steady_nights(5)
    g = bedtime_guidance(datetime(2026, 6, 15, 7, 0), nights, need_min=480, repo=_BrokenRepo())
    assert g is not None                    # still returns a usable guidance
    assert g.circadian_note is None         # just skips the circadian grounding


def test_plan_night_threads_repo_into_bedtime_circadian_grounding():
    nights = _steady_nights(10, bed_hour=23, wake_hour=7)
    repo = _FakeRepo(nights)
    plan = plan_night(datetime(2026, 6, 15, 20, 0), datetime(2026, 6, 16, 5, 0), nights,
                      repo=repo)
    assert plan.bedtime is not None
    assert plan.bedtime.wake_maintenance_zone is not None
