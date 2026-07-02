"""Unit tests for the bespoke direct Eight Sleep client (``eightsleep_direct``).

Everything here is fully offline: HTTP is either mocked out at the ``_request``
chokepoint (for higher-level behavior: token caching, device/physiology parsing,
read_frame, actuation payloads) or exercised against a tiny fake ``aiohttp``-shaped
session object (for the actual retry/backoff loop inside ``_request`` itself). No test
ever opens a real socket.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from sleepctl.adapters.eightsleep_direct import (
    APP_API_URL,
    AUTH_URL,
    CLIENT_API_URL,
    EightSleepDirectClient,
    EightSleepRequestError,
    _backoff_delay,
    map_stage,
)
from sleepctl.models import SleepStage


def _run(coro):
    return asyncio.run(coro)


def _make_client(**kwargs) -> EightSleepDirectClient:
    kwargs.setdefault("min_request_interval_s", 0.0)
    return EightSleepDirectClient("user@example.com", "pw", "America/New_York", "left", **kwargs)


# --------------------------------------------------------------------------------------
# (a) token caching + refresh-grant path
# --------------------------------------------------------------------------------------


class TestTokenCaching:
    def test_valid_cached_token_skips_network_entirely(self, tmp_path):
        cache = tmp_path / "eight_token.json"
        cache.write_text(json.dumps({
            "access_token": "cached-access",
            "refresh_token": "cached-refresh",
            "expires_at": time.time() + 7200,  # well past the 5-min buffer
            "user_id": "u1",
            "token_type": "bearer",
        }))
        client = _make_client(token_cache_path=str(cache))

        async def fail_if_called(*a, **kw):
            raise AssertionError("no network call should be made for a valid cached token")
        client._request = fail_if_called

        token = _run(client._ensure_token())
        assert token == "cached-access"

    def test_refresh_grant_used_when_cached_token_near_expiry(self, tmp_path):
        cache = tmp_path / "eight_token.json"
        cache.write_text(json.dumps({
            "access_token": "old-access",
            "refresh_token": "refresh-abc",
            "expires_at": time.time() + 10,  # inside the 5-min buffer -> needs refresh
            "user_id": "u1",
            "token_type": "bearer",
        }))
        client = _make_client(token_cache_path=str(cache))

        grants_seen = []

        async def fake_request(method, url, *, params=None, json_body=None, auth=True):
            assert url == AUTH_URL
            grants_seen.append(json_body["grant_type"])
            assert json_body["grant_type"] == "refresh_token"
            assert json_body["refresh_token"] == "refresh-abc"
            # password must NEVER be sent on the refresh path
            assert "password" not in json_body
            return {
                "access_token": "new-access",
                "refresh_token": "refresh-def",
                "expires_in": 72000,
                "userId": "u1",
                "token_type": "bearer",
            }

        client._request = fake_request
        token = _run(client._ensure_token())

        assert token == "new-access"
        assert grants_seen == ["refresh_token"]
        # persisted back to the cache file
        saved = json.loads(cache.read_text())
        assert saved["access_token"] == "new-access"
        assert saved["refresh_token"] == "refresh-def"

    def test_password_grant_is_fallback_only_when_refresh_fails(self, tmp_path):
        cache = tmp_path / "eight_token.json"
        cache.write_text(json.dumps({
            "access_token": "old-access",
            "refresh_token": "stale-refresh",
            "expires_at": time.time() - 10,  # already expired
            "user_id": "u1",
            "token_type": "bearer",
        }))
        client = _make_client(token_cache_path=str(cache))
        grants_seen = []

        async def fake_request(method, url, *, params=None, json_body=None, auth=True):
            grants_seen.append(json_body["grant_type"])
            if json_body["grant_type"] == "refresh_token":
                raise EightSleepRequestError("401 refresh rejected")
            assert json_body["username"] == "user@example.com"
            assert json_body["password"] == "pw"
            return {"access_token": "pw-access", "refresh_token": "pw-refresh",
                    "expires_in": 72000, "userId": "u1"}

        client._request = fake_request
        token = _run(client._ensure_token())

        assert token == "pw-access"
        assert grants_seen == ["refresh_token", "password"]

    def test_no_cache_goes_straight_to_password_grant(self, tmp_path):
        cache = tmp_path / "does-not-exist.json"
        client = _make_client(token_cache_path=str(cache))
        grants_seen = []

        async def fake_request(method, url, *, params=None, json_body=None, auth=True):
            grants_seen.append(json_body["grant_type"])
            return {"access_token": "fresh", "refresh_token": "r", "expires_in": 72000, "userId": "u1"}

        client._request = fake_request
        token = _run(client._ensure_token())
        assert token == "fresh"
        assert grants_seen == ["password"]
        assert cache.exists()  # written 0600
        assert oct(cache.stat().st_mode)[-3:] == "600"


# --------------------------------------------------------------------------------------
# (b) device parse maps all rich fields into device_status()
# --------------------------------------------------------------------------------------


class TestDeviceStatus:
    def _device_payload(self):
        return {
            "hasWater": True,
            "needsPriming": False,
            "priming": False,
            "lastPrime": "2026-07-01T10:00:00Z",
            "lastLowWater": "2026-06-30T04:00:00Z",
            "online": True,
            "leftHeatingLevel": 12,
            "leftTargetHeatingLevel": 20,
            "leftNowHeating": True,
            "leftKelvin": {"currentActivity": "asleep", "currentTargetLevel": 20, "active": True},
            "sensorInfo": {"connected": True, "model": "pod2", "skuName": "pod2-sku"},
            "firmwareVersion": "1.2.3",
            "lastHeard": "2026-07-02T08:00:00Z",
            "isTemperatureAvailable": True,
        }

    def test_maps_all_recon_fields(self):
        client = _make_client()
        client._device = self._device_payload()
        status = client.device_status()

        assert status["online"] is True
        assert status["has_water"] is True
        assert status["priming"] is False
        assert status["needs_priming"] is False
        assert status["last_prime"] == "2026-07-01T10:00:00Z"
        assert status["last_low_water"] == "2026-06-30T04:00:00Z"
        assert status["device_level"] == 12
        assert status["device_target_level"] == 20
        assert status["now_heating"] is True
        assert status["now_cooling"] is False
        assert status["external_schedule"] == {
            "activity": "asleep", "target_level": 20, "active": True,
        }
        assert status["sensor_connected"] is True
        assert status["firmware_version"] == "1.2.3"
        assert status["last_heard"] == "2026-07-02T08:00:00Z"
        assert status["simulated"] is False

    def test_now_cooling_when_target_negative(self):
        client = _make_client()
        payload = self._device_payload()
        payload["leftTargetHeatingLevel"] = -15
        client._device = payload
        status = client.device_status()
        assert status["now_heating"] is False
        assert status["now_cooling"] is True

    def test_empty_device_defaults_gracefully(self):
        client = _make_client()
        status = client.device_status()
        assert status["online"] is None
        assert status["now_heating"] is False
        assert status["now_cooling"] is False
        assert status["external_schedule"] == {"activity": None, "target_level": None, "active": None}


# --------------------------------------------------------------------------------------
# (c) read_frame() builds a correct SensorFrame incl. bed_temp F conversion + data age
# --------------------------------------------------------------------------------------


class TestReadFrame:
    def test_maps_physiology_and_device_into_frame(self):
        client = _make_client()
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        client._physiology = {
            "heart_rate": 55,
            "hrv": 71,
            "respiratory_rate": 13.2,
            "bed_temp_c": 25.0,   # -> 77.0 F
            "room_temp_c": 20.0,  # -> 68.0 F
            "stage": "asleepDeep",
            "sample_time": now_iso,
        }
        client._device = {"leftHeatingLevel": 10, "leftTargetHeatingLevel": 15}

        frame = client.read_frame()

        assert frame.heart_rate == 55
        assert frame.hrv == 71
        assert frame.respiratory_rate == 13.2
        assert frame.stage is SleepStage.DEEP
        assert frame.bed_temp_f == pytest.approx(77.0)
        assert frame.room_temp_f == pytest.approx(68.0)
        assert frame.device_level == 10
        assert frame.target_level == 15
        assert frame.commanded_level == 10
        assert frame.presence is True
        assert frame.data_age_seconds is not None
        assert frame.data_age_seconds < 5.0

    def test_stale_sample_reports_large_age_and_no_presence(self):
        client = _make_client()
        old_iso = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        client._physiology = {"heart_rate": 60, "hrv": 65, "respiratory_rate": 14.0,
                              "bed_temp_c": 26.0, "room_temp_c": None, "stage": "light",
                              "sample_time": old_iso}
        frame = client.read_frame()
        assert frame.data_age_seconds > 3000
        assert frame.presence is False   # >10 min since last HR sample
        assert frame.room_temp_f is None

    def test_no_physiology_yet_yields_none_age_and_unknown_stage(self):
        client = _make_client()
        frame = client.read_frame()
        assert frame.data_age_seconds is None
        assert frame.stage is SleepStage.UNKNOWN
        assert frame.heart_rate is None
        assert frame.presence is False


# --------------------------------------------------------------------------------------
# (d) rate-limit/backoff retries then returns stale instead of raising
# --------------------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload if payload is not None else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self._payload


class _FakeSession:
    """Mimics aiohttp.ClientSession.request(): returns/raises the next queued item."""

    def __init__(self, queue):
        self._queue = list(queue)
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class TestRequestBackoff:
    def _authed_client(self, queue):
        from sleepctl.adapters.eightsleep_direct import _TokenState

        client = _make_client(max_retries=3)
        client._session = _FakeSession(queue)
        client._token = _TokenState("tok", "ref", time.time() + 9999, "u1")
        return client

    def test_retries_on_429_then_succeeds(self, monkeypatch):
        sleeps = []

        async def fake_sleep(d):
            sleeps.append(d)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        client = self._authed_client([_FakeResponse(429), _FakeResponse(200, {"ok": 1})])
        data = _run(client._request("GET", "https://example.test/x"))

        assert data == {"ok": 1}
        assert client._session.calls == 2
        assert len(sleeps) == 1
        # index-derived jitter, deterministic (not `random`) -- matches _backoff_delay(0)
        assert sleeps[0] == pytest.approx(_backoff_delay(0))

    def test_exhausts_retries_and_raises_typed_error(self, monkeypatch):
        async def fake_sleep(d):
            return None

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        client = self._authed_client([_FakeResponse(503)] * 10)  # more than enough 5xx
        with pytest.raises(EightSleepRequestError):
            _run(client._request("GET", "https://example.test/x"))
        # attempted max_retries + 1 times, no more
        assert client._session.calls == client.max_retries + 1

    def test_update_device_swallows_failure_and_keeps_stale_data(self, monkeypatch):
        async def fake_sleep(d):
            return None

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        client = self._authed_client([_FakeResponse(500)] * 10)
        client._device_id = "device-1"
        stale = {"online": True, "hasWater": True}
        client._device = stale

        _run(client.update_device())  # must not raise

        assert client._device == stale  # unchanged: stale data preserved

    def test_update_physiology_swallows_failure_and_keeps_stale_data(self, monkeypatch):
        async def fake_sleep(d):
            return None

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        client = self._authed_client([_FakeResponse(500)] * 10)
        client._user_id = "u1"
        stale = {"heart_rate": 60}
        client._physiology = stale

        _run(client.update_physiology(force=True))  # must not raise

        assert client._physiology == stale


# --------------------------------------------------------------------------------------
# (e) set_level clamps + issues the right PUT(s)
# --------------------------------------------------------------------------------------


class TestSetHeatingLevel:
    def test_clamps_and_issues_turn_on_then_two_puts(self):
        client = _make_client()
        client._user_id = "u1"
        calls = []

        async def fake_request(method, url, *, params=None, json_body=None, auth=True):
            calls.append((method, url, json_body))
            return {}

        client._request = fake_request
        _run(client.set_heating_level(500, duration_s=1800))  # 500 clamps to 100

        temp_url = f"{APP_API_URL}/users/u1/temperature"
        assert calls[0] == ("PUT", temp_url, {"currentState": {"type": "smart"}})
        assert calls[1] == ("PUT", temp_url, {"currentLevel": 100})
        assert calls[2] == ("PUT", temp_url, {"timeBased": {"level": 100, "durationSeconds": 1800}})

    def test_clamps_negative_out_of_range(self):
        client = _make_client()
        client._user_id = "u1"
        calls = []

        async def fake_request(method, url, *, params=None, json_body=None, auth=True):
            calls.append((method, url, json_body))
            return {}

        client._request = fake_request
        _run(client.set_heating_level(-500))

        assert calls[1][2] == {"currentLevel": -100}
        assert calls[2][2] == {"timeBased": {"level": -100, "durationSeconds": 0}}


# --------------------------------------------------------------------------------------
# (f) drop-in interface parity: same public methods as the pyEight client
# --------------------------------------------------------------------------------------


class TestDropInParity:
    def test_public_methods_are_a_superset_of_the_pyeight_client(self):
        # Importing EightSleepClient never requires pyEight to be installed -- it's a
        # lazy import inside connect() only.
        from sleepctl.adapters.eightsleep_cloud import EightSleepClient

        def _public_methods(cls):
            return {
                name for name in dir(cls)
                if not name.startswith("_") and callable(getattr(cls, name))
            }

        pyeight_methods = _public_methods(EightSleepClient)
        direct_methods = _public_methods(EightSleepDirectClient)
        missing = pyeight_methods - direct_methods
        assert not missing, f"drop-in parity broken -- direct client is missing: {missing}"


# --------------------------------------------------------------------------------------
# small pure helpers
# --------------------------------------------------------------------------------------


class TestMapStage:
    def test_basic(self):
        assert map_stage(None) is SleepStage.UNKNOWN
        assert map_stage("light") is SleepStage.LIGHT
        assert map_stage("asleepDeep") is SleepStage.DEEP
        assert map_stage("rem") is SleepStage.REM
        assert map_stage("out") is SleepStage.AWAKE


class TestBackoffDelay:
    def test_deterministic_not_random(self):
        # same attempt index -> same delay, every time (no `random` involved)
        assert _backoff_delay(1) == _backoff_delay(1)
        assert _backoff_delay(0) < _backoff_delay(1) < _backoff_delay(2)
