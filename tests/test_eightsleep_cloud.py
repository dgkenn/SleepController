"""Unit tests for the pure mapping/parsing logic in ``eightsleep_cloud``.

These tests never touch the network and never require pyEight to be importable —
they construct plain ``SimpleNamespace`` fakes that mimic the attributes pyEight's
``EightSleep``/``EightUser`` expose (``current_heart_rate``, ``current_hrv``,
``bed_presence``, ``device_data``, etc.) and inject them directly into the private
``_eight``/``_user``/``_last_update`` slots of ``EightSleepClient``. This exercises
``read_frame()``, ``device_status()``, and the small pure helpers (``map_stage``,
``_looks_like_fahrenheit``, ``_convert_bed_temp``, ``_room_temp_f``, ``_clamp``,
``_safe``) without any live device or optional dependency.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from sleepctl.adapters.eightsleep_cloud import (
    EightSleepClient,
    _looks_like_fahrenheit,
    _safe,
    map_stage,
)
from sleepctl.models import SleepStage


def _make_client(user=None, eight=None, last_update=None) -> EightSleepClient:
    client = EightSleepClient("u@example.com", "pw", "America/New_York", side="left")
    client._eight = eight
    client._user = user
    client._last_update = last_update
    return client


def _fake_user(**overrides) -> SimpleNamespace:
    defaults = dict(
        current_heart_rate=55,
        current_hrv=70,
        current_breath_rate=13.2,
        current_sleep_stage="light",
        current_bed_temp=40,  # raw unitless level, not already Fahrenheit
        current_room_temp=68.0,
        bed_presence=True,
        heating_level=25,
        target_heating_level=30,
        alarms=[],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _fake_eight(device_data=None, room_temperature=None, convert=None) -> SimpleNamespace:
    def default_convert(raw, unit):
        # crude but deterministic fake conversion: raw level -> plausible °F
        return 80.0 + float(raw) / 10.0

    return SimpleNamespace(
        device_data=device_data if device_data is not None else {},
        room_temperature=room_temperature,
        convert_raw_bed_temp_to_degrees=convert or default_convert,
    )


# --------------------------------------------------------------------------------------
# map_stage
# --------------------------------------------------------------------------------------


class TestMapStage:
    def test_none_and_empty(self):
        assert map_stage(None) is SleepStage.UNKNOWN
        assert map_stage("") is SleepStage.UNKNOWN

    def test_plain_values(self):
        assert map_stage("awake") is SleepStage.AWAKE
        assert map_stage("out") is SleepStage.AWAKE
        assert map_stage("light") is SleepStage.LIGHT
        assert map_stage("deep") is SleepStage.DEEP
        assert map_stage("rem") is SleepStage.REM

    def test_colon_prefixed(self):
        assert map_stage("asleep:deep") is SleepStage.DEEP
        assert map_stage("asleep:rem") is SleepStage.REM

    def test_camel_case_compound(self):
        assert map_stage("asleepDeep") is SleepStage.DEEP
        assert map_stage("asleepLight") is SleepStage.LIGHT
        assert map_stage("asleepRem") is SleepStage.REM

    def test_case_insensitive(self):
        assert map_stage("DEEP") is SleepStage.DEEP
        assert map_stage("Light") is SleepStage.LIGHT

    def test_unrecognized_falls_back_unknown(self):
        assert map_stage("frobnicating") is SleepStage.UNKNOWN


# --------------------------------------------------------------------------------------
# _looks_like_fahrenheit / _safe
# --------------------------------------------------------------------------------------


class TestLooksLikeFahrenheit:
    def test_none(self):
        assert _looks_like_fahrenheit(None) is False

    def test_in_range(self):
        assert _looks_like_fahrenheit(50.0) is True
        assert _looks_like_fahrenheit(75.0) is True
        assert _looks_like_fahrenheit(120.0) is True

    def test_out_of_range_low(self):
        assert _looks_like_fahrenheit(49.9) is False
        assert _looks_like_fahrenheit(-20) is False

    def test_out_of_range_high(self):
        assert _looks_like_fahrenheit(120.1) is False


class TestSafe:
    def test_returns_value_on_success(self):
        assert _safe(lambda: 42) == 42

    def test_returns_default_on_exception(self):
        def boom():
            raise IndexError("no device data")

        assert _safe(boom) is None
        assert _safe(boom, default="fallback") == "fallback"

    def test_returns_default_false_is_respected(self):
        # default=False must be returned as-is (not falsy-coerced away)
        def boom():
            raise RuntimeError()

        assert _safe(boom, False) is False


# --------------------------------------------------------------------------------------
# EightSleepClient._clamp
# --------------------------------------------------------------------------------------


class TestClamp:
    def test_within_range(self):
        assert EightSleepClient._clamp(0) == 0
        assert EightSleepClient._clamp(50) == 50
        assert EightSleepClient._clamp(-50) == -50

    def test_clamps_high(self):
        assert EightSleepClient._clamp(500) == 100

    def test_clamps_low(self):
        assert EightSleepClient._clamp(-500) == -100

    def test_accepts_float_like_and_truncates(self):
        assert EightSleepClient._clamp(37.9) == 37


# --------------------------------------------------------------------------------------
# EightSleepClient._convert_bed_temp / _room_temp_f (private helpers, pure given fakes)
# --------------------------------------------------------------------------------------


class TestConvertBedTemp:
    def test_none_raw_returns_none(self):
        client = _make_client(eight=_fake_eight())
        assert client._convert_bed_temp(None) is None

    def test_missing_converter_returns_none(self):
        eight = SimpleNamespace(device_data={})  # no convert_raw_bed_temp_to_degrees attr
        client = _make_client(eight=eight)
        assert client._convert_bed_temp(40) is None

    def test_delegates_to_pyeight_converter(self):
        client = _make_client(eight=_fake_eight())
        assert client._convert_bed_temp(40) == pytest.approx(84.0)

    def test_converter_raising_is_swallowed(self):
        def raising_convert(raw, unit):
            raise ValueError("bad raw value")

        client = _make_client(eight=_fake_eight(convert=raising_convert))
        assert client._convert_bed_temp(40) is None


class TestRoomTempF:
    def test_user_room_temp_in_fahrenheit_range_used(self):
        user = _fake_user(current_room_temp=68.0)
        client = _make_client(user=user, eight=_fake_eight())
        assert client._room_temp_f(user) == 68.0

    def test_user_room_temp_out_of_range_rejected(self):
        # e.g. a stray Celsius value (20.0) misread as the "room temp" field
        user = _fake_user(current_room_temp=20.0)
        client = _make_client(user=user, eight=_fake_eight())
        assert client._room_temp_f(user) is None

    def test_falls_back_to_eight_room_temperature_when_user_field_missing(self):
        user = _fake_user()
        del user.current_room_temp  # simulate a Pod 2 missing this field entirely
        client = _make_client(user=user, eight=_fake_eight(room_temperature=70.0))
        assert client._room_temp_f(user) == 70.0

    def test_fallback_also_rejected_if_not_fahrenheit_like(self):
        user = _fake_user()
        del user.current_room_temp
        client = _make_client(user=user, eight=_fake_eight(room_temperature=21.0))
        assert client._room_temp_f(user) is None

    def test_user_field_raising_falls_back(self):
        user = _fake_user()

        class Raising:
            def __get__(self, obj, objtype=None):
                raise IndexError("unloaded")

        # Attach a raising descriptor for current_room_temp via a small subclass instance.
        class Weird:
            current_room_temp = Raising()

        weird_user = Weird()
        client = _make_client(user=weird_user, eight=_fake_eight(room_temperature=71.5))
        assert client._room_temp_f(weird_user) == 71.5


# --------------------------------------------------------------------------------------
# EightSleepClient.read_frame
# --------------------------------------------------------------------------------------


class TestReadFrame:
    def test_maps_all_fields(self):
        user = _fake_user(
            current_heart_rate=58,
            current_hrv=72,
            current_breath_rate=14.1,
            current_sleep_stage="asleep:deep",
            current_bed_temp=40,
            current_room_temp=69.0,
            bed_presence=True,
            heating_level=15,
            target_heating_level=20,
        )
        eight = _fake_eight()
        last_update = datetime.now() - timedelta(seconds=12)
        client = _make_client(user=user, eight=eight, last_update=last_update)

        frame = client.read_frame()

        assert frame.timestamp == last_update
        assert frame.stage is SleepStage.DEEP
        assert frame.heart_rate == 58
        assert frame.hrv == 72
        assert frame.respiratory_rate == 14.1
        assert frame.presence is True
        assert frame.bed_temp_f == pytest.approx(84.0)  # 80 + 40/10 via fake converter
        assert frame.room_temp_f == 69.0
        assert frame.commanded_level == 15
        assert frame.device_level == 15
        assert frame.target_level == 20
        assert frame.data_age_seconds == pytest.approx(12.0, abs=1.0)

    def test_bed_temp_already_fahrenheit_skips_conversion(self):
        # If current_bed_temp already looks like a plausible Fahrenheit reading,
        # read_frame should NOT run it through the raw->degrees converter.
        def exploding_convert(raw, unit):
            raise AssertionError("should not be called when value already looks like F")

        user = _fake_user(current_bed_temp=82.0)
        eight = _fake_eight(convert=exploding_convert)
        client = _make_client(user=user, eight=eight, last_update=datetime.now())

        frame = client.read_frame()
        assert frame.bed_temp_f == 82.0

    def test_raises_if_no_user_connected(self):
        client = _make_client(user=None, eight=_fake_eight())
        with pytest.raises(RuntimeError):
            client.read_frame()

    def test_no_last_update_yields_none_age_and_uses_now(self):
        user = _fake_user()
        client = _make_client(user=user, eight=_fake_eight(), last_update=None)
        frame = client.read_frame()
        assert frame.data_age_seconds is None
        # timestamp falls back to "now" when there has been no successful update yet
        assert (datetime.now() - frame.timestamp).total_seconds() < 5

    def test_missing_or_raising_fields_do_not_crash_read_frame(self):
        """Pod 2 often omits fields entirely; some pyEight properties raise (e.g.
        IndexError from unloaded device data) instead of returning None. read_frame
        must survive all of that via the ``_safe`` wrapper."""

        class FlakyUser:
            current_sleep_stage = "light"
            bed_presence = True

            @property
            def current_heart_rate(self):
                raise IndexError("device data not loaded")

            @property
            def current_hrv(self):
                raise KeyError("missing")

            @property
            def current_breath_rate(self):
                raise RuntimeError("boom")

            @property
            def current_bed_temp(self):
                raise IndexError("unloaded")

            @property
            def heating_level(self):
                raise IndexError("unloaded")

            @property
            def target_heating_level(self):
                raise IndexError("unloaded")

            # current_room_temp intentionally absent entirely (no attribute)

        flaky = FlakyUser()
        client = _make_client(user=flaky, eight=_fake_eight(), last_update=datetime.now())

        frame = client.read_frame()  # must not raise

        assert frame.stage is SleepStage.LIGHT
        assert frame.presence is True
        assert frame.heart_rate is None
        assert frame.hrv is None
        assert frame.respiratory_rate is None
        assert frame.bed_temp_f is None
        assert frame.commanded_level is None
        assert frame.device_level is None
        assert frame.target_level is None
        assert frame.room_temp_f is None

    def test_presence_false_and_no_stage(self):
        user = _fake_user(current_sleep_stage=None, bed_presence=False)
        client = _make_client(user=user, eight=_fake_eight(), last_update=datetime.now())
        frame = client.read_frame()
        assert frame.stage is SleepStage.UNKNOWN
        assert frame.presence is False


# --------------------------------------------------------------------------------------
# EightSleepClient.device_status
# --------------------------------------------------------------------------------------


class TestDeviceStatus:
    def test_parses_full_device_data(self):
        device_data = {
            "online": True,
            "hasWater": True,
            "priming": False,
            "needsPriming": False,
            "isTemperatureAvailable": True,
        }
        client = _make_client(user=_fake_user(alarms=[]), eight=_fake_eight(device_data=device_data))
        status = client.device_status()
        assert status == {
            "online": True,
            "has_water": True,
            "priming": False,
            "needs_priming": False,
            "temp_available": True,
            "last_prime": None,
            "last_low_water": None,
            "device_level": None,
            "device_target_level": None,
            "now_heating": None,
            "now_cooling": None,
            "external_schedule": None,
            "alarm": None,
            "simulated": False,
        }

    def test_parses_new_capacity_and_schedule_fields(self):
        device_data = {
            "online": True,
            "hasWater": True,
            "priming": False,
            "needsPriming": False,
            "isTemperatureAvailable": True,
            "lastPrime": "2026-07-02T01:00:00+00:00",
            "lastLowWater": "2026-07-01T23:00:00+00:00",
            "leftHeatingLevel": 42,
            "leftTargetHeatingLevel": 80,
            "leftNowHeating": False,
            "leftNowCooling": True,
            "leftKelvin": {"currentActivity": "schedule", "currentTargetLevel": 55,
                          "active": True},
            # right-side fields must NOT leak into a left-side client's status
            "rightHeatingLevel": -99,
        }
        client = _make_client(user=_fake_user(alarms=[]), eight=_fake_eight(device_data=device_data))
        status = client.device_status()
        assert status["last_prime"] == "2026-07-02T01:00:00+00:00"
        assert status["last_low_water"] == "2026-07-01T23:00:00+00:00"
        assert status["device_level"] == 42
        assert status["device_target_level"] == 80
        assert status["now_heating"] is False
        assert status["now_cooling"] is True
        assert status["external_schedule"] == {
            "activity": "schedule", "target_level": 55, "active": True,
        }

    def test_right_side_client_reads_right_prefixed_fields(self):
        device_data = {"rightHeatingLevel": 7, "rightTargetHeatingLevel": 10,
                       "rightNowHeating": True, "rightNowCooling": False,
                       "leftHeatingLevel": -1}
        client = _make_client(user=_fake_user(alarms=[]), eight=_fake_eight(device_data=device_data))
        client.side = "right"
        status = client.device_status()
        assert status["device_level"] == 7
        assert status["device_target_level"] == 10
        assert status["now_heating"] is True
        assert status["now_cooling"] is False

    def test_missing_device_data_keys_default_to_none(self):
        client = _make_client(user=_fake_user(alarms=[]), eight=_fake_eight(device_data={}))
        status = client.device_status()
        assert status["online"] is None
        assert status["has_water"] is None
        assert status["priming"] is None
        assert status["needs_priming"] is None
        assert status["temp_available"] is None
        assert status["last_prime"] is None
        assert status["last_low_water"] is None
        assert status["device_level"] is None
        assert status["device_target_level"] is None
        assert status["now_heating"] is None
        assert status["now_cooling"] is None
        assert status["external_schedule"] is None
        assert status["simulated"] is False

    def test_device_data_property_raising_is_tolerated(self):
        class RaisingEight:
            @property
            def device_data(self):
                raise IndexError("device data not loaded")

        client = _make_client(user=_fake_user(alarms=[]), eight=RaisingEight())
        status = client.device_status()
        assert status["online"] is None
        assert status["priming"] is None

    def test_alarm_readback_included_when_present(self):
        device_data = {"online": True, "hasWater": True, "priming": False,
                        "needsPriming": False, "isTemperatureAvailable": True}
        user = _fake_user(alarms=[{"enabled": True, "time": "07:00:00"}])
        client = _make_client(user=user, eight=_fake_eight(device_data=device_data))
        status = client.device_status()
        assert status["alarm"] == {"enabled": True, "time": "07:00:00"}

    def test_alarm_readback_falls_back_to_next_timestamp(self):
        user = _fake_user(alarms=[{"enabled": True, "nextTimestamp": "2026-07-02T07:00:00Z"}])
        client = _make_client(user=user, eight=_fake_eight(device_data={}))
        status = client.device_status()
        assert status["alarm"] == {"enabled": True, "time": "2026-07-02T07:00:00Z"}

    def test_alarm_readback_none_when_not_a_dict(self):
        user = _fake_user(alarms=["unexpected-string-shape"])
        client = _make_client(user=user, eight=_fake_eight(device_data={}))
        status = client.device_status()
        assert status["alarm"] is None

    def test_alarm_readback_none_when_no_alarms(self):
        user = _fake_user(alarms=[])
        client = _make_client(user=user, eight=_fake_eight(device_data={}))
        status = client.device_status()
        assert status["alarm"] is None


# --------------------------------------------------------------------------------------
# EightSleepClient.get_current_level
# --------------------------------------------------------------------------------------


class TestGetCurrentLevel:
    def test_returns_int_level(self):
        client = _make_client(user=_fake_user(heating_level=42))
        assert client.get_current_level() == 42

    def test_none_level_defaults_to_zero(self):
        client = _make_client(user=_fake_user(heating_level=None))
        assert client.get_current_level() == 0


# --------------------------------------------------------------------------------------
# EightSleepClient.prime_pod -- swallow 409 "already priming", propagate everything else
# --------------------------------------------------------------------------------------


class _FakeHTTPError(Exception):
    def __init__(self, status):
        super().__init__(f"http error {status}")
        self.status = status


def _run(coro):
    return asyncio.run(coro)


class TestPrimePod:
    def test_swallows_409_already_priming(self):
        async def failing_prime():
            try:
                raise RuntimeError("Conflict")
            except RuntimeError as inner:
                # Simulate aiohttp's ClientResponseError chained as __cause__ with .status
                raise RuntimeError("prime_pod failed") from _FakeHTTPError(409)

        user = SimpleNamespace(prime_pod=failing_prime)
        client = _make_client(user=user)

        _run(client.prime_pod())  # must not raise

    def test_propagates_non_409_errors(self):
        async def failing_prime():
            raise RuntimeError("prime_pod failed") from _FakeHTTPError(500)

        user = SimpleNamespace(prime_pod=failing_prime)
        client = _make_client(user=user)

        with pytest.raises(RuntimeError):
            _run(client.prime_pod())

    def test_propagates_errors_with_no_cause_at_all(self):
        async def failing_prime():
            raise RuntimeError("no cause here")

        user = SimpleNamespace(prime_pod=failing_prime)
        client = _make_client(user=user)

        with pytest.raises(RuntimeError):
            _run(client.prime_pod())

    def test_success_path_does_not_raise(self):
        async def ok_prime():
            return None

        user = SimpleNamespace(prime_pod=ok_prime)
        client = _make_client(user=user)

        _run(client.prime_pod())


# --------------------------------------------------------------------------------------
# EightSleepClient.capabilities -- static baseline, no device required
# --------------------------------------------------------------------------------------


class TestCapabilities:
    def test_returns_expected_shape(self):
        client = _make_client()
        caps = client.capabilities()
        assert caps["source"] == "eightsleep_cloud"
        assert caps["real_time"] is False
        assert "current_heart_rate" in caps["fields_expected"]
        assert "set_heating_level" in caps["commands_expected"]
