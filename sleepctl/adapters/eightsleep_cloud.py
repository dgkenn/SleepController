"""Tier 0: Eight Sleep cloud via the async ``pyEight`` (lukas-clarke) library.

The maintained pyEight fork is **async** (asyncio). ``EightSleepClient`` wraps it for the
live daemon: ``await connect()``, periodic ``await update()``, a sync ``read_frame()`` that
snapshots the user's current properties, and ``await set_heating_level()``. ``pyEight`` is
an OPTIONAL dependency, imported lazily, so this module always imports without it.

Device facts honored here:
- No official API; temperature is a unitless level in [-100, 100], mapped to °F by the
  controller's calibration (see ``controller/thermal.py``).
- Biometrics arrive with latency; we stamp ``data_age_seconds`` from the last successful
  update so the controller can refuse to act on stale data.
- Silence: alarms are programmed thermal-only (vibration disabled) by default.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sleepctl.adapters.base import PodSensorSource, ThermalActuator
from sleepctl.models import NightSummary, SensorFrame, SleepStage


def _import_pyeight():
    try:
        from pyeight.eight import EightSleep  # type: ignore

        return EightSleep
    except Exception as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "pyEight is required for the Eight Sleep cloud adapter. Install it with "
            "`pip install pyEight` (the lukas-clarke OAuth2 fork)."
        ) from exc


_STAGE_MAP = {
    "awake": SleepStage.AWAKE,
    "out": SleepStage.AWAKE,
    "light": SleepStage.LIGHT,
    "deep": SleepStage.DEEP,
    "rem": SleepStage.REM,
}


def map_stage(raw: Optional[str]) -> SleepStage:
    """Map a pyEight sleep-stage string (e.g. 'asleep:deep') to SleepStage."""
    if not raw:
        return SleepStage.UNKNOWN
    key = raw.lower().split(":")[-1].strip()
    return _STAGE_MAP.get(key, SleepStage.UNKNOWN)


def _looks_like_fahrenheit(value: Optional[float]) -> bool:
    return value is not None and 50.0 <= value <= 120.0


class EightSleepClient:
    """Async wrapper around the pyEight EightSleep client for one bed side."""

    def __init__(
        self,
        email: str,
        password: str,
        timezone: str = "UTC",
        side: str = "left",
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ) -> None:
        self.email = email
        self.password = password
        self.timezone = timezone
        self.side = side
        self.client_id = client_id
        self.client_secret = client_secret
        self._eight = None
        self._user = None
        self._last_update: Optional[datetime] = None

    # -- lifecycle ---------------------------------------------------------------
    async def connect(self) -> None:  # pragma: no cover - requires live device
        EightSleep = _import_pyeight()
        self._eight = EightSleep(
            self.email,
            self.password,
            self.timezone,
            client_id=self.client_id,
            client_secret=self.client_secret,
        )
        await self._eight.start()
        self._user = self._select_user()

    def _select_user(self):  # pragma: no cover - requires live device
        eight = self._eight
        uid = None
        fetch = getattr(eight, "fetch_user_id", None)
        if fetch is not None:
            uid = fetch(self.side)
        if uid is None and getattr(eight, "users", None):
            uid = next(iter(eight.users))
        if uid is None:
            raise RuntimeError("No Eight Sleep user/side available")
        return eight.users[uid]

    async def update(self) -> None:  # pragma: no cover - requires live device
        await self._eight.update_user_data()
        await self._eight.update_device_data()
        self._last_update = datetime.now()

    async def close(self) -> None:  # pragma: no cover - requires live device
        if self._eight is not None:
            await self._eight.stop()

    # -- sensing -----------------------------------------------------------------
    def read_frame(self) -> SensorFrame:  # pragma: no cover - requires live device
        user = self._user
        if user is None:
            raise RuntimeError("connect() must be called before read_frame()")
        now = datetime.now()
        age = (now - self._last_update).total_seconds() if self._last_update else None

        bed_temp = getattr(user, "current_bed_temp", None)
        bed_temp_f = bed_temp if _looks_like_fahrenheit(bed_temp) else self._convert_bed_temp(bed_temp)

        return SensorFrame(
            timestamp=self._last_update or now,
            stage=map_stage(getattr(user, "current_sleep_stage", None)),
            stage_confidence=None,
            heart_rate=getattr(user, "current_heart_rate", None),
            hrv=getattr(user, "current_hrv", None),
            respiratory_rate=getattr(user, "current_breath_rate", None),
            movement=None,
            presence=getattr(user, "bed_presence", None),
            bed_temp_f=bed_temp_f,
            room_temp_f=self._room_temp_f(user),
            commanded_level=getattr(user, "heating_level", None),
            data_age_seconds=age,
        )

    def _convert_bed_temp(self, raw):  # pragma: no cover - requires live device
        conv = getattr(self._eight, "convert_raw_bed_temp_to_degrees", None)
        if raw is None or conv is None:
            return None
        try:
            return conv(raw, "f")
        except Exception:
            return None

    def _room_temp_f(self, user):  # pragma: no cover - requires live device
        val = getattr(user, "current_room_temp", None)
        if val is None:
            val = getattr(self._eight, "room_temperature", None)
        return val if _looks_like_fahrenheit(val) else None

    # -- acting ------------------------------------------------------------------
    @staticmethod
    def _clamp(level: int) -> int:
        return max(-100, min(100, int(level)))

    async def set_heating_level(self, level: int, duration_s: int = 0) -> None:  # pragma: no cover
        await self._user.set_heating_level(self._clamp(level), duration_s)

    async def set_smart_level(self, level: int, sleep_stage: str) -> None:  # pragma: no cover
        await self._user.set_smart_heating_level(self._clamp(level), sleep_stage)

    async def set_thermal_alarm(self, alarm_id, time, thermal_level: int) -> None:  # pragma: no cover
        """Program a thermal-only alarm (vibration + audio disabled for silence)."""
        await self._user.set_alarm_direct(
            alarm_id=alarm_id,
            enabled=True,
            time=time,
            weekdays=None,
            vibration_enabled=False,
            vibration_power=0,
            vibration_pattern=None,
            thermal_enabled=True,
            thermal_level=self._clamp(thermal_level),
            audio_enabled=False,
            audio_level=0,
            audio_track=None,
            smart_light_sleep=True,
            smart_sleep_cap=True,
            smart_sleep_cap_minutes=0,
        )

    def get_current_level(self) -> int:  # pragma: no cover - requires live device
        return int(getattr(self._user, "heating_level", 0) or 0)

    async def fetch_night_summary(self, date: str) -> NightSummary:  # pragma: no cover
        # Best-effort: the pyEight user trends/intervals expose nightly metrics; map what is
        # available, leave the rest None. Detailed mapping refined against a live Pod 2.
        return NightSummary(date=date)

    def capabilities(self) -> dict:
        """Expected Pod 2 capabilities; the calibrate CLI refines this against the device."""
        return {
            "source": "eightsleep_cloud",
            "real_time": False,
            "biometric_latency": "~minute-level via intervals, several-min lag",
            "fields_expected": [
                "current_heart_rate", "current_hrv", "current_breath_rate",
                "current_sleep_stage", "current_bed_temp", "current_room_temp",
                "bed_presence(unreliable)", "heating_level",
            ],
            "commands_expected": [
                "set_heating_level", "set_smart_heating_level", "set_alarm_direct",
                "turn_on_side", "turn_off_side",
            ],
            "note": "Validate against Pod 2 with `sleepctl calibrate`; degrade on missing fields.",
        }


# --------------------------------------------------------------------------------------
# Thin synchronous ABC wrappers for the offline/sync Runtime path. These are convenience
# shims; the live daemon (loop/live.py) uses the async EightSleepClient directly.
# --------------------------------------------------------------------------------------


class EightSleepCloudSource(PodSensorSource):
    def __init__(self, client: EightSleepClient) -> None:
        self.client = client

    def read_frame(self) -> SensorFrame:  # pragma: no cover - requires live device
        return self.client.read_frame()

    def fetch_night_summary(self, date: str) -> NightSummary:  # pragma: no cover
        return NightSummary(date=date)

    def capabilities(self) -> dict:
        return self.client.capabilities()


class EightSleepCloudActuator(ThermalActuator):
    """Sync ABC shim. Live actuation is async via EightSleepClient.set_heating_level."""

    def __init__(self, client: EightSleepClient) -> None:
        self.client = client
        self._last = 0

    def set_level(self, level: int, duration_s: int = 0) -> None:  # pragma: no cover
        raise RuntimeError(
            "Use the async EightSleepClient.set_heating_level via LiveDaemon; the sync "
            "actuator shim cannot drive the async pyEight API."
        )

    def set_smart_level(self, level: int, stage: str) -> None:  # pragma: no cover
        raise RuntimeError("Use the async EightSleepClient.set_smart_level via LiveDaemon.")

    def set_alarm(self, time, vibration: int, thermal_level: int) -> None:  # pragma: no cover
        raise RuntimeError("Use the async EightSleepClient.set_thermal_alarm via LiveDaemon.")

    def get_current_level(self) -> int:  # pragma: no cover
        return self.client.get_current_level()
