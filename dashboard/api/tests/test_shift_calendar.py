"""Calendar-driven shifts: syncing the OAuth-free ICS calendar feed into the shift planner.

The resident dedicates ONE calendar to work shifts only -- each event IS the shift itself
(start->end). These tests cover: sync writes the right next_shift/kind from a synthetic feed,
disabling the calendar preserves a manually-set shift config, day-shift auto-wake = start minus
the prep buffer, night-shift gets no auto-wake, and the /shift/plan endpoint reflects the next
calendar event end-to-end."""

from __future__ import annotations

from datetime import datetime, timedelta


def _future_ics(start: datetime, end: datetime, summary: str = "ICU shift") -> str:
    def fmt(dt):
        return dt.strftime("%Y%m%dT%H%M%S")
    return f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
DTSTART:{fmt(start)}
DTEND:{fmt(end)}
SUMMARY:{summary}
UID:test-shift@example.com
END:VEVENT
END:VCALENDAR
"""


def _connect_calendar(auth_client, monkeypatch, ics_text: str):
    """Configure + enable the calendar, stubbing the network fetch to return ics_text."""
    from sleepctl.adapters.calendar import IcsCalendarSource
    monkeypatch.setattr(IcsCalendarSource, "_fetch_text", lambda self: ics_text)
    r = auth_client.put("/calendar/config", json={
        "enabled": True, "ics_url": "https://example.invalid/secret.ics"})
    assert r.status_code == 200
    # clear any cached source from a previous test so the new stub text is picked up
    from app import services
    services._ICS_SOURCE_CACHE.clear()


def test_sync_writes_next_shift_and_night_kind(auth_client, monkeypatch):
    from app import services
    now = datetime.now()
    start = now + timedelta(days=2, hours=1)
    start = start.replace(hour=19, minute=0, second=0, microsecond=0)  # evening -> night
    end = start + timedelta(hours=8)
    _connect_calendar(auth_client, monkeypatch, _future_ics(start, end, "Night shift, ICU"))

    from app.db import get_repo
    repo = get_repo()
    cfg = services.sync_calendar_to_shift(repo)
    assert cfg["enabled"] is True
    assert cfg["kind"] == "night"
    assert cfg["source"] == "calendar"
    assert cfg["next_shift"] == start.isoformat()
    assert cfg["shift_end"] == end.isoformat()


def test_sync_writes_day_kind_for_morning_shift(auth_client, monkeypatch):
    from app import services
    now = datetime.now()
    start = (now + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    if start <= now:
        start += timedelta(days=1)
    end = start + timedelta(hours=10)
    _connect_calendar(auth_client, monkeypatch, _future_ics(start, end, "Day shift, clinic"))

    from app.db import get_repo
    repo = get_repo()
    cfg = services.sync_calendar_to_shift(repo)
    assert cfg["kind"] == "day"
    assert cfg["source"] == "calendar"
    assert cfg["next_shift"] == start.isoformat()


def test_calendar_disabled_preserves_manual_shift_config(auth_client, monkeypatch):
    from app import services
    from app.db import get_repo
    repo = get_repo()

    # Manually set a shift hint first.
    manual_start = (datetime.now() + timedelta(days=5)).replace(
        hour=20, minute=0, second=0, microsecond=0)
    manual_cfg = services.shift_config_update(repo, {
        "enabled": True, "next_shift": manual_start.isoformat(), "kind": "call"})
    assert manual_cfg["source"] == "manual"

    # Ensure the calendar is disabled (default in a fresh env) -- sync must be a no-op.
    auth_client.put("/calendar/config", json={"enabled": False, "ics_url": None})
    result = services.sync_calendar_to_shift(repo)
    assert result["source"] == "manual"
    assert result["next_shift"] == manual_start.isoformat()
    assert result["kind"] == "call"


def test_day_shift_recommended_wake_is_start_minus_buffer(auth_client, monkeypatch):
    from app import services
    from app.db import get_repo
    from sleepctl.config import AppConfig
    repo = get_repo()

    now = datetime.now()
    start = (now + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    if start <= now:
        start += timedelta(days=1)
    end = start + timedelta(hours=10)
    _connect_calendar(auth_client, monkeypatch, _future_ics(start, end, "Day shift"))
    services.sync_calendar_to_shift(repo)

    wake = services.calendar_effective_wake(repo)
    buffer_min = AppConfig.default().tunables.shift_prep_buffer_min
    assert wake == start - timedelta(minutes=buffer_min)


def test_night_shift_has_no_auto_wake(auth_client, monkeypatch):
    from app import services
    from app.db import get_repo
    repo = get_repo()

    now = datetime.now()
    start = (now + timedelta(days=1)).replace(hour=21, minute=0, second=0, microsecond=0)
    if start <= now:
        start += timedelta(days=1)
    end = start + timedelta(hours=9)
    _connect_calendar(auth_client, monkeypatch, _future_ics(start, end, "Night shift"))
    services.sync_calendar_to_shift(repo)

    assert services.calendar_effective_wake(repo) is None


def test_shift_plan_endpoint_reflects_connected_calendar(auth_client, monkeypatch):
    now = datetime.now()
    start = (now + timedelta(days=1)).replace(hour=6, minute=30, second=0, microsecond=0)
    if start <= now:
        start += timedelta(days=1)
    end = start + timedelta(hours=9)
    _connect_calendar(auth_client, monkeypatch, _future_ics(start, end, "Day shift"))

    plan = auth_client.get("/shift/plan").json()
    assert plan["shift_enabled"] is True
    assert plan["next_shift"] == start.isoformat()
    assert plan["next_shift_kind"] == "day"
    assert plan["next_shift_source"] == "calendar"
    assert plan["shift_end"] == end.isoformat()
    assert plan["recommended_wake"] is not None
    rec_wake = datetime.fromisoformat(plan["recommended_wake"])
    assert rec_wake < start
