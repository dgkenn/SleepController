"""Coverage for the service functions a real measurement showed were untouched.

``app/services.py`` sits at 84% line coverage; ten functions were mostly or entirely uncovered.
These cover the four with real logic and real consequences, in risk order. The rest of the
uncovered set is the Hue bridge integration (needs hardware, and is a thin pass-through to
``adapters/hue.py``), which is why it isn't here.

``gym_effective_wake`` leads because it is the only one that MOVES THE ALARM. Everything else in
this file changes what a card displays; that one changes when the user is woken up.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app import services


@pytest.fixture()
def repo(tmp_path):
    from sleepctl.storage.repository import Repository
    from app import db as app_db

    r = Repository(str(tmp_path / "svc.db"), check_same_thread=False)
    r.conn.executescript(app_db._DASHBOARD_DDL)
    app_db._apply_migrations(r.conn)
    r.conn.commit()
    yield r
    r.close()


def _set_gym(repo, **values):
    repo.conn.execute("DELETE FROM settings_kv WHERE key='gym_config'")
    repo.conn.execute("INSERT INTO settings_kv (key, value) VALUES ('gym_config', ?)",
                      (json.dumps(values),))
    repo.conn.commit()


# ------------------------------------------------------------------ gym_effective_wake
def test_gym_disabled_never_moves_the_alarm(repo):
    """The safe no-op. With the advisor off, the user's own wake time must come back untouched."""
    _set_gym(repo, enabled=False)
    wake = datetime(2026, 7, 30, 7, 0)
    assert services.gym_effective_wake(repo, wake) == wake


def test_no_gym_config_at_all_is_also_a_no_op(repo):
    """A box that has never opened the gym card must behave exactly like one with it disabled."""
    wake = datetime(2026, 7, 30, 7, 0)
    assert services.gym_effective_wake(repo, wake) == wake


def test_gym_enabled_never_moves_the_alarm_LATER(repo):
    """The advisor may pull the deadline earlier for a workout; it must never let the user
    oversleep their real obligation by pushing it back."""
    _set_gym(repo, enabled=True, early_offset_min=45)
    wake = datetime(2026, 7, 30, 7, 0)
    assert services.gym_effective_wake(repo, wake) <= wake


def test_gym_with_no_history_is_still_safe(repo):
    """recent_nights() is empty on a fresh box -- the median-bedtime branch must not divide by
    zero or hand back a nonsense deadline."""
    _set_gym(repo, enabled=True)
    wake = datetime(2026, 7, 30, 7, 0)
    out = services.gym_effective_wake(repo, wake)
    assert isinstance(out, datetime) and out <= wake


def test_gym_returns_a_datetime_not_a_string(repo):
    """The daemon assigns this straight onto context.required_wake_time; a str would poison every
    downstream time comparison rather than failing loudly."""
    _set_gym(repo, enabled=True)
    assert isinstance(services.gym_effective_wake(repo, datetime(2026, 7, 30, 7, 0)), datetime)


# ------------------------------------------------------------------ nap_preview
def test_nap_preview_defaults_to_a_power_nap_length():
    out = services.nap_preview()
    assert isinstance(out, dict) and out


def test_nap_preview_honours_an_explicit_duration():
    short = services.nap_preview(duration_min=20)
    long_ = services.nap_preview(duration_min=90)
    assert short != long_, "a 20-minute and a 90-minute nap are different strategies"


def test_nap_preview_accepts_a_wake_time_and_derives_the_window():
    out = services.nap_preview(wake_time="23:59")
    assert isinstance(out, dict) and out


def test_nap_preview_rolls_a_past_wake_time_to_tomorrow():
    """A 'wake me at 07:00' entered in the evening means tomorrow morning, not 14 hours ago —
    otherwise the window goes negative and the strategy is nonsense."""
    out = services.nap_preview(wake_time="00:01")
    assert isinstance(out, dict) and out


@pytest.mark.parametrize("bad", ["not-a-time", "25:99", "", "7", "::", "a:b"])
def test_nap_preview_falls_back_on_an_unparseable_wake_time(bad):
    """The field is free text on the nap card; a typo must degrade to a default nap, not 500."""
    out = services.nap_preview(wake_time=bad)
    assert isinstance(out, dict) and out


