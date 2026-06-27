"""Outdoor ambient temperature via Open-Meteo (free, no API key).

Used to make comfort targets ambient-aware: on a hot night the bed should bias cooler,
on a cold night warmer. Defaults to Boston, MA. Stdlib-only (urllib), cached, and fails
soft (returns the last value or None) so a network blip never disrupts control.
"""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime
from typing import List, Optional, Tuple

from sleepctl.adapters.base import WeatherSource

# Boston, MA
BOSTON_LAT = 42.3601
BOSTON_LON = -71.0589

_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&current=temperature_2m&temperature_unit=fahrenheit"
)
_HOURLY_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&hourly=temperature_2m&temperature_unit=fahrenheit&forecast_days=2"
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

    # -- overnight forecast (for environmental pre-compensation) ------------------
    def _fetch_hourly(self) -> List[Tuple[str, float]]:
        """Override point for tests; returns [(iso_hour, temp_f), ...]."""
        url = _HOURLY_URL.format(lat=self.latitude, lon=self.longitude)
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:
            data = json.load(resp)
        times = data["hourly"]["time"]
        temps = data["hourly"]["temperature_2m"]
        return [(t, float(v)) for t, v in zip(times, temps) if v is not None]

    def overnight_forecast(self, from_dt: Optional[datetime] = None,
                           hours: int = 11) -> Optional[dict]:
        """Summarize the outdoor temperature trajectory across tonight's sleep window.

        Returns {start_f, end_f, low_f, high_f, trend, hours: [{hour, temp_f}]} or None.
        ``trend`` is warming / cooling / stable based on end-vs-start.
        """
        ref = from_dt or datetime.now()
        try:
            hourly = self._fetch_hourly()
        except Exception:
            return None
        series: List[Tuple[datetime, float]] = []
        for t, v in hourly:
            try:
                dt = datetime.fromisoformat(t)
            except ValueError:
                continue
            series.append((dt, v))
        future = [(dt, v) for dt, v in series if dt >= ref.replace(minute=0, second=0, microsecond=0)]
        window = future[:hours] if future else []
        if len(window) < 2:
            return None
        temps = [v for _, v in window]
        start_f, end_f = temps[0], temps[-1]
        delta = end_f - start_f
        trend = "warming" if delta >= 2 else ("cooling" if delta <= -2 else "stable")
        return {
            "start_f": round(start_f, 1),
            "end_f": round(end_f, 1),
            "low_f": round(min(temps), 1),
            "high_f": round(max(temps), 1),
            "trend": trend,
            "hours": [{"hour": dt.strftime("%H:%M"), "temp_f": round(v, 1)}
                      for dt, v in window],
        }
