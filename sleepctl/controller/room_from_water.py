"""Room temperature from the Pod's own water reading, when the Pod has been off long enough.

The hub reports ``currentDeviceLevel`` (``{side}HeatingLevel``): the level its WATER is actually
at, on the -100..100 scale -- a measurement, not an echo of the command (it lags a command by up
to 36 levels and converges gradually; with nothing commanded it drifts on its own, and the user
sees "currently at -6" when they get into a bed that has been off). With the side switched off
(``{side}NowHeating`` False) the water relaxes toward the air around the hub, so after enough
hours it reads the bedroom's temperature. On this account that is the only room reading there
is: the Pod's own room sensor sits behind the subscription.

The hub's reservoir is insulated and relaxes slowly -- one afternoon it was still ~13 F above the
room eight hours after a warm spell, drifting ~1.4 levels/hour -- so the estimate is taken only
after the side has been off for ``settle_h`` hours AND the level has moved no more than
``max_drift_levels`` over the last ``window_min`` with a trend under ``max_slope_per_h`` (the
whole levels the hub reports hide a slow drift from a plain range test: 1.4 levels/hour moves
only 2-3 levels in two hours). An unknown power state never counts as off.
The last good estimate is held (the water stops tracking the room once the Pod runs) for up to
``hold_h`` hours, which covers the night that follows it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import List, Optional, Tuple

from sleepctl.controller.calibration import level_to_fahrenheit

#: Plausible bedroom range. Outside it the reading is not the room (a hub in a hot closet, a
#: glitch) and is not reported.
ROOM_MIN_F, ROOM_MAX_F = 55.0, 88.0


class RoomFromWater:
    def __init__(self, settle_h: float = 6.0, window_min: float = 120.0,
                 max_drift_levels: int = 3, max_slope_per_h: float = 0.5,
                 hold_h: float = 16.0) -> None:
        self.settle = timedelta(hours=settle_h)
        self.window = timedelta(minutes=window_min)
        self.max_drift = int(max_drift_levels)
        self.max_slope = float(max_slope_per_h)
        self.hold = timedelta(hours=hold_h)
        self._off_since: Optional[datetime] = None
        self._recent: List[Tuple[datetime, int]] = []
        self._last: Optional[Tuple[float, datetime]] = None
        self.reason: str = "no reading yet"

    def observe(self, ts: datetime, level: Optional[int], side_on: Optional[bool]) -> None:
        """Feed one reading: the hub's water level and whether this side is switched on."""
        if level is None or side_on is None:
            self._off_since, self._recent = None, []
            self.reason = "power state or water level unknown"
            return
        if side_on:
            self._off_since, self._recent = None, []
            self.reason = "the Pod is running (water is not at room temperature)"
            return
        if self._off_since is None:
            self._off_since = ts
        self._recent.append((ts, int(level)))
        self._recent = [(t, l) for t, l in self._recent if t >= ts - self.window]
        if ts - self._off_since < self.settle:
            hrs = (ts - self._off_since).total_seconds() / 3600.0
            self.reason = f"off for {hrs:.1f} h; needs {self.settle.total_seconds() / 3600:.0f} h to settle"
            return
        if self._recent[0][0] > ts - self.window + timedelta(minutes=10):
            self.reason = "not enough recent readings"
            return
        levels = [l for _t, l in self._recent]
        if max(levels) - min(levels) > self.max_drift:
            self.reason = f"water still drifting ({max(levels) - min(levels)} levels in the window)"
            return
        slope = _slope_per_h(self._recent)
        if abs(slope) > self.max_slope:
            self.reason = f"water still drifting ({slope:+.1f} levels/hour)"
            return
        f = level_to_fahrenheit(int(round(median(levels))))
        if not (ROOM_MIN_F <= f <= ROOM_MAX_F):
            self.reason = f"settled reading {f:.1f} F is outside a plausible room range"
            return
        self._last = (round(f, 1), ts)
        self.reason = "settled"

    def estimate(self, now: datetime) -> Optional[float]:
        """The latest settled room temperature (F), or None when there is none recent enough."""
        if self._last is None or now - self._last[1] > self.hold:
            return None
        return self._last[0]

    def taken_at(self) -> Optional[datetime]:
        return self._last[1] if self._last else None


def _slope_per_h(points: List[Tuple[datetime, int]]) -> float:
    """Least-squares trend of the level, in levels per hour."""
    t0 = points[0][0]
    xs = [(t - t0).total_seconds() / 3600.0 for t, _l in points]
    ys = [float(l) for _t, l in points]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