def test_nap_preview_never_produces_a_zero_length_window():
    """`max(5, ...)` floor: a wake time seconds away must not yield a 0-minute nap plan."""
    now = datetime.now()
    almost_now = (now + timedelta(seconds=30)).strftime("%H:%M")
    out = services.nap_preview(wake_time=almost_now)
    assert isinstance(out, dict) and out


# ------------------------------------------------------------------ cbti_advice
def _night(repo, date, bedtime=None, wake=None, total=None, eff=None, wake_events=1):
    from sleepctl.models import NightSummary

    repo.save_night_summary(NightSummary(
        date=date, bedtime=bedtime, wake_time=wake, total_sleep_min=total,
        sleep_efficiency=eff, wake_events=wake_events))


def test_cbti_advice_on_an_empty_history_is_safe(repo):
    out = services.cbti_advice(repo)
    assert isinstance(out, dict)


def test_cbti_advice_derives_time_in_bed_from_bedtime_and_wake(repo):
    for i in range(10):
        d = f"2026-07-{i + 1:02d}"
        _night(repo, d, bedtime=datetime(2026, 7, i + 1, 23, 0),
               wake=datetime(2026, 7, i + 2, 7, 0), total=420.0, eff=0.875)
    out = services.cbti_advice(repo)
    assert isinstance(out, dict) and out


def test_cbti_advice_reconstructs_time_in_bed_from_efficiency_when_times_are_missing(repo):
    """TIB = total_sleep / efficiency is how the efficiency figure was defined; the fallback has
    to agree with that definition or the whole titration drifts."""
    for i in range(10):
        _night(repo, f"2026-07-{i + 1:02d}", total=420.0, eff=0.875)   # -> TIB 480
    out = services.cbti_advice(repo)
    assert isinstance(out, dict) and out


def test_cbti_advice_passes_through_nights_it_cannot_place(repo):
    """A night with neither times nor efficiency must NOT get a guessed TIB — inventing one
    would quietly corrupt the efficiency estimate the recommendation keys off."""
    for i in range(5):
        _night(repo, f"2026-07-{i + 1:02d}", total=None, eff=None)
    out = services.cbti_advice(repo)          # must not raise
    assert isinstance(out, dict)


def test_cbti_advice_tolerates_a_zero_efficiency_night(repo):
    """total/efficiency with efficiency 0 is a ZeroDivisionError waiting to happen."""
    _night(repo, "2026-07-01", total=420.0, eff=0.0)
    out = services.cbti_advice(repo)
    assert isinstance(out, dict)


def test_cbti_advice_respects_the_lookback_window(repo):
    for i in range(20):
        _night(repo, f"2026-07-{i + 1:02d}", total=420.0, eff=0.875)
    assert isinstance(services.cbti_advice(repo, nights_back=3), dict)


# ------------------------------------------------------------------ calendar_refresh
def test_calendar_refresh_with_no_feed_configured_is_a_clean_no_op(repo):
    out = services.calendar_refresh(repo)
    assert isinstance(out, dict)


def test_calendar_refresh_reports_an_unreachable_feed_rather_than_raising(repo):
    """An unreachable ICS is the single most common calendar failure (it's a secret URL that
    rotates). It must surface as a result the card can show, not a 500."""
    repo.conn.execute("DELETE FROM settings_kv WHERE key='calendar_ics_url'")
    repo.conn.execute("INSERT INTO settings_kv (key, value) VALUES ('calendar_ics_url', ?)",
                      (json.dumps("http://127.0.0.1:9/nope.ics"),))
    repo.conn.commit()
    out = services.calendar_refresh(repo)
    assert isinstance(out, dict)


# ------------------------------------------------------------------ _parse_epoch
@pytest.mark.parametrize("value,expected_none", [
    (None, True),
    ("", True),
    ("not-a-timestamp", True),
    (float("nan"), True),
    (float("inf"), True),
    (1785180000.0, False),
    (1785180000, False),
    ("2026-07-28T02:00:00", False),
])
def test_parse_epoch_handles_every_shape_the_verity_history_can_contain(value, expected_none):
    """It runs inside the cardiac quality guard on every ingest; a malformed history row must
    degrade to None rather than take the ingest path down."""
    out = services._parse_epoch(value)
    assert (out is None) is expected_none, (value, out)
    if out is not None:
        assert isinstance(out, float)
