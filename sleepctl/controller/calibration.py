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


def fahrenheit_to_level(degrees_f: float) -> int:
    """Nearest device level for a target °F (mirrors pyEight temp_to_heating_level)."""
    degrees_f = clamp_fahrenheit(degrees_f)
    best_level, best_diff = 0, 1e9
    for level, temp in RAW_TO_FAHRENHEIT_MAP.items():
        diff = abs(temp - degrees_f)
        if diff < best_diff:
            best_diff, best_level = diff, level
    return best_level


def level_to_fahrenheit(level: int) -> float:
    """°F for a device level (nearest key; mirrors pyEight heating_level_to_temp)."""
    best_temp, best_diff = RAW_TO_FAHRENHEIT_MAP[0] if 0 in RAW_TO_FAHRENHEIT_MAP else 81, 1e9
    for lvl, temp in RAW_TO_FAHRENHEIT_MAP.items():
        diff = abs(lvl - level)
        if diff < best_diff:
            best_diff, best_temp = diff, temp
    return float(best_temp)
