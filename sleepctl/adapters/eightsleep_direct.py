"""Tier 0(b): a BESPOKE, direct Eight Sleep Pod 2 cloud client (no pyEight dependency).

Why this exists: pyEight re-authenticates far more than the API requires, fetches the
full user/device payload on every tick regardless of what actually changed, and mixes a
slow batch/processed physiology feed with the fast device-state feed. This module talks to
the cloud API directly, matching the endpoints/payloads reconnoitred live against
production (verified 2026-07):

- Auth:     ``POST https://auth-api.8slp.net/v1/tokens``           (~860ms; ~20h token life)
- Device:   ``GET  https://client-api.8slp.net/v1/devices/{id}``   (~150ms; safe every few s)
- Identify: ``GET  https://client-api.8slp.net/v1/users/me``       (once, at connect())
- Trends:   ``GET  https://client-api.8slp.net/v1/users/{id}/trends``  (batch, minutes lag)
- Control:  ``PUT/POST https://app-api.8slp.net/v1/...``            (temperature/away/priming/alarms)

Design:
  * ONE persistent ``aiohttp.ClientSession`` (``trust_env=True`` for corporate/dev proxies;
    harmless where no proxy exists), created in ``connect()``, closed in ``close()``.
  * A token cache (access + refresh + expiry) persisted to a 0600 file next to the user's
    credentials, refreshed near expiry (5 min buffer) via ``grant_type=refresh_token``,
    falling back to a full password grant only when refresh fails or no cache exists.
  * Two independently-cadenced reads: ``update_device()`` (fast, every control tick) and
    ``update_physiology()`` (throttled to ~60s by default; internally a no-op if called
    again before the interval elapses). ``update()`` composes both, matching the pyEight
    adapter's ``update(user=True, device=True)`` signature so it drops in unchanged.
  * A single ``_request()`` chokepoint for every HTTP call: attaches the bearer token,
    enforces a per-client minimum request spacing, retries 429/5xx/timeout/connection
    errors with exponential backoff (deterministic index-derived jitter -- no ``random``,
    so retries are reproducible in tests and logs), and never raises out of the read path
    (``update_device``/``update_physiology`` log a warning and keep the last-known-good
    cache on failure). Control/actuation calls DO propagate errors -- the live daemon's
    per-tick error handler already tolerates and logs those.

This is a DROP-IN replacement for ``sleepctl.adapters.eightsleep_cloud.EightSleepClient``:
same public method names/signatures (``connect``, ``update``, ``read_frame``,
``device_status``, ``set_heating_level``, ``set_smart_level``, ``turn_on_side``,
``turn_off_side``, ``set_away_mode``, ``prime_pod``, ``increment_level``,
``set_thermal_alarm``, ``set_wake_alarm``, ``fetch_night_summary``, ``probe``,
``capabilities``, ``get_current_level``, ``now``, ``close``). See
``sleepctl/adapters/eightsleep_cloud.py`` for the reference interface (not imported here,
by design, to avoid coupling two adapters under concurrent development -- the stage map and
SensorFrame construction are re-implemented locally, tiny as they are).

## Live smoke (manual)

Never hit the network from automated tests. To sanity-check this module against the REAL
API by hand::

    python - <<'PY'
    import asyncio
    from sleepctl.adapters.credentials import load_credentials
    from sleepctl.adapters.eightsleep_direct import EightSleepDirectClient

    async def main():
        creds = load_credentials()
        client = EightSleepDirectClient(
            creds.email, creds.password, creds.timezone, creds.side,
            creds.client_id, creds.client_secret,
        )
        await client.connect()          # auth (cached after first run) + /users/me + device + trends
        await client.update()
        print(client.device_status())
        print(client.read_frame())
        await client.close()

    asyncio.run(main())
    PY

First run pays the ~860ms auth cost and writes ``~/.config/sleepctl/eight_token.json``
(0600); subsequent runs within ~20h skip straight to the ~150ms device GET.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from sleepctl.models import NightSummary, SensorFrame, SleepStage

_LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
# Endpoints + known public client credentials (these are NOT user secrets -- pyEight and
# every other community client ship them; they identify the "app", not the account).
# --------------------------------------------------------------------------------------

AUTH_URL = "https://auth-api.8slp.net/v1/tokens"
CLIENT_API_URL = "https://client-api.8slp.net/v1"
APP_API_URL = "https://app-api.8slp.net/v1"

KNOWN_CLIENT_ID = "0894c7f33bb94800a03f1f4df13a4f38"
KNOWN_CLIENT_SECRET = "f0954a3ed5763ba3d06834c73731a32f15f168f47d4f164751275def86db0c76"

DEFAULT_TOKEN_CACHE_PATH = Path.home() / ".config" / "sleepctl" / "eight_token.json"

TOKEN_EXPIRY_BUFFER_S = 300.0   # re-auth if the cached token expires within 5 min
DEFAULT_PHYSIOLOGY_INTERVAL_S = 60.0
# Sensed physiology (trends timeseries) is per-minute + laggy. Beyond this age we treat a sensed
# sample as too old to close the thermal loop on -> null it so control falls to safe open-loop.
# Matches the pyEight bed-presence 10-min window: a sample older than this is not "now".
PHYSIOLOGY_STALE_S = 600.0
DEFAULT_MIN_REQUEST_INTERVAL_S = 0.15
DEFAULT_MAX_RETRIES = 3
DEFAULT_TIMEOUT_S = 20.0

# Deterministic pseudo-jitter table, keyed by retry index -- NOT ``random``, so backoff
# timing is reproducible in logs/tests. Values are small (tens of ms) added on top of the
# exponential base so simultaneous retries across ticks don't perfectly lock-step.
_JITTER_TABLE_S = (0.03, 0.11, 0.07, 0.15, 0.02, 0.09)
_BACKOFF_BASE_S = 0.25


def _backoff_delay(attempt: int) -> float:
    base = _BACKOFF_BASE_S * (2 ** attempt)
    jitter = _JITTER_TABLE_S[attempt % len(_JITTER_TABLE_S)]
    return base + jitter


# --------------------------------------------------------------------------------------
# Typed errors
# --------------------------------------------------------------------------------------


class EightSleepAuthError(Exception):
    """Authentication (password or refresh grant) failed."""


class EightSleepRequestError(Exception):
    """A request failed after exhausting retries (429/5xx/timeout/connection error)."""


# --------------------------------------------------------------------------------------
# Small pure helpers (stage map / temp conversion / iso parsing) -- deliberately NOT
# imported from eightsleep_cloud.py to keep the two adapters decoupled.
# --------------------------------------------------------------------------------------

_STAGE_MAP = {
    "awake": SleepStage.AWAKE,
    "out": SleepStage.AWAKE,
    "light": SleepStage.LIGHT,
    "deep": SleepStage.DEEP,
    "rem": SleepStage.REM,
}


def map_stage(raw: Optional[str]) -> SleepStage:
    """Map an Eight Sleep sleep-stage string to SleepStage (tolerant of case/prefix)."""
    if not raw:
        return SleepStage.UNKNOWN
    key = raw.lower().split(":")[-1].strip()
    if key in _STAGE_MAP:
        return _STAGE_MAP[key]
    for token, stage in (("deep", SleepStage.DEEP), ("rem", SleepStage.REM),
                         ("light", SleepStage.LIGHT), ("awake", SleepStage.AWAKE),
                         ("out", SleepStage.AWAKE)):
        if token in key:
            return stage
    return SleepStage.UNKNOWN


def _c_to_f(celsius: Optional[float]) -> Optional[float]:
    if celsius is None:
        return None
    try:
        return float(celsius) * 9.0 / 5.0 + 32.0
    except (TypeError, ValueError):
        return None


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an Eight Sleep ISO timestamp (may end in 'Z') to a tz-aware UTC datetime."""
    if not value:
        return None
    try:
        v = str(value).strip()
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _safe(fn, default=None):
    """Call ``fn``, returning ``default`` on ANY error -- a single missing/odd field in a
    partial cloud payload must never crash a whole tick."""
    try:
        return fn()
    except Exception:
        return default


