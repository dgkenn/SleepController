"""Real Eight Sleep °F <-> device-level calibration.

The device's heating level (-100..100 in the API; -10..+10 in the app) maps to a water
temperature in **55-110 °F** via a NON-LINEAR lookup table — "the Eight Sleep app does not
use an algebraic formula" (per the pyEight library). This table is vendored verbatim from
pyEight ``constants.RAW_TO_FAHRENHEIT_MAP`` (public device data) so the controller can do
the conversion without depending on pyEight, and ``fahrenheit_to_level`` /
``level_to_fahrenheit`` mirror pyEight's ``util`` nearest-key behaviour exactly.

Key reference points: level 0 ~= 81 °F, -100 = 55 °F, +92 = 110 °F (e.g. 66 °F -> -68).
"""

from __future__ import annotations

import math

MIN_TEMP_F = 55
MAX_TEMP_F = 110

# Vendored from pyEight pyeight/constants.py (RAW_TO_FAHRENHEIT_MAP): heating level -> °F.
RAW_TO_FAHRENHEIT_MAP: dict[int, int] = {
    -100: 55, -99: 56, -97: 57, -95: 58, -94: 59, -92: 60, -90: 61, -86: 62, -81: 63,
    -77: 64, -72: 65, -68: 66, -63: 67, -58: 68, -54: 69, -49: 70, -44: 71, -40: 72,
    -35: 73, -31: 74, -26: 75, -21: 76, -18: 77, -17: 77, -12: 78, -7: 79, -3: 80,
    1: 81, 4: 82, 7: 83, 10: 84, 14: 85, 16: 86, 17: 86, 20: 87, 23: 88, 26: 89,
    29: 90, 32: 91, 35: 92, 38: 93, 41: 94, 44: 95, 48: 96, 51: 97, 54: 98, 57: 99,
    60: 100, 63: 101, 66: 102, 69: 103, 72: 104, 75: 105, 78: 106, 80: 107, 81: 107,
    85: 108, 88: 109, 92: 110, 100: 111,
}


def clamp_fahrenheit(degrees_f: float) -> float:
    """Clamp a target to the device's supported 55-110 °F range."""
    return max(float(MIN_TEMP_F), min(float(MAX_TEMP_F), degrees_f))


def _anchors():
    """The table as (level, °F) sorted by level, with repeated temperatures collapsed so the
    curve is strictly increasing (the vendored table maps both -18 and -17 to 77 °F)."""
    pts = sorted(RAW_TO_FAHRENHEIT_MAP.items())
    out = []
    for lvl, temp in pts:
        if out and float(temp) <= out[-1][1]:
            continue
        out.append((int(lvl), float(temp)))
    return out


_ANCHORS = None


def _curve():
    global _ANCHORS
    if _ANCHORS is None:
        _ANCHORS = _anchors()
    return _ANCHORS


def fahrenheit_to_level(degrees_f: float) -> int:
    """Device level for a target °F, INTERPOLATED between the table's whole-degree anchors.

    The vendored table has one entry per whole degree, and the lookup used to snap to the
    nearest one -- so every target was rounded to a whole degree before it reached the bed,
    although the Pod accepts every integer level between (-58 is 68 F, -54 is 69 F, and -57,
    -56, -55 are the temperatures in between; the Pod's own ramps pass through them).
    Measured 2026-09-22 against that snapping: 68.75-69.5 F all mapped to -54, so the 0.5 F
    wake-recovery warmth never changed the bed, and the thermal trial's 0.0 / 0.5 and 1.0 /
    1.5 F arms were physically the same dose. Whole degrees still map exactly to their
    anchors, so nothing that asked for a whole degree changes.
    """
    degrees_f = clamp_fahrenheit(degrees_f)
    pts = _curve()
    if degrees_f <= pts[0][1]:
        return pts[0][0]
    if degrees_f >= pts[-1][1]:
        return pts[-1][0]
    for (l0, t0), (l1, t1) in zip(pts, pts[1:]):
        if t0 <= degrees_f <= t1:
            frac = (degrees_f - t0) / (t1 - t0)
            return int(math.floor(l0 + frac * (l1 - l0) + 0.5))
    return pts[-1][0]


def level_to_fahrenheit(level: int) -> float:
    """°F for a device level, interpolated the same way (the exact inverse at the anchors)."""
    pts = _curve()
    lvl = float(level)
    if lvl <= pts[0][0]:
        return float(pts[0][1])
    if lvl >= pts[-1][0]:
        return float(pts[-1][1])
    for (l0, t0), (l1, t1) in zip(pts, pts[1:]):
        if l0 <= lvl <= l1:
            frac = (lvl - l0) / (l1 - l0) if l1 != l0 else 0.0
            return round(t0 + frac * (t1 - t0), 2)
    return float(pts[-1][1])
