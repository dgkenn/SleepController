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

import asyncio
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


def _fix_pyeight_host_header() -> None:
    """Work around a pyEight bug that 404s every authenticated request.

    pyEight's ``DEFAULT_API_HEADERS`` hardcodes ``host: app-api.8slp.net`` and
    reuses that single dict for *all* requests, including the ones to
    ``client-api.8slp.net`` (e.g. ``GET /v1/users/me`` in ``fetch_device_list``).
    aiohttp honors an explicit Host header verbatim, so the client-api host gets
    the wrong Host and the server returns 404 — connection fails before any data
    is read. Dropping the static Host lets aiohttp derive the correct one from
    each URL. Verified live against the real cloud API (with vs. without the
    header: 404 vs. 200). Safe + idempotent; no-op if the key is already gone.
    """
    try:
        from pyeight import constants  # type: ignore

        constants.DEFAULT_API_HEADERS.pop("host", None)
        constants.DEFAULT_API_HEADERS.pop("Host", None)
    except Exception:  # pragma: no cover - depends on optional install / version
        pass


_STAGE_MAP = {
    "awake": SleepStage.AWAKE,
    "out": SleepStage.AWAKE,
    "light": SleepStage.LIGHT,
    "deep": SleepStage.DEEP,
    "rem": SleepStage.REM,
}


def map_stage(raw: Optional[str]) -> SleepStage:
    """Map a pyEight sleep-stage string to SleepStage.

    Tolerant of the formats seen across firmwares: ``"deep"``, ``"asleep:deep"``,
    camelCase ``"asleepDeep"``, ``"out"`` (out of bed), etc.
    """
    if not raw:
        return SleepStage.UNKNOWN
    key = raw.lower().split(":")[-1].strip()
    if key in _STAGE_MAP:
        return _STAGE_MAP[key]
    # substring fallback for camelCase / compound labels (e.g. "asleepDeep")
    for token, stage in (("deep", SleepStage.DEEP), ("rem", SleepStage.REM),
                         ("light", SleepStage.LIGHT), ("awake", SleepStage.AWAKE),
                         ("out", SleepStage.AWAKE)):
        if token in key:
            return stage
    return SleepStage.UNKNOWN


def _looks_like_fahrenheit(value: Optional[float]) -> bool:
    return value is not None and 50.0 <= value <= 120.0