# --------------------------------------------------------------------------------------
# Token cache (0600 file next to the user's credentials)
# --------------------------------------------------------------------------------------


@dataclass
class _TokenState:
    access_token: str
    refresh_token: str
    expires_at: float          # epoch seconds
    user_id: str
    token_type: str = "bearer"

    def to_json(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "user_id": self.user_id,
            "token_type": self.token_type,
        }

    @classmethod
    def from_json(cls, data: dict) -> Optional["_TokenState"]:
        try:
            return cls(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token", ""),
                expires_at=float(data["expires_at"]),
                user_id=data.get("user_id", ""),
                token_type=data.get("token_type", "bearer"),
            )
        except (KeyError, TypeError, ValueError):
            return None


class EightSleepDirectClient:
    """Bespoke async client for the Eight Sleep cloud API (Pod 2), talking directly to the
    documented endpoints instead of going through pyEight. Drop-in for
    ``eightsleep_cloud.EightSleepClient`` -- same public surface."""

    def __init__(
        self,
        email: str,
        password: str,
        timezone: str = "UTC",
        side: str = "left",
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        *,
        token_cache_path: Optional[str] = None,
        physiology_interval_s: float = DEFAULT_PHYSIOLOGY_INTERVAL_S,
        min_request_interval_s: float = DEFAULT_MIN_REQUEST_INTERVAL_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        session: Any = None,
    ) -> None:
        self.email = email
        self.password = password
        self.timezone = timezone
        self.side = side
        self.client_id = client_id or KNOWN_CLIENT_ID
        self.client_secret = client_secret or KNOWN_CLIENT_SECRET

        self._token_cache_path = Path(token_cache_path) if token_cache_path else DEFAULT_TOKEN_CACHE_PATH
        self._physiology_interval_s = physiology_interval_s
        self._min_request_interval_s = min_request_interval_s
        self.max_retries = max_retries
        self._timeout_s = timeout_s

        self._session = session
        self._owns_session = False
        self._last_request_mono: float = 0.0

        self._token: Optional[_TokenState] = None
        self._user_id: Optional[str] = None
        self._device_id: Optional[str] = None

        self._device: dict = {}
        self._device_ts: Optional[datetime] = None
        self._physiology: dict = {}
        self._physiology_ts: Optional[datetime] = None
        self._last_update: Optional[datetime] = None

        self._alarm_id: Optional[str] = None

    # ------------------------------------------------------------------ lifecycle
    async def connect(self) -> None:  # pragma: no cover - requires live device
        if self._session is None:
            import aiohttp  # imported lazily so the module always imports without it

            self._session = aiohttp.ClientSession(trust_env=True)
            self._owns_session = True

        await self._ensure_token()

        me = await self._request("GET", f"{CLIENT_API_URL}/users/me")
        user_obj = (me or {}).get("user", {}) or {}
        current_device = user_obj.get("currentDevice") or {}
        device_id = current_device.get("id")
        if not device_id:
            devices = user_obj.get("devices") or []
            device_id = devices[0] if devices else None
        if not device_id:
            raise RuntimeError(
                "Signed in to Eight Sleep, but no Pod is registered to this account yet. "
                "Finish setup in the Eight Sleep app first, then retry."
            )
        self._device_id = str(device_id)
        # Only adopt the API's side when it is a real physical side. It also
        # returns ``solo``/``away`` (away mode), which must NOT overwrite the
        # configured physical side -- otherwise device reads target keys that do
        # not exist and the controller goes blind (see _corrected_side).
        api_side = (current_device.get("side") or "").lower()
        if api_side in ("left", "right"):
            self.side = api_side
        if self._token is not None and self._token.user_id:
            self._user_id = self._token.user_id
        else:
            self._user_id = user_obj.get("userId") or user_obj.get("id")

        # Warm both caches so the very first read_frame()/device_status() has real data.
        await self.update_device()
        await self.update_physiology(force=True)

    async def close(self) -> None:  # pragma: no cover - requires live device
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._session = None

    def now(self) -> datetime:
        """Current wall-clock time (the live dashboard daemon calls ``client.now()``)."""
        return datetime.now()

    # ------------------------------------------------------------------ auth / token cache
    def _load_token_cache(self) -> Optional[_TokenState]:
        try:
            if not self._token_cache_path.exists():
                return None
            data = json.loads(self._token_cache_path.read_text(encoding="utf-8"))
            return _TokenState.from_json(data)
        except (OSError, ValueError):
            return None

    def _save_token_cache(self, token: _TokenState) -> None:
        try:
            self._token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._token_cache_path.write_text(json.dumps(token.to_json(), indent=2), encoding="utf-8")
            os.chmod(self._token_cache_path, 0o600)
        except OSError as exc:  # pragma: no cover - filesystem edge case
            _LOGGER.warning("failed to persist Eight Sleep token cache: %r", exc)

    async def _authenticate(self, grant: str) -> None:
        """``grant``: 'password' or 'refresh_token'. Raises EightSleepAuthError on failure."""
        body = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": grant,
        }
        if grant == "refresh_token":
            if not self._token or not self._token.refresh_token:
                raise EightSleepAuthError("no refresh_token available")
            body["refresh_token"] = self._token.refresh_token
        else:
            body["username"] = self.email
            body["password"] = self.password

        try:
            resp = await self._request("POST", AUTH_URL, json_body=body, auth=False)
        except EightSleepRequestError as exc:
            raise EightSleepAuthError(f"{grant} grant failed: {exc}") from exc

        if not resp or "access_token" not in resp:
            raise EightSleepAuthError(f"malformed auth response: {resp!r}")

        expires_in = float(resp.get("expires_in", 72000))
        prior_refresh = self._token.refresh_token if self._token else ""
        prior_user = self._token.user_id if self._token else ""
        self._token = _TokenState(
            access_token=resp["access_token"],
            refresh_token=resp.get("refresh_token") or prior_refresh,
            expires_at=time.time() + expires_in,
            user_id=str(resp.get("userId") or prior_user or ""),
            token_type=resp.get("token_type", "bearer"),
        )
        self._save_token_cache(self._token)

    async def _ensure_token(self) -> str:
        """Return a valid bearer token, refreshing/re-authenticating only when necessary."""
        if self._token is None:
            self._token = self._load_token_cache()

        now = time.time()
        if self._token and now + TOKEN_EXPIRY_BUFFER_S < self._token.expires_at:
            return self._token.access_token

        if self._token and self._token.refresh_token:
            try:
                await self._authenticate("refresh_token")
                return self._token.access_token
            except EightSleepAuthError as exc:
                _LOGGER.info("refresh_token grant failed (%r); falling back to password grant", exc)

        await self._authenticate("password")
        return self._token.access_token

    # ------------------------------------------------------------------ HTTP chokepoint
    async def _throttle(self) -> None:
        now = time.monotonic()
        wait = self._min_request_interval_s - (now - self._last_request_mono)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_mono = time.monotonic()

    async def _http_call(self, method: str, url: str, params, json_body, headers):
        assert self._session is not None, "connect() must be called (or a session injected) first"
        async with self._session.request(
            method, url, params=params, json=json_body, headers=headers, timeout=self._timeout_s,
        ) as resp:
            status = getattr(resp, "status", None)
            try:
                data = await resp.json(content_type=None)
            except TypeError:
                # Some fakes/older aiohttp signatures don't accept content_type kw.
                data = await resp.json()
            except Exception:
                data = None
            return status, data

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        auth: bool = True,
    ) -> Optional[dict]:
        """Single chokepoint for every HTTP call: auth header, throttling, retry/backoff,
        error typing. Raises ``EightSleepRequestError`` after exhausting retries -- callers
        on the read path (``update_device``/``update_physiology``) catch this and keep
        stale data rather than propagating into the control loop."""
        headers = {"content-type": "application/json", "accept": "application/json"}
        last_exc: Optional[BaseException] = None

        for attempt in range(self.max_retries + 1):
            try:
                await self._throttle()
                if auth:
                    token = await self._ensure_token()
                    headers["authorization"] = f"Bearer {token}"

                status, data = await self._http_call(method, url, params, json_body, headers)

                if status == 401 and auth:
                    # Token may have been revoked/expired server-side ahead of our clock;
                    # drop the cache and force one re-auth, then retry (not counted as a
                    # "failure" attempt against the caller's error path).
                    self._token = None
                    continue

                if status == 429 or (status is not None and status >= 500):
                    raise EightSleepRequestError(f"{method} {url} -> HTTP {status}")
                if status is not None and status >= 400:
                    raise EightSleepRequestError(f"{method} {url} -> HTTP {status}: {data!r}")

                return data if data is not None else {}

            except EightSleepRequestError as exc:
                last_exc = exc
            except (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError) as exc:
                last_exc = exc
            except Exception as exc:  # aiohttp.ClientError et al (kept generic: aiohttp is
                # an optional import, so we can't reference its exception types at module
                # scope without forcing the dependency on every caller).
                last_exc = exc

            if attempt < self.max_retries:
                await asyncio.sleep(_backoff_delay(attempt))

        _LOGGER.warning("giving up on %s %s after %d attempt(s): %r",
                       method, url, self.max_retries + 1, last_exc)
        raise EightSleepRequestError(f"{method} {url} failed: {last_exc!r}") from last_exc

    # ------------------------------------------------------------------ fast path: device
    def _corrected_side(self) -> str:
        """Resolve to a real physical device key prefix (``left``/``right``).

        The device only ever exposes ``left*``/``right*`` heating keys. But
        ``currentDevice.side`` can report non-physical values -- ``solo`` (single
        occupant) and, critically, ``away`` while away mode is active. Returning
        those verbatim makes every read target a key that does not exist
        (``awayTargetHeatingLevel`` -> ``None``), silently blinding the whole
        controller. Anything that is not ``left``/``right`` collapses to ``left``
        (the primary key on a solo pod)."""
        s = (self.side or "").lower()
        return s if s in ("left", "right") else "left"

    async def update_device(self) -> None:
        """Fast device-state GET (~150ms live; rate-limit-tolerant at a few-second cadence).
        On failure, logs and keeps the last-known-good ``self._device`` (never raises)."""
        if not self._device_id:
            return
        try:
            data = await self._request("GET", f"{CLIENT_API_URL}/devices/{self._device_id}")
        except EightSleepRequestError as exc:
            _LOGGER.warning("device update failed, using stale data: %r", exc)
            return
        result = data.get("result", data) if isinstance(data, dict) else None
        if not isinstance(result, dict):
            return
        self._device = result
        self._device_ts = datetime.now()

    # ------------------------------------------------------------ slow path: physiology
    def _extract_latest_physiology(self, days: list) -> dict:
        if not days:
            return {}
        sessions = (days[-1] or {}).get("sessions") or []
        if not sessions:
            return {}
        session = sessions[-1] or {}
        ts = session.get("timeseries") or {}

        def _last(key: str):
            arr = ts.get(key)
            return arr[-1][1] if arr else None

        def _last_ts(key: str):
            arr = ts.get(key)
            return arr[-1][0] if arr else None

        stages = session.get("stages") or []
        stage = stages[-1].get("stage") if stages else None
        sample_ts = _last_ts("heartRate") or _last_ts("hrv") or _last_ts("respiratoryRate")

        return {
            "heart_rate": _last("heartRate"),
            "hrv": _last("hrv"),
            "respiratory_rate": _last("respiratoryRate"),
            "bed_temp_c": _last("tempBedC"),
            "room_temp_c": _last("tempRoomC"),
            "tnt": _last("tnt"),
            "stage": stage,
            "presence_start": session.get("presenceStart"),
            "presence_end": session.get("presenceEnd"),
            "sample_time": sample_ts,
        }

    async def update_physiology(self, force: bool = False) -> None:
        """Throttled trends GET (~60s cadence by default; batch/processed, lags minutes).
        A no-op if the interval hasn't elapsed unless ``force=True``. On failure, logs and
        keeps the last-known-good ``self._physiology`` (never raises)."""
        now = datetime.now()
        if not force and self._physiology_ts is not None:
            if (now - self._physiology_ts).total_seconds() < self._physiology_interval_s:
                return
        if not self._user_id:
            return

        today = now.date()
        params = {
            "tz": self.timezone,
            "from": (today - timedelta(days=1)).isoformat(),
            "to": (today + timedelta(days=1)).isoformat(),
            # The API rejects requesting BOTH include-main and include-all-sessions
            # (HTTP 400 "Should only request one ...") -- which silently emptied physiology
            # on every poll. Request all sessions (covers naps + the main night session).
            "include-all-sessions": "true",
            "model-version": "v2",
        }
        try:
            data = await self._request("GET", f"{CLIENT_API_URL}/users/{self._user_id}/trends", params=params)
        except EightSleepRequestError as exc:
            _LOGGER.warning("physiology update failed, using stale data: %r", exc)
            return

        days = (data or {}).get("days", [])
        self._physiology = self._extract_latest_physiology(days)
        self._physiology_ts = now

    async def update(self, user: bool = True, device: bool = True) -> None:
        """Drop-in for the pyEight adapter's ``update(user, device)``. ``device`` refreshes
        the fast device GET every call; ``user`` refreshes physiology, but that fetch is
        internally throttled to ``physiology_interval_s`` regardless of call cadence -- so a
        fast telemetry loop calling ``update(device=False)`` frequently is cheap and safe."""
        if device:
            await self.update_device()
        if user:
            await self.update_physiology()
        self._last_update = datetime.now()

    # ------------------------------------------------------------------------- sensing
    def _presence(self, now_utc: datetime) -> bool:
        sample_dt = _parse_iso(self._physiology.get("sample_time"))
        if sample_dt is None or self._physiology.get("heart_rate") is None:
            return False
        return (now_utc - sample_dt).total_seconds() < 600

    def read_frame(self) -> SensorFrame:
        """Build a SensorFrame from the cached device + physiology reads (no network call)."""
        now = datetime.now()
        now_utc = datetime.now(timezone.utc)
        phys = self._physiology or {}
        device = self._device or {}
        side = self._corrected_side()

        device_level = device.get(f"{side}HeatingLevel")
        target_level = device.get(f"{side}TargetHeatingLevel")

        sample_dt = _parse_iso(phys.get("sample_time"))
        # SENSED-physiology freshness. The trends pipeline is session-gated: it is EMPTY for the
        # first ~15-30 min each night (no session yet) and can go stale if a session ends. We must
        # never close the thermal loop on absent/stale sensed data, so we gate every sensed field
        # on the age of its own sample. When not fresh -> null the sensed fields; the controller
        # then falls to safe OPEN-LOOP control (induction/maintenance targets still applied) rather
        # than either (a) closing on a stale/fake signal or (b) hard-freezing. ``data_age_seconds``
        # reports DEVICE freshness (poll recency), which is what should gate a true telemetry-freeze
        # HOLD -- distinct from "no session yet", which is normal and must not freeze induction.
        phys_fresh = sample_dt is not None and (now_utc - sample_dt).total_seconds() <= PHYSIOLOGY_STALE_S
        device_age = (now - self._last_update).total_seconds() if self._last_update else None

        return SensorFrame(
            timestamp=self._physiology_ts or now,
            stage=map_stage(phys.get("stage")) if phys_fresh else map_stage(None),
            stage_confidence=None,
            heart_rate=phys.get("heart_rate") if phys_fresh else None,
            hrv=phys.get("hrv") if phys_fresh else None,
            respiratory_rate=phys.get("respiratory_rate") if phys_fresh else None,
            movement=None,
            presence=self._presence(now_utc),
            # SENSED tempBedC/tempRoomC only when fresh; else None -> open-loop (never circular).
            bed_temp_f=_c_to_f(phys.get("bed_temp_c")) if phys_fresh else None,
            room_temp_f=_c_to_f(phys.get("room_temp_c")) if phys_fresh else None,
            commanded_level=device_level,
            device_level=device_level,
            target_level=target_level,
            data_age_seconds=device_age,
        )

    def device_status(self) -> dict:
        """Rich live device health for the dashboard -- water/priming/online/schedule state
        plus the actual vs. commanded heating level, straight off the fast device GET."""
        d = self._device or {}
        side = self._corrected_side()
        target = d.get(f"{side}TargetHeatingLevel")
        now_active = d.get(f"{side}NowHeating")
        kelvin = d.get(f"{side}Kelvin") or {}
        sensor_info = d.get("sensorInfo") or {}
        return {
            "online": d.get("online"),
            "has_water": d.get("hasWater"),
            "priming": d.get("priming"),
            "needs_priming": d.get("needsPriming"),
            "last_prime": d.get("lastPrime"),
            "last_low_water": d.get("lastLowWater"),
            "device_level": d.get(f"{side}HeatingLevel"),
            "device_target_level": target,
            "now_heating": bool(now_active) and target is not None and target > 0,
            "now_cooling": bool(now_active) and target is not None and target < 0,
            "external_schedule": {
                "activity": kelvin.get("currentActivity"),
                "target_level": kelvin.get("currentTargetLevel"),
                "active": kelvin.get("active"),
            },
            "sensor_connected": sensor_info.get("connected"),
            "firmware_version": d.get("firmwareVersion"),
            "last_heard": d.get("lastHeard"),
            "temp_available": d.get("isTemperatureAvailable"),
            "alarm": self._alarm_cache_readback(),
            "simulated": False,
        }

    def _alarm_cache_readback(self) -> Optional[dict]:
        return None  # populated on demand by set_wake_alarm's resolve step; best-effort only

    def get_current_level(self) -> int:
        return int((self._device or {}).get(f"{self._corrected_side()}HeatingLevel") or 0)

    # ------------------------------------------------------------------------- acting
    @staticmethod
    def _clamp(level: int) -> int:
        return max(-100, min(100, int(level)))

    async def set_heating_level(self, level: int, duration_s: int = 0) -> None:
        """Set the immediate heating level (turns the side on first, matching the app)."""
        level = self._clamp(level)
        await self.turn_on_side()
        url = f"{APP_API_URL}/users/{self._user_id}/temperature"
        await self._request("PUT", url, json_body={"currentLevel": level})
        await self._request("PUT", url, json_body={"timeBased": {"level": level, "durationSeconds": duration_s}})

    async def set_smart_level(self, level: int, sleep_stage: str) -> None:
        """Set the per-stage smart level (bedTimeLevel/initialSleepLevel/finalSleepLevel)."""
        possible = ("bedTimeLevel", "initialSleepLevel", "finalSleepLevel")
        if sleep_stage not in possible:
            raise ValueError(f"Invalid sleep stage {sleep_stage!r}. Should be one of {possible}")
        url = f"{APP_API_URL}/users/{self._user_id}/temperature"
        data = await self._request("GET", url)
        smart = (data or {}).get("smart", {}) or {}
        smart[sleep_stage] = self._clamp(level)
        await self._request("PUT", url, json_body={"smart": smart})

    async def set_autopilot(self, enabled: bool) -> None:
        """Enable/disable Eight Sleep's Autopilot schedule for this side.

        Autopilot's dynamic *bedtime* engine (``currentState.type == 'smart:bedtime'``)
        continuously re-writes ``currentLevel`` to its own escalating targets, which
        overrides our commands within ~45 s. Setting ``smart.enabled = false`` is the
        validated way to take exclusive control **while the pod keeps actuating** --
        unlike away mode, which idles the device (target 0, no heating). Steering the
        static stage levels (``bedTimeLevel`` etc.) does *not* work: the dynamic engine
        ignores them and reasserts its own target (verified live).

        Stage levels are preserved (we GET the current ``smart`` object and only flip
        ``enabled``), so re-enabling restores the user's Autopilot exactly.
        """
        url = f"{APP_API_URL}/users/{self._user_id}/temperature"
        data = await self._request("GET", url)
        smart = dict((data or {}).get("smart", {}) or {})
        smart["enabled"] = bool(enabled)
        await self._request("PUT", url, json_body={"smart": smart})

    async def turn_on_side(self) -> None:
        url = f"{APP_API_URL}/users/{self._user_id}/temperature"
        await self._request("PUT", url, json_body={"currentState": {"type": "smart"}})

    async def turn_off_side(self) -> None:
        url = f"{APP_API_URL}/users/{self._user_id}/temperature"
        await self._request("PUT", url, json_body={"currentState": {"type": "off"}})

    async def is_away(self) -> Optional[bool]:
        """Authoritative away-mode read (``GET /away-mode -> isAway``). Returns
        ``None`` on error so callers can distinguish 'unknown' from 'not away'.
        Away mode idles the pod to target 0 and makes ``currentDevice.side``
        report ``away``, so the daemon uses this to self-heal an away flag it
        never set (e.g. Eight Sleep's own app/Autopilot enabling it)."""
        try:
            data = await self._request("GET", f"{APP_API_URL}/users/{self._user_id}/away-mode")
        except EightSleepRequestError:
            return None
        return bool((data or {}).get("isAway")) if isinstance(data, dict) else None

    async def set_away_mode(self, enabled: bool) -> None:
        action = "start" if enabled else "end"
        # 24h-ago UTC timestamp makes the API apply the transition immediately (matches the
        # behavior verified against pyEight/the live API).
        stamp = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        url = f"{APP_API_URL}/users/{self._user_id}/away-mode"
        await self._request("PUT", url, json_body={"awayPeriod": {action: stamp}})

    async def prime_pod(self) -> None:
        """A 409 means a priming task is already running -- benign, swallowed."""
        url = f"{APP_API_URL}/devices/{self._device_id}/priming/tasks"
        data = {"notifications": {"users": [self._user_id], "meta": "rePriming"}}
        try:
            await self._request("POST", url, json_body=data)
        except EightSleepRequestError as exc:
            if "409" in str(exc):
                return
            raise

    async def increment_level(self, offset: int) -> None:
        url = f"{APP_API_URL}/users/{self._user_id}/temperature"
        current = await self._request("GET", url)
        new_level = self._clamp(int((current or {}).get("currentLevel", 0)) + int(offset))
        await self._request("PUT", url, json_body={"currentLevel": new_level})

    async def _resolve_alarm_id(self) -> Optional[str]:
        """Return an existing alarm id on the device, caching it. The alarm API can only
        MODIFY an existing alarm (verified live: unknown ids 400/404) -- one wake-alarm
        slot must already exist on the device (created once via the app)."""
        if self._alarm_id:
            return self._alarm_id
        try:
            data = await self._request("GET", f"{APP_API_URL}/users/{self._user_id}/alarms")
        except EightSleepRequestError:
            return None
        alarms = (data or {}).get("alarms") or []
        self._alarm_id = alarms[0]["id"] if alarms and isinstance(alarms[0], dict) else None
        return self._alarm_id

    def _full_alarm_payload(
        self, alarm_id: str, *, enabled: bool, time_str: str,
        vibration_enabled: bool, vibration_power: int, vibration_pattern: str,
        thermal_enabled: bool, thermal_level: int,
        audio_enabled: bool, audio_level: int, audio_track: str,
        smart_light_sleep: bool, smart_sleep_cap: bool, smart_sleep_cap_minutes: int,
        weekdays: Optional[dict] = None,
    ) -> dict:
        """The FULL alarm payload -- the convenience per-field PUTs are buggy/clobber each
        other server-side (see docs/THERMAL_WATER_LOOP_DEBUGGING.md); every field must be
        specified on every write."""
        return {
            "id": alarm_id,
            "enabled": enabled,
            "time": time_str,
            "repeat": {
                "enabled": True,
                "weekDays": weekdays or {
                    "monday": True, "tuesday": True, "wednesday": True,
                    "thursday": True, "friday": True, "saturday": True, "sunday": True,
                },
            },
            "vibration": {
                "enabled": vibration_enabled,
                "powerLevel": self._clamp(vibration_power) if vibration_power else 0,
                "pattern": vibration_pattern,
            },
            "thermal": {"enabled": thermal_enabled, "level": self._clamp(thermal_level)},
            "smart": {
                "lightSleepEnabled": smart_light_sleep,
                "sleepCapEnabled": smart_sleep_cap,
                "sleepCapMinutes": smart_sleep_cap_minutes,
            },
            "audio": {"enabled": audio_enabled, "trackId": audio_track, "level": audio_level},
            "snoozing": False,
        }

    async def set_thermal_alarm(self, alarm_id, time, thermal_level: int) -> None:
        """Program a thermal-only alarm (vibration + audio disabled for silence)."""
        target_id = alarm_id or await self._resolve_alarm_id()
        if target_id is None:
            raise RuntimeError("No alarm slot exists on the Pod to drive (create one in the app first).")
        payload = self._full_alarm_payload(
            target_id, enabled=True, time_str=str(time),
            vibration_enabled=False, vibration_power=0, vibration_pattern="INTENSE",
            thermal_enabled=True, thermal_level=thermal_level,
            audio_enabled=False, audio_level=0, audio_track="futuristic",
            smart_light_sleep=True, smart_sleep_cap=True, smart_sleep_cap_minutes=0,
        )
        await self._request("PUT", f"{APP_API_URL}/users/{self._user_id}/alarms/{target_id}", json_body=payload)

    async def set_wake_alarm(self, spec) -> None:
        """Program the heat + gentle-vibration smart wake alarm (audio OFF for silence)."""
        time_val = spec.time
        time_str = time_val.strftime("%H:%M:%S") if isinstance(time_val, datetime) else str(time_val)
        if len(time_str) == 5:
            time_str += ":00"
        alarm_id = await self._resolve_alarm_id()
        if alarm_id is None:
            raise RuntimeError(
                "No alarm slot exists on the Pod to drive. Create one wake alarm in the "
                "Eight Sleep app once -- sleepctl then manages its time/level silently."
            )
        payload = self._full_alarm_payload(
            alarm_id, enabled=True, time_str=time_str,
            vibration_enabled=spec.vibration_power > 0,
            vibration_power=max(0, min(100, int(spec.vibration_power))),
            vibration_pattern="INTENSE",
            thermal_enabled=False, thermal_level=50,
            audio_enabled=False, audio_level=0, audio_track="futuristic",
            smart_light_sleep=True, smart_sleep_cap=True,
            smart_sleep_cap_minutes=spec.window_min,
        )
        await self._request("PUT", f"{APP_API_URL}/users/{self._user_id}/alarms/{alarm_id}", json_body=payload)

    async def fetch_night_summary(self, date: str) -> NightSummary:  # pragma: no cover
        # Best-effort: leave detailed nightly-metric mapping to the storage/nightly layer,
        # which already reconstructs summaries from persisted frames; this stays a stub for
        # interface parity with the pyEight adapter.
        return NightSummary(date=date)

    async def probe(self) -> dict:  # pragma: no cover - requires live device
        """Live per-field capability probe -- mirrors the pyEight adapter's ``probe()``."""
        await self.update()
        frame = self.read_frame()
        status = self.device_status()

        fields = {
            "current_heart_rate": frame.heart_rate,
            "current_hrv": frame.hrv,
            "current_breath_rate": frame.respiratory_rate,
            "current_sleep_stage": frame.stage.value if frame.stage else None,
            "current_bed_temp": frame.bed_temp_f,
            "current_room_temp": frame.room_temp_f,
            "bed_presence": frame.presence,
            "heating_level": frame.device_level,
            "target_heating_level": frame.target_level,
        }
        fields = {k: {"available": v is not None, "value": v} for k, v in fields.items()}

        commands = {
            name: True
            for name in ("set_heating_level", "set_smart_level", "set_thermal_alarm",
                        "set_wake_alarm", "turn_on_side", "turn_off_side")
        }

        warnings = []
        for core in ("current_heart_rate", "current_sleep_stage", "heating_level"):
            if not fields[core]["available"]:
                warnings.append(f"Core field '{core}' is unavailable on this device/session.")
        if status.get("online") is False:
            warnings.append("Device reports offline.")

        return {
            "is_pod_with_cooling": True,
            "has_base": False,
            "side": self.side,
            "fields": fields,
            "commands": commands,
            "warnings": warnings,
            "device_data_keys": sorted((self._device or {}).keys()),
        }

    def capabilities(self) -> dict:
        return {
            "source": "eightsleep_direct",
            "real_time": False,
            "biometric_latency": ("device state (water/level/online) ~150ms real-time; "
                                  "physiology (HR/HRV/RR/stage) batch trends, minutes lag"),
            "fields_expected": [
                "current_heart_rate", "current_hrv", "current_breath_rate",
                "current_sleep_stage", "current_bed_temp", "current_room_temp",
                "bed_presence", "device_level", "target_level",
            ],
            "commands_expected": [
                "set_heating_level", "set_smart_level", "set_thermal_alarm",
                "set_wake_alarm", "turn_on_side", "turn_off_side", "set_away_mode",
                "prime_pod", "increment_level",
            ],
            "note": "Direct client (no pyEight). Validate against the real Pod with "
                    "`sleepctl calibrate`.",
        }


