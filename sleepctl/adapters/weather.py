"""Outdoor ambient temperature via Open-Meteo (free, no API key).

Used to make comfort targets ambient-aware: on a hot night the bed should bias cooler,
on a cold night warmer. Defaults to Boston, MA. Stdlib-only (urllib), cached, and fails
soft (returns the last value or None) so a network blip never disrupts control.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Optional

from sleepctl.adapters.base import WeatherSource

# Boston, MA
BOSTON_LAT = 42.3601
BOSTON_LON = -71.0589

_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&current=temperature_2m&temperature_unit=fahrenheit"
)


class OpenMeteoWeather(WeatherSource):
    def __init__(
        self,
        latitude: float = BOSTON_LAT,
        longitude: float = BOSTON_LON,
        cache_seconds: float = 1800.0,  # weather changes slowly; refetch at most every 30 min
        timeout: float = 10.0,
    ) -> None:
        self.latitude = latitude
        self.longitude = longitude
        self.cache_seconds = cache_seconds
        self.timeout = timeout
        self._cached: Optional[float] = None
        self._fetched_at: float = 0.0

    def _fetch(self) -> Optional[float]:
        """Override point for tests; performs the actual HTTP GET."""
        url = _URL.format(lat=self.latitude, lon=self.longitude)
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:
            data = json.load(resp)
        return float(data["current"]["temperature_2m"])

    def current_temp_f(self) -> Optional[float]:
        now = time.time()
        if self._cached is not None and (now - self._fetched_at) < self.cache_seconds:
            return self._cached
        try:
            value = self._fetch()
        except Exception:
            return self._cached  # fail soft: keep last known (may be None)
        if value is not None:
            self._cached = value
            self._fetched_at = now
        return self._cached
