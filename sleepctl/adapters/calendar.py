"""Calendar context: required wake time + first commitment + short-night flag.

``GoogleCalendarSource`` pulls from Google Calendar via OAuth (libraries imported lazily, so
this module imports without them) — heavyweight to set up (credentials, consent screen,
token refresh). ``IcsCalendarSource`` is the OAuth-free alternative: paste a read-only
"secret address in iCal format" URL (Google Calendar Settings -> a calendar's "Integrate
calendar" section provides one; other calendars — Outlook, Apple, Fastmail — offer the same
kind of secret ICS feed). No app registration, no consent screen, no token refresh; just a
URL treated as user data (never hardcoded, never logged). ``ManualCalendarSource`` is a
dependency-free fallback for offline/testing use, and the manual "next shift" hint.
"""

from __future__ import annotations

import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from sleepctl.adapters.base import CalendarSource
from sleepctl.models import ContextRecord


# A night shorter than this sleep opportunity is treated as a short-sleep day.
SHORT_SLEEP_THRESHOLD_MIN = 7 * 60


def _sleep_opportunity_min(now: datetime, required_wake: Optional[datetime]) -> Optional[float]:
    if required_wake is None:
        return None
    return max(0.0, (required_wake - now).total_seconds() / 60.0)


class ManualCalendarSource(CalendarSource):
    """Build context from explicitly provided values (no external deps)."""

    def __init__(
        self,
        required_wake_time: Optional[datetime] = None,
        first_commitment: Optional[datetime] = None,
        work_start_time: Optional[datetime] = None,
        bedtime: Optional[datetime] = None,
        schedule_variable: Optional[bool] = None,
    ) -> None:
        self.required_wake_time = required_wake_time
        self.first_commitment = first_commitment or required_wake_time
        self.work_start_time = work_start_time
        self.bedtime = bedtime
        self.schedule_variable = schedule_variable

    def get_context(self, date: str) -> ContextRecord:
        ref = self.bedtime or datetime.now()
        opp = _sleep_opportunity_min(ref, self.required_wake_time)
        return ContextRecord(
            date=date,
            required_wake_time=self.required_wake_time,
            work_start_time=self.work_start_time,
            first_commitment=self.first_commitment,
            sleep_opportunity_min=opp,
            is_short_sleep_day=(opp is not None and opp < SHORT_SLEEP_THRESHOLD_MIN),
            schedule_variable=self.schedule_variable,
        )


class GoogleCalendarSource(CalendarSource):
    """Reads the earliest commitment of the day from Google Calendar."""

    def __init__(
        self,
        credentials_path: str = "credentials.json",
        token_path: str = "token.json",
        calendar_id: str = "primary",
        bedtime_hint: Optional[datetime] = None,
    ) -> None:
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.calendar_id = calendar_id
        self.bedtime_hint = bedtime_hint
        self._service = None

    def _build_service(self):  # pragma: no cover - requires google libs + auth
        import os

        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
        creds = None
        if os.path.exists(self.token_path):
            creds = Credentials.from_authorized_user_file(self.token_path, scopes)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_path, scopes
                )
                creds = flow.run_local_server(port=0)
            with open(self.token_path, "w", encoding="utf-8") as fh:
                fh.write(creds.to_json())
        return build("calendar", "v3", credentials=creds)

    def get_context(self, date: str) -> ContextRecord:  # pragma: no cover - live API
        if self._service is None:
            self._service = self._build_service()
        day_start = datetime.fromisoformat(date + "T00:00:00")
        day_end = day_start + timedelta(days=1)
        events = (
            self._service.events()
            .list(
                calendarId=self.calendar_id,
                timeMin=day_start.isoformat() + "Z",
                timeMax=day_end.isoformat() + "Z",
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
            .get("items", [])
        )
        first = None
        for ev in events:
            start = ev.get("start", {}).get("dateTime")
            if start:
                first = datetime.fromisoformat(start.replace("Z", "+00:00"))
                break
        ref = self.bedtime_hint or datetime.now()
        opp = _sleep_opportunity_min(ref, first)
        return ContextRecord(
            date=date,
            required_wake_time=first,
            work_start_time=first,
            first_commitment=first,
            sleep_opportunity_min=opp,
            is_short_sleep_day=(opp is not None and opp < SHORT_SLEEP_THRESHOLD_MIN),
            schedule_variable=None,
        )


# --------------------------------------------------------------------------------------------
# OAuth-free ICS ingest
#
# Google (and most calendar providers) can produce a "secret address in iCal format" — a plain
# read-only URL ending in .ics that requires no OAuth app, no consent screen, and no token
# refresh: just a long random path that acts as the credential. The user pastes that URL once;
# everything below is a pure-stdlib parser + a thin fetch-with-cache wrapper around it.
# --------------------------------------------------------------------------------------------


@dataclass
class IcsEvent:
    """One parsed VEVENT, reduced to what the sleep engine needs."""

    start: datetime
    end: Optional[datetime] = None
    summary: str = ""
    all_day: bool = False

    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat() if self.end else None,
            "summary": self.summary,
            "all_day": self.all_day,
        }