# --------------------------------------------------------------------------------------
# Wiring helper: EIGHTSLEEP_CLIENT=direct|pyeight toggle (default "direct"), with automatic
# fallback to the pyEight-backed client. Self-contained here so callers (run_daemon.py,
# cli.py) need only one import + one call, and never touch eightsleep_cloud.py's internals.
# --------------------------------------------------------------------------------------


def _client_choice_from_env(prefer: Optional[str] = None) -> str:
    return (prefer or os.environ.get("EIGHTSLEEP_CLIENT", "direct")).strip().lower()


async def build_eightsleep_client(
    email: str,
    password: str,
    timezone: str = "UTC",
    side: str = "left",
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    *,
    prefer: Optional[str] = None,
    **direct_kwargs: Any,
):
    """Construct + connect an Eight Sleep client honoring ``EIGHTSLEEP_CLIENT`` (or the
    explicit ``prefer`` override): ``"direct"`` (default) builds this module's
    ``EightSleepDirectClient``; ``"pyeight"`` builds the legacy pyEight-backed
    ``eightsleep_cloud.EightSleepClient``. If the direct client fails to import (e.g. the
    optional ``aiohttp``/``[eightsleep]`` extra isn't installed) or fails to connect (auth
    error, network error, no Pod registered, ...), this transparently falls back to the
    pyEight client and returns THAT instance instead -- callers get back a connected,
    ready-to-use client either way and never need their own try/except.
    """
    choice = _client_choice_from_env(prefer)

    async def _pyeight_client():
        from sleepctl.adapters.eightsleep_cloud import EightSleepClient

        client = EightSleepClient(email, password, timezone, side, client_id, client_secret)
        await client.connect()
        return client

    if choice == "pyeight":
        return await _pyeight_client()

    try:
        client = EightSleepDirectClient(
            email, password, timezone, side, client_id, client_secret, **direct_kwargs
        )
        await client.connect()
        return client
    except Exception as exc:  # ImportError (no aiohttp), auth/connect failure, etc.
        _LOGGER.warning(
            "EightSleepDirectClient failed to import/connect (%r); falling back to the "
            "pyEight-backed client", exc,
        )
        return await _pyeight_client()
