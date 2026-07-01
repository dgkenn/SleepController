"""Tests for the OAuth-free ICS calendar ingest: the pure parser (from a sample ICS string,
no network) and IcsCalendarSource's fetch/cache/fail-soft behavior (network call stubbed)."""

from datetime import datetime

import pytest

from sleepctl.adapters.calendar import (
    IcsCalendarSource,
    next_wake_time_from_events,
    parse_ics,
    upcoming_events,
)

SAMPLE_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
DTSTART:20260705T113000Z
DTEND:20260705T193000Z
SUMMARY:Night shift\\, ICU
UID:abc123@google.com
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20260710
DTEND;VALUE=DATE:20260711
SUMMARY:Vacation day
UID:def456@google.com
END:VEVENT
BEGIN:VEVENT
DTSTART:20260703T083000
DTEND:20260703T100000
SUMMARY:Clinic follow-up
UID:ghi789@google.com
END:VEVENT
BEGIN:VEVENT
DTSTART:20260706T220000Z
DTEND;VALUE=DATE:20260707
SUMMARY:Long event summary that wraps across
 a folded continuation line per RFC 5545
UID:jkl012@google.com
END:VEVENT
END:VCALENDAR
"""


# --------------------------------------------------------------------------- pure parser

def test_parses_all_vevents():
    events = parse_ics(SAMPLE_ICS)
    assert len(events) == 4


def test_parses_utc_datetime_with_z_suffix():
    events = parse_ics(SAMPLE_ICS)
    icu = next(e for e in events if "Night shift" in e.summary)
    assert icu.start == datetime(2026, 7, 5, 11, 30)
    assert icu.end == datetime(2026, 7, 5, 19, 30)
    assert icu.all_day is False


def test_parses_floating_local_datetime():
    events = parse_ics(SAMPLE_ICS)
    clinic = next(e for e in events if "Clinic" in e.summary)
    assert clinic.start == datetime(2026, 7, 3, 8, 30)


def test_parses_all_day_value_date_events():
    events = parse_ics(SAMPLE_ICS)
    vac = next(e for e in events if "Vacation" in e.summary)
    assert vac.all_day is True
    assert vac.start == datetime(2026, 7, 10, 0, 0)
    assert vac.end == datetime(2026, 7, 11, 0, 0)


def test_unescapes_commas_in_summary():
    events = parse_ics(SAMPLE_ICS)
    icu = next(e for e in events if "ICU" in e.summary)
    assert icu.summary == "Night shift, ICU"


def test_unfolds_continuation_lines():
    events = parse_ics(SAMPLE_ICS)
    long_ev = next(e for e in events if e.summary.startswith("Long event"))
    assert "folded continuation line" in long_ev.summary


def test_events_sorted_by_start():
    events = parse_ics(SAMPLE_ICS)
    starts = [e.start for e in events]
    assert starts == sorted(starts)


def test_empty_and_garbage_input_returns_no_events():
    assert parse_ics("") == []
    assert parse_ics("not an ics file at all\njust noise") == []


def test_malformed_vevent_missing_dtstart_is_skipped():
    text = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:No start time
UID:broken@x.com
END:VEVENT
BEGIN:VEVENT
DTSTART:20260101T120000
SUMMARY:Fine
UID:ok@x.com
END:VEVENT
END:VCALENDAR
"""
    events = parse_ics(text)
    assert len(events) == 1
    assert events[0].summary == "Fine"


# --------------------------------------------------------------------------- upcoming/next helpers

def test_upcoming_events_filters_by_window():
    events = parse_ics(SAMPLE_ICS)
    now = datetime(2026, 7, 1, 0, 0)
    within_3days = upcoming_events(events, now=now, within_days=3)
    assert [e.summary for e in within_3days] == ["Clinic follow-up"]


def test_next_wake_time_is_earliest_upcoming_start():
    events = parse_ics(SAMPLE_ICS)
    now = datetime(2026, 7, 1, 0, 0)
    nxt = next_wake_time_from_events(events, now=now, within_days=14)
    assert nxt == datetime(2026, 7, 3, 8, 30)


def test_next_wake_time_none_when_nothing_upcoming():
    events = parse_ics(SAMPLE_ICS)
    now = datetime(2027, 1, 1, 0, 0)  # after everything in the sample
    assert next_wake_time_from_events(events, now=now) is None


# --------------------------------------------------------------------------- IcsCalendarSource

class _StubSource(IcsCalendarSource):
    """Overrides the network fetch so tests never touch a real URL."""

    def __init__(self, text: str, **kw):
        super().__init__(ics_url="https://example.invalid/secret.ics", **kw)
        self._stub_text = text
        self.fetch_calls = 0

    def _fetch_text(self) -> str:
        self.fetch_calls += 1
        return self._stub_text


def test_refresh_parses_and_caches():
    src = _StubSource(SAMPLE_ICS, cache_seconds=900.0)
    events = src.refresh()
    assert len(events) == 4
    assert src.fetch_calls == 1
    # second call within the cache window should NOT re-fetch
    src.refresh()
    assert src.fetch_calls == 1


def test_refresh_force_bypasses_cache():
    src = _StubSource(SAMPLE_ICS, cache_seconds=900.0)
    src.refresh()
    src.refresh(force=True)
    assert src.fetch_calls == 2


def test_fetch_failure_fails_soft_and_keeps_last_good_events():
    src = _StubSource(SAMPLE_ICS, cache_seconds=0.0)  # always stale, so refresh always tries
    src.refresh()
    assert len(src._cached_events) == 4

    def _boom():
        raise TimeoutError("simulated network failure")
    src._fetch_text = _boom
    events = src.refresh(force=True)
    assert events == src._cached_events  # falls back to last known good, not empty/raise
    assert src._last_error is not None


def test_get_context_derives_wake_time_for_a_given_date():
    src = _StubSource(SAMPLE_ICS)
    ctx = src.get_context("2026-07-03")
    assert ctx.required_wake_time == datetime(2026, 7, 3, 8, 30)
    assert ctx.is_short_sleep_day in (True, False)  # computed, not None, given a wake time


def test_get_context_ignores_all_day_events_for_wake_time():
    src = _StubSource(SAMPLE_ICS)
    ctx = src.get_context("2026-07-10")  # only the all-day "Vacation day" event that date
    assert ctx.required_wake_time is None


def test_get_context_no_events_that_day():
    src = _StubSource(SAMPLE_ICS)
    ctx = src.get_context("2026-08-01")
    assert ctx.required_wake_time is None
    assert ctx.sleep_opportunity_min is None