def _unfold_ics_lines(text: str) -> List[str]:
    """RFC 5545 line unfolding: a line starting with a space/tab continues the previous line."""
    lines: List[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _unescape_ics_text(value: str) -> str:
    return (value.replace("\\n", " ").replace("\\N", " ")
            .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def _parse_ics_datetime(value: str, params: dict) -> "tuple[Optional[datetime], bool]":
    """Parse a DTSTART/DTEND value per its VALUE=/TZID params. Returns (datetime, is_all_day).

    Timezone handling is intentionally simple: a trailing 'Z' -> UTC; a bare local date/time
    (including one with a TZID we don't resolve against a tz database) is treated as naive
    local time, which is the right behavior for the common case (the resident's own calendar,
    same machine timezone) without pulling in a tz-database dependency.
    """
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value, "%Y%m%d"), True
    is_utc = value.endswith("Z")
    core = value[:-1] if is_utc else value
    try:
        dt = datetime.strptime(core, "%Y%m%dT%H%M%S")
    except ValueError:
        return None, False
    if is_utc:
        # Convert to naive local time so callers can compare directly against datetime.now().
        try:
            from datetime import timezone as _tz
            local = dt.replace(tzinfo=_tz.utc).astimezone().replace(tzinfo=None)
            return local, False
        except Exception:
            return dt, False
    return dt, False


def _parse_ics_line(line: str) -> "tuple[str, dict, str]":
    """Split an unfolded ICS line into (NAME, {params}, value)."""
    head, _, value = line.partition(":")
    parts = head.split(";")
    name = parts[0].upper()
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        if k:
            params[k.upper()] = v
    return name, params, value


def parse_ics(text: str) -> List[IcsEvent]:
    """Parse ICS text into a list of ``IcsEvent`` (VEVENT blocks only). Pure, no I/O.

    Deliberately minimal — this is not a full RFC 5545 implementation (no RRULE expansion,
    no timezone database), which is sufficient for "when is my next shift" ingest: each
    calendar entry the user (or a shared roster) creates is a single VEVENT with a concrete
    DTSTART, which is exactly what every mainstream calendar UI produces for a one-off or an
    already-expanded recurring event export.
    """
    events: List[IcsEvent] = []
    in_event = False
    cur: dict = {}
    for line in _unfold_ics_lines(text or ""):
        if line.upper() == "BEGIN:VEVENT":
            in_event = True
            cur = {}
            continue
        if line.upper() == "END:VEVENT":
            in_event = False
            start = cur.get("DTSTART")
            if start is not None:
                dt, all_day = start
                end_val = cur.get("DTEND")
                end_dt = end_val[0] if end_val else None
                events.append(IcsEvent(
                    start=dt, end=end_dt,
                    summary=_unescape_ics_text(cur.get("SUMMARY", "")),
                    all_day=all_day,
                ))
            continue
        if not in_event:
            continue
        name, params, value = _parse_ics_line(line)
        if name in ("DTSTART", "DTEND"):
            dt, all_day = _parse_ics_datetime(value, params)
            if dt is not None:
                cur[name] = (dt, all_day)
        elif name == "SUMMARY":
            cur[name] = value
    events.sort(key=lambda e: e.start)
    return events


def upcoming_events(events: List[IcsEvent], now: Optional[datetime] = None,
                    within_days: int = 14) -> List[IcsEvent]:
    """Events starting between ``now`` and ``now + within_days`` days, earliest first."""
    now = now or datetime.now()
    horizon = now + timedelta(days=within_days)
    return [e for e in events if now <= e.start <= horizon]


def next_wake_time_from_events(events: List[IcsEvent], now: Optional[datetime] = None,
                               within_days: int = 3) -> Optional[datetime]:
    """The earliest upcoming event's start time — used as the required-wake-time proxy (the
    first commitment of the day, same semantics as ``GoogleCalendarSource``/manual hint)."""
    upcoming = upcoming_events(events, now=now, within_days=within_days)
    return upcoming[0].start if upcoming else None


class IcsCalendarSource(CalendarSource):
    """OAuth-free calendar ingest from a read-only secret ICS URL.

    Fetches via stdlib ``urllib`` with a short timeout, caches the parsed result (calendars
    change slowly), and fails soft — a network blip or a bad URL returns the last known good
    parse (or an empty list), never raises into the control loop. The ICS URL itself is
    treated purely as user-supplied configuration; this class never persists or logs it
    beyond holding it in memory for the fetch.
    """

    def __init__(self, ics_url: str, bedtime_hint: Optional[datetime] = None,
                cache_seconds: float = 900.0, timeout: float = 8.0) -> None:
        self.ics_url = ics_url
        self.bedtime_hint = bedtime_hint
        self.cache_seconds = cache_seconds
        self.timeout = timeout
        self._cached_events: List[IcsEvent] = []
        self._fetched_at: float = 0.0
        self._last_error: Optional[str] = None

    def _fetch_text(self) -> str:
        """Override point for tests; performs the actual HTTP GET."""
        req = urllib.request.Request(self.ics_url, headers={"User-Agent": "sleepctl/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def refresh(self, force: bool = False) -> List[IcsEvent]:
        """Re-fetch + re-parse if the cache is stale (or ``force``); fails soft on error."""
        now = time.time()
        if not force and self._cached_events and (now - self._fetched_at) < self.cache_seconds:
            return self._cached_events
        try:
            text = self._fetch_text()
            events = parse_ics(text)
        except Exception as exc:  # network error, bad URL, malformed feed — never raise upward
            self._last_error = str(exc)
            return self._cached_events
        self._cached_events = events
        self._fetched_at = now
        self._last_error = None
        return self._cached_events

    def get_context(self, date: str) -> ContextRecord:
        events = self.refresh()
        day_start = datetime.fromisoformat(date + "T00:00:00")
        day_end = day_start + timedelta(days=1)
        todays = [e for e in events if day_start <= e.start < day_end and not e.all_day]
        first = todays[0].start if todays else None
        ref = self.bedtime_hint or datetime.now()
        opp = _sleep_opportunity_min(ref, first)
        return ContextRecord(
            date=date,
            required_wake_time=first,
            work_start_time=first,
            first_commitment=first,
            sleep_opportunity_min=opp,
            is_short_sleep_day=(opp is not None and opp < SHORT_SLEEP_THRESHOLD_MIN),
            schedule_variable=None,
        )


# --------------------------------------------------------------------------------------------
# Shift classification — turns a single-calendar "the shift IS the event" feed into the
# day/night kind the shift planner (``sleepctl.shift_manager.Shift``) already understands.
# Deliberately a pure, start-hour heuristic: no per-user configuration needed, and it degrades
# gracefully for any rotation (day/evening/night) a hospital roster throws at it. "call" shifts
# are intentionally NOT auto-classified here — they carry different semantics (post-call
# recovery / drowsy-driving warnings) that only the user should opt into, via the manual picker.
# --------------------------------------------------------------------------------------------

# A shift starting at/after this hour (24h clock) is treated as a night shift...
NIGHT_SHIFT_START_HOUR = 16
# ...or before this hour (i.e. an overnight/early-morning start is still "night").
NIGHT_SHIFT_END_HOUR = 4


def classify_shift(start: datetime) -> str:
    """Classify a shift as ``"day"`` or ``"night"`` from its start hour alone.

    Night: starts in the evening/overnight, hour >= 16 (4pm) or hour < 4 (am). Everything else
    (roughly 04:00-15:59) is a day shift. This intentionally never returns "call" — that kind
    stays manual-only (see module docstring above)."""
    hour = start.hour
    if hour >= NIGHT_SHIFT_START_HOUR or hour < NIGHT_SHIFT_END_HOUR:
        return "night"
    return "day"