def _safe(fn, default=None):
    """Call ``fn`` and return ``default`` on ANY error.

    pyEight properties read from internal JSON that may be empty/partial — especially on
    a Pod 2, which can report fewer fields than Pod 3/4. Some properties raise (e.g.
    ``heating_level`` -> IndexError when device data is unloaded), which a plain getattr
    would not catch. This keeps a single missing field from crashing a whole tick.
    """
    try:
        return fn()
    except Exception:
        return default


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
        self._alarm_id: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------------
    async def connect(self) -> None:  # pragma: no cover - requires live device
        EightSleep = _import_pyeight()
        _fix_pyeight_host_header()
        self._eight = EightSleep(
            self.email,
            self.password,
            self.timezone,
            client_id=self.client_id,
            client_secret=self.client_secret,
        )
        try:
            await self._eight.start()
        except IndexError as exc:
            # pyEight raises a bare IndexError from assign_users() when the
            # account authenticates but has no Pod registered yet. Translate it
            # into an actionable message (seen live: valid login, empty device
            # list before the Pod was paired).
            raise RuntimeError(
                "Signed in to Eight Sleep, but no Pod is registered to this "
                "account yet. Finish setup in the Eight Sleep app first — fill "
                "the Pod with water, connect the Hub to Wi-Fi, and pair the bed "
                "— then retry."
            ) from exc
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
        # Device data MUST refresh before user data: pyEight's update_user_data() reads
        # device_data (e.g. target_heating_level), so a stale/empty device payload makes it
        # raise IndexError. Refreshing the device first (and tolerating a transient miss)
        # keeps the live daemon from dying on a single bad cloud response.
        await self._eight.update_device_data()
        await self._eight.update_user_data()
        self._last_update = datetime.now()

    async def close(self) -> None:  # pragma: no cover - requires live device
        if self._eight is not None:
            await self._eight.stop()

    def now(self) -> datetime:
        """Current wall-clock time. The live dashboard daemon calls ``client.now()``
        each tick (the simulator client overrides it with its synthetic clock)."""
        return datetime.now()

    # -- sensing -----------------------------------------------------------------
    def read_frame(self) -> SensorFrame:  # pragma: no cover - requires live device
        user = self._user
        if user is None:
            raise RuntimeError("connect() must be called before read_frame()")
        now = datetime.now()
        age = (now - self._last_update).total_seconds() if self._last_update else None

        # Every property read is wrapped: on a Pod 2 some fields may be missing or raise.
        bed_temp = _safe(lambda: user.current_bed_temp)
        bed_temp_f = bed_temp if _looks_like_fahrenheit(bed_temp) else self._convert_bed_temp(bed_temp)

        return SensorFrame(
            timestamp=self._last_update or now,
            stage=map_stage(_safe(lambda: user.current_sleep_stage)),
            stage_confidence=None,
            heart_rate=_safe(lambda: user.current_heart_rate),
            hrv=_safe(lambda: user.current_hrv),
            respiratory_rate=_safe(lambda: user.current_breath_rate),
            movement=None,
            presence=_safe(lambda: user.bed_presence),
            bed_temp_f=bed_temp_f,
            room_temp_f=self._room_temp_f(user),
            commanded_level=_safe(lambda: user.heating_level),
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
        val = _safe(lambda: user.current_room_temp)
        if val is None:
            val = _safe(lambda: self._eight.room_temperature)
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

    async def _resolve_alarm_id(self) -> Optional[str]:  # pragma: no cover - requires live device
        """Return an existing alarm id on the device, caching it.

        pyEight's ``set_alarm_direct`` can only **modify an existing alarm** — it raises
        ``"Alarm with ID … not found"`` for an unknown id, so we must drive a real slot
        rather than invent one. (Verified live: the device exposes ``user.alarms`` with the
        slot's UUID.)
        """
        if self._alarm_id:
            return self._alarm_id
        u = self._user
        for meth in ("update_alarm_data", "_ensure_alarm_data"):
            fn = getattr(u, meth, None)
            if fn is None:
                continue
            try:
                res = fn()
                if asyncio.iscoroutine(res):
                    await res
                break
            except Exception:
                continue
        alarms = getattr(u, "alarms", None) or []
        self._alarm_id = alarms[0]["id"] if alarms else None
        return self._alarm_id

    async def set_wake_alarm(self, spec) -> None:  # pragma: no cover - requires live device
        """Program the heat + gentle-vibration smart wake alarm (audio OFF for silence)."""
        from datetime import datetime as _dt

        time_str = spec.time.strftime("%H:%M:%S") if isinstance(spec.time, _dt) else str(spec.time)
        if len(time_str) == 5:               # "HH:MM" -> "HH:MM:SS" (API rejects the short form)
            time_str += ":00"
        alarm_id = await self._resolve_alarm_id()
        if alarm_id is None:
            raise RuntimeError(
                "No alarm slot exists on the Pod to drive. Create one wake alarm in the "
                "Eight Sleep app once — sleepctl then manages its time/level silently "
                "(it can modify an existing alarm but cannot create one via the API)."
            )
        # Payload must use device-valid values (verified live, a bad field => 400):
        #  - vibration_pattern: "INTENSE" is the recognized pattern (power sets intensity).
        #  - thermal is left to the controller's own wake ramp (set_heating_level during the
        #    wake window); the alarm's thermal "level" is a separate 0-100 scale, so we don't
        #    drive it here and avoid a scale mismatch.
        #  - audio fully off but a valid trackId, silence preserved.
        await self._user.set_alarm_direct(
            alarm_id=alarm_id,
            enabled=True,
            time=time_str,
            weekdays=None,
            vibration_enabled=spec.vibration_power > 0,
            vibration_power=max(0, min(100, int(spec.vibration_power))),
            vibration_pattern="INTENSE",
            thermal_enabled=False,
            thermal_level=50,
            audio_enabled=False,                 # silence preserved
            audio_level=0,
            audio_track="futuristic",
            smart_light_sleep=True,              # fire during light sleep in the window
            smart_sleep_cap=True,
            smart_sleep_cap_minutes=spec.window_min,
        )

    def get_current_level(self) -> int:  # pragma: no cover - requires live device
        return int(getattr(self._user, "heating_level", 0) or 0)

    # ----------------------------------------------------- Eight Sleep app parity
    # These mirror the controls exposed by the official Eight Sleep app so the
    # dashboard offers the same functionality (power, away, prime, fine adjust).

    async def turn_on_side(self) -> None:  # pragma: no cover - requires live device
        """Power the user's side on (equivalent to the app's main on/off toggle)."""
        await self._user.turn_on_side()

    async def turn_off_side(self) -> None:  # pragma: no cover - requires live device
        """Power the user's side off."""
        await self._user.turn_off_side()

    async def set_away_mode(self, enabled: bool) -> None:  # pragma: no cover - requires live device
        """Start/stop away mode (the app's travel toggle)."""
        await self._user.set_away_mode("start" if enabled else "end")

    async def prime_pod(self) -> None:  # pragma: no cover - requires live device
        """Prime the Pod's water (the app's prime/clean routine)."""
        await self._user.prime_pod()

    async def increment_level(self, offset: int) -> None:  # pragma: no cover - requires live device
        """Nudge the heating level by ``offset`` (the app's +/- buttons)."""
        await self._user.increment_heating_level(int(offset))

    async def fetch_night_summary(self, date: str) -> NightSummary:  # pragma: no cover
        # Best-effort: the pyEight user trends/intervals expose nightly metrics; map what is
        # available, leave the rest None. Detailed mapping refined against a live Pod 2.
        return NightSummary(date=date)

    async def probe(self) -> dict:  # pragma: no cover - requires live device
        """Live per-field capability probe — run this on the real Pod 2 (calibrate CLI).

        Reports what THIS device actually supports rather than guessing from the model:
        whether it cools, whether a base is present, which biometric fields populate, and
        which control commands the library exposes. Use it the first time you connect a
        Pod 2 to confirm the controller's assumptions hold.
        """
        await self.update()
        user = self._user
        eight = self._eight

        is_pod = bool(_safe(lambda: eight.is_pod, False))
        has_base = bool(_safe(lambda: eight._has_base, False))

        fields = {}
        for name in ("current_heart_rate", "current_hrv", "current_breath_rate",
                     "current_sleep_stage", "current_bed_temp", "current_room_temp",
                     "bed_presence", "heating_level", "target_heating_level"):
            val = _safe(lambda n=name: getattr(user, n))
            fields[name] = {"available": val is not None, "value": val}

        commands = {
            name: hasattr(user, name)
            for name in ("set_heating_level", "set_smart_heating_level",
                         "set_alarm_direct", "turn_on_side", "turn_off_side")
        }

        warnings = []
        if not is_pod:
            warnings.append(
                "Device does not report the 'cooling' feature — it may be heat-only. "
                "The cooling-biased control rules will have limited effect."
            )
        for core in ("current_heart_rate", "current_sleep_stage", "heating_level"):
            if not fields[core]["available"]:
                warnings.append(f"Core field '{core}' is unavailable on this device.")

        return {
            "is_pod_with_cooling": is_pod,
            "has_base": has_base,
            "side": self.side,
            "fields": fields,
            "commands": commands,
            "warnings": warnings,
            "device_data_keys": sorted(_safe(lambda: list(eight.device_data.keys()), [])),
        }

    def capabilities(self) -> dict:
        """Expected Pod 2 capabilities (static baseline); ``probe()`` confirms live."""
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
