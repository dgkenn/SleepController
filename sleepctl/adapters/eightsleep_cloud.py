"""Tier 0 source/actuator: Eight Sleep cloud via the ``pyEight`` OAuth2 library.

This is the always-available, zero-device-risk path. It reads the minute-level
``intervals`` time-series the Pod uploads (HR/HRV/breath/movement/stage) and issues
unitless -100..100 temperature commands. ``pyEight`` is an OPTIONAL dependency and is
imported lazily, so this module always imports even without credentials/network.

Notes from device research:
- No official API; temperature is unitless (-100..100), mapped to °F by calibration.
- Biometrics arrive with latency (~minute resolution, several-minute lag) -> we set
  ``data_age_seconds`` so the controller can refuse to act on stale data.
- Sleep stage from the cloud is coarse; map best-effort to SleepStage.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sleepctl.adapters.base import PodSensorSource, ThermalActuator
from sleepctl.models import NightSummary, SensorFrame, SleepStage


def _require_pyeight():
    try:
        import pyeight  # type: ignore  # noqa: F401

        return pyeight
    except Exception as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "pyEight is required for the Eight Sleep cloud adapter. "
            "Install it with `pip install pyEight` (the lukas-clarke OAuth2 fork)."
        ) from exc


_STAGE_MAP = {
    "awake": SleepStage.AWAKE,
    "out": SleepStage.AWAKE,
    "light": SleepStage.LIGHT,
    "deep": SleepStage.DEEP,
    "rem": SleepStage.REM,
}


def map_stage(raw: Optional[str]) -> SleepStage:
    if not raw:
        return SleepStage.UNKNOWN
    key = raw.lower().split(":")[-1]
    return _STAGE_MAP.get(key, SleepStage.UNKNOWN)


class EightSleepCloudSource(PodSensorSource):
    def __init__(
        self,
        email: str,
        password: str,
        timezone: str = "UTC",
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        side: str = "left",
    ) -> None:
        self.email = email
        self.password = password
        self.timezone = timezone
        self.client_id = client_id
        self.client_secret = client_secret
        self.side = side
        self._api = None  # lazily constructed live client

    def _client(self):  # pragma: no cover - requires live device
        if self._api is None:
            pyeight = _require_pyeight()
            self._api = pyeight.EightSleep(
                self.email,
                self.password,
                self.timezone,
                client_id=self.client_id,
                client_secret=self.client_secret,
            )
        return self._api

    def read_frame(self) -> SensorFrame:  # pragma: no cover - requires live device
        api = self._client()
        user = api.users[next(iter(api.users))] if getattr(api, "users", None) else None
        if user is None:
            raise RuntimeError("No Eight Sleep user/side available")
        ts = getattr(user, "current_values_datetime", None) or datetime.now()
        age = (datetime.now() - ts).total_seconds() if isinstance(ts, datetime) else None
        return SensorFrame(
            timestamp=ts if isinstance(ts, datetime) else datetime.now(),
            stage=map_stage(getattr(user, "current_sleep_stage", None)),
            stage_confidence=getattr(user, "current_sleep_quality", None),
            heart_rate=getattr(user, "current_heart_rate", None),
            hrv=getattr(user, "current_hrv", None),
            respiratory_rate=getattr(user, "current_resp_rate", None),
            movement=getattr(user, "current_tnt", None),
            presence=getattr(user, "bed_presence", None),
            bed_temp_f=getattr(user, "current_bed_temp_f", None),
            room_temp_f=getattr(api, "room_temperature_f", None),
            commanded_level=getattr(user, "heating_level", None),
            data_age_seconds=age,
        )

    def fetch_night_summary(self, date: str) -> NightSummary:  # pragma: no cover
        # Real implementation maps the user's session/intervals for `date` into the
        # NightSummary fields; unavailable fields remain None.
        return NightSummary(date=date)

    def capabilities(self) -> dict:
        """Expected Pod 2 capabilities; a live probe (calibrate CLI) refines this."""
        return {
            "source": "eightsleep_cloud",
            "real_time": False,
            "biometric_latency": "~minute-level via intervals, several-min lag",
            "fields_expected": [
                "heart_rate", "hrv", "respiratory_rate", "movement",
                "presence(unreliable)", "bed_temp_f", "room_temp_f", "sleep_stage(coarse)",
            ],
            "commands_expected": [
                "set_heating_level", "set_smart_heating_level", "alarm", "away", "prime",
            ],
            "note": "Validate against Pod 2 with the calibrate CLI; degrade on missing fields.",
        }


class EightSleepCloudActuator(ThermalActuator):
    def __init__(self, source: EightSleepCloudSource) -> None:
        self.source = source
        self._last = 0

    def _user(self):  # pragma: no cover - requires live device
        api = self.source._client()
        return api.users[next(iter(api.users))]

    @staticmethod
    def _clamp(level: int) -> int:
        return max(-100, min(100, int(level)))

    def set_level(self, level: int, duration_s: int = 0) -> None:  # pragma: no cover
        level = self._clamp(level)
        self._last = level
        self._user().set_heating_level(level, duration_s)

    def set_smart_level(self, level: int, stage: str) -> None:  # pragma: no cover
        self._user().set_smart_heating_level(self._clamp(level), stage)

    def set_alarm(self, time, vibration: int, thermal_level: int) -> None:  # pragma: no cover
        self._user().set_alarm_direct(
            time=time, vibration=vibration, thermal_level=self._clamp(thermal_level)
        )

    def get_current_level(self) -> int:
        return self._last
