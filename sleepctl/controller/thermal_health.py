"""Thermal-response health check — is the bed ACTUALLY heating/cooling?

Lesson learned live (Pod 2 Pro, cover bypassed into a bucket): the cover-side ``bed_temp_f``
is NOT a usable measure of whether the thermoelectric unit is working. In a hot room it
tracked ambient air — it *rose* while the bed was commanded to MAX COOL. Pure artifact.

The trustworthy signal is the Hub's own water-temp-derived **device level**
(``currentDeviceLevel`` / ``heating_level``, on the -100..100 scale). When the element is
working it ramps toward the commanded ``target_level`` (verified: ~5 levels/min cooling, all
the way to +100 on heat); when it is not (low water, cover disengaged, hardware fault) it
sits flat despite the command. This monitor watches that relationship and reports health,
deliberately ignoring ``bed_temp_f``.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class ThermalHealth:
    state: str            # "ok" | "ramping" | "stalled" | "unknown"
    responding: bool      # bed is tracking the command (or already at setpoint)
    reason: str
    device_level: Optional[int] = None
    target_level: Optional[int] = None
    gap: Optional[int] = None  # target - device

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "responding": self.responding,
            "reason": self.reason,
            "device_level": self.device_level,
            "target_level": self.target_level,
            "gap": self.gap,
        }


class ThermalResponseMonitor:
    """Records (target_level, device_level) over time and judges whether the bed responds."""

    def __init__(self, cfg) -> None:
        t = cfg.tunables
        self.at_target_margin = t.thermal_at_target_margin
        self.window_min = t.thermal_response_window_min
        self.min_progress = t.thermal_min_progress_levels
        # Measured device-level ramp rates from the in-bed self-test (levels/min, positive
        # magnitudes). When set, the stall test expects progress proportional to YOUR bed's
        # real speed instead of a flat floor — sharper true-stall detection, fewer false alarms
        # on a legitimately slow bed.
        self._cool_rate: Optional[float] = None
        self._heat_rate: Optional[float] = None
        # keep enough history to cover the window at a ~1-2 min cadence
        self._samples: deque[tuple[datetime, int, int]] = deque(maxlen=240)

    def set_measured_rates(self, cool_levels_per_min, heat_levels_per_min) -> None:
        """Feed the self-test's measured ramp rates (levels/min). Cool is stored as a positive
        magnitude; either may be None."""
        self._cool_rate = abs(cool_levels_per_min) if cool_levels_per_min else None
        self._heat_rate = abs(heat_levels_per_min) if heat_levels_per_min else None

    def record(self, now: datetime, target_level, device_level) -> None:
        if target_level is None or device_level is None:
            return
        self._samples.append((now, int(target_level), int(device_level)))

    def status(self, now: Optional[datetime] = None) -> ThermalHealth:
        if not self._samples:
            return ThermalHealth("unknown", True, "no device-level samples yet")
        last_ts, target, device = self._samples[-1]
        now = now or last_ts
        gap = target - device

        # Within margin of the setpoint: the bed is where it was told to be — healthy, and
        # there's no active command to verify.
        if abs(gap) <= self.at_target_margin:
            return ThermalHealth("ok", True, "at setpoint", device, target, gap)

        # Actively commanded away from the current level: did the device level make real
        # progress toward the target over the response window?
        cutoff = now - timedelta(minutes=self.window_min)
        window = [s for s in self._samples if s[0] >= cutoff]
        if len(window) < 2 or (window[-1][0] - window[0][0]).total_seconds() < self.window_min * 30:
            return ThermalHealth("unknown", True, "not enough history in window",
                                 device, target, gap)

        first_dev = window[0][2]
        # progress = movement of the device level in the commanded direction
        progress = (device - first_dev) if gap > 0 else (first_dev - device)

        # Expected progress from the MEASURED rate (if calibrated): rate * elapsed window,
        # capped by the remaining gap, and we only demand a fraction of it. This raises the bar
        # for a fast-measured bed (catches a real stall) and lowers it for a slow one (avoids a
        # false alarm), while never dropping below the static floor.
        elapsed_min = (window[-1][0] - window[0][0]).total_seconds() / 60.0
        rate = self._heat_rate if gap > 0 else self._cool_rate
        threshold = self.min_progress
        expected = None
        if rate:
            expected = min(abs(gap), rate * elapsed_min)
            threshold = max(self.min_progress, 0.3 * expected)

        if progress >= threshold:
            verb = "warming" if gap > 0 else "cooling"
            return ThermalHealth("ramping", True,
                                 f"{verb}: device level {first_dev} -> {device} toward {target}",
                                 device, target, gap)
        verb = "warm" if gap > 0 else "cool"
        expect_txt = f"; expected ~{expected:.0f}" if expected is not None else ""
        return ThermalHealth("stalled", False,
                             f"commanded to {verb} but device level not responding "
                             f"({first_dev} -> {device} over {self.window_min} min{expect_txt}) — "
                             f"check water level, cover connection, or hardware",
                             device, target, gap)
