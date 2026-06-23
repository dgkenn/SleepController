"""Calendar context: required wake time + first commitment + short-night flag.

``GoogleCalendarSource`` pulls from Google Calendar (libraries imported lazily, so this
module imports without them). ``ManualCalendarSource`` is a dependency-free fallback for
offline/testing use.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

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
