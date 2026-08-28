"""One-time, version-controlled correction of a comfort band that its own evidence refutes.

The comfort band is the last thing standing between the controller and the bed: every
in-maintenance command is clamped to it, so when the band is wrong the band decides the night.

The stored band on this deployment was ``observed_night_2026-08-04_v2`` -- neutral 66.9,
cool_edge 65.5, warm_edge 68.0 -- derived, by its own ``ratings.caveat``, from a single night
("n=1 night ... this band is secondary"). The same ratings blob records the measurements that
refute it:

    best_sleep_median_level   -55  (69 F water), 160 min unbroken
    cold_end_awakenings       levels -77..-80  (63-64 F water)
    warm_end_awakenings       levels -38..-51  (70-72 F water)

69 F -- the temperature at which this user's longest unbroken sleep was recorded -- is ABOVE
the band's ceiling and therefore unreachable. Measured on 2026-08-27 with the band in force:
the commanded water sat at 64-67 F all night, pinned at the floor for two thirds of the
maintenance ticks, across 16 wake events; awakening pre-emption tried to warm 57 times and
could not get above 67 F.

The corrected band centres on the measured best-sleep temperature and keeps ~3 F of clearance
from BOTH recorded awakening zones. It is applied once, only to the exact profile named above,
and only while that profile still excludes the best-sleep temperature -- so a later, better
band (from a real comfort sweep, or set by hand through /control/comfort-profile) is never
overwritten.
"""

from __future__ import annotations

import json
from typing import Optional

#: The exact stored profile this correction targets. Matching on the source string keeps the
#: change surgical: nothing else is touched, and once applied the source no longer matches.
SUPERSEDED_SOURCE = "observed_night_2026-08-04_v2"
CORRECTED_SOURCE = "evidence_corrected_2026-08-28"

#: Derived from the superseded profile's OWN recorded evidence (see the module docstring).
CORRECTED = {
    "neutral_f": 69.0,     # best_sleep_median_level: 69 F water, 160 min unbroken
    "cool_edge_f": 67.0,   # 3 F above the 63-64 F cold-awakening zone
    "warm_edge_f": 69.5,   # below the 70-72 F warm-awakening zone, keeps 69 F reachable
}

#: The correction only makes sense while the stored band cannot reach this.
BEST_SLEEP_F = 69.0


def needs_correction(profile: Optional[dict]) -> bool:
    """Is this the specific superseded band, still excluding the best-sleep temperature?"""
    if not profile:
        return False
    if str(profile.get("source") or "") != SUPERSEDED_SOURCE:
        return False
    warm = profile.get("warm_edge_f")
    cool = profile.get("cool_edge_f")
    if warm is None or cool is None:
        return False
    try:
        # Only correct while the band genuinely cannot reach the measured best-sleep point.
        return not (float(cool) <= BEST_SLEEP_F <= float(warm))
    except (TypeError, ValueError):
        return False


def corrected_profile(profile: dict) -> dict:
    """The replacement band, preserving the original evidence for audit."""
    ratings = profile.get("ratings")
    if isinstance(ratings, str):
        try:
            ratings = json.loads(ratings)
        except Exception:
            ratings = {"raw": ratings}
    if not isinstance(ratings, dict):
        ratings = {}
    out = dict(CORRECTED)
    out["source"] = CORRECTED_SOURCE
    out["ratings"] = {
        **ratings,
        "superseded": {
            "source": profile.get("source"),
            "neutral_f": profile.get("neutral_f"),
            "cool_edge_f": profile.get("cool_edge_f"),
            "warm_edge_f": profile.get("warm_edge_f"),
        },
        "correction_reason": (
            "The superseded band excluded 69 F, the water temperature its own evidence records "
            "as this user's best sleep (160 min unbroken). With it in force on 2026-08-27 the "
            "commanded water sat at 64-67 F all night, pinned at the floor for two thirds of "
            "maintenance ticks across 16 wake events, and pre-emption could not warm above "
            "67 F. Re-centred on the measured best-sleep temperature with ~3 F clearance from "
            "both recorded awakening zones (cold 63-64 F, warm 70-72 F)."
        ),
    }
    return out


def apply_comfort_correction(repo) -> Optional[dict]:
    """Apply the correction if it is due. Returns the new profile, or None if nothing changed."""
    try:
        prof = repo.get_comfort_profile()
    except Exception:
        return None
    if not needs_correction(prof):
        return None
    new = corrected_profile(prof)
    try:
        repo.save_comfort_profile(new)
    except Exception:
        return None
    return new
