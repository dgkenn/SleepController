"""Personal comfort mapping — an in-bed sweep that learns what YOU actually feel.

The device reasons in a water-temperature °F scale; what you feel under the cover depends on
your body heat, mattress topper, and sheets. This guided sweep holds the bed at a few
temperatures while you rate each ("too cold / a bit cool / just right / a bit warm / too warm"),
and turns those ratings into a personal comfort anchor:

  * ``neutral_f`` — the temperature you rated closest to "just right" (interpolated across the
    zero-crossing of your ratings), which becomes the controller's neutral setpoint.
  * ``cool_edge_f`` / ``warm_edge_f`` — the coldest / warmest temperatures still comfortable to
    you, i.e. your personal comfort band.

Unlike the thermal self-test this is INTERACTIVE and stateful across ticks: the daemon holds the
current step's temperature and waits for your rating, which advances to the next step. It is a
pure state machine here (no device I/O) so it is fully unit-testable; the daemon supplies the
actuation and persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

# Rating scale (what you tap): -2 too cold · -1 a bit cool · 0 just right · +1 a bit warm · +2 too warm
RATING_MIN, RATING_MAX = -2, 2
_ACCEPTABLE = 1          # |rating| <= this is still "comfortable" (defines the band edges)

# Default sweep: cool → warm around a typical neutral, within the hot-sleeper-friendly range.
# Ordered coolest-first so you experience a monotonic warming (easier to rate consistently).
DEFAULT_STEPS_F = [64.0, 68.0, 72.0, 76.0]


@dataclass
class ComfortProfile:
    neutral_f: Optional[float]
    cool_edge_f: Optional[float]
    warm_edge_f: Optional[float]
    ratings: List[dict]
    source: str = "comfort_cal"

    def to_dict(self) -> dict:
        return {"neutral_f": self.neutral_f, "cool_edge_f": self.cool_edge_f,
                "warm_edge_f": self.warm_edge_f, "ratings": self.ratings, "source": self.source}


@dataclass
class ComfortCalibration:
    """Stateful comfort sweep. Steps through ``steps_f`` collecting a rating for each."""

    steps_f: List[float] = field(default_factory=lambda: list(DEFAULT_STEPS_F))
    idx: int = 0
    ratings: List[dict] = field(default_factory=list)  # [{f, rating}] in the order rated
    cancelled: bool = False

    @property
    def done(self) -> bool:
        return self.cancelled or self.idx >= len(self.steps_f)

    def current_target_f(self) -> Optional[float]:
        """The temperature to hold right now (None once the sweep is done)."""
        if self.done:
            return None
        return self.steps_f[self.idx]

    def rate(self, rating: int) -> None:
        """Record the rating for the current step and advance. Out-of-range ratings are clamped."""
        if self.done:
            return
        r = max(RATING_MIN, min(RATING_MAX, int(rating)))
        self.ratings.append({"f": self.steps_f[self.idx], "rating": r})
        self.idx += 1

    def cancel(self) -> None:
        self.cancelled = True

    def progress(self) -> dict:
        return {
            "running": not self.done,
            "cancelled": self.cancelled,
            "step": self.idx + 1 if not self.done else len(self.steps_f),
            "n_steps": len(self.steps_f),
            "current_target_f": self.current_target_f(),
            "ratings": list(self.ratings),
        }

    def finalize(self) -> ComfortProfile:
        """Turn the collected ratings into a comfort profile (call once the sweep is done)."""
        return build_comfort_profile(self.ratings)


def build_comfort_profile(ratings: List[dict]) -> ComfortProfile:
    """Derive neutral + comfort-band edges from [{f, rating}] samples.

    neutral = where the rating curve crosses 0 (too-cold negative → too-warm positive), found by
    linear interpolation between the coolest non-negative and warmest non-positive samples; the
    edges are the coldest/warmest temperatures whose |rating| is still within the acceptable band.
    """
    pts = sorted(((float(r["f"]), int(r["rating"])) for r in ratings if r.get("f") is not None),
                 key=lambda p: p[0])
    if not pts:
        return ComfortProfile(None, None, None, list(ratings))

    # Comfort band: temperatures rated within the acceptable range.
    acceptable = [f for f, r in pts if abs(r) <= _ACCEPTABLE]
    cool_edge = min(acceptable) if acceptable else None
    warm_edge = max(acceptable) if acceptable else None

    # Neutral = zero-crossing of rating vs temperature.
    neutral = _zero_crossing(pts)
    if neutral is None:
        # No sign change tried: fall back to the single best-rated (closest to just-right)
        # temperature, preferring the cooler one on ties (hot sleeper).
        best = min(pts, key=lambda p: (abs(p[1]), p[0]))
        neutral = best[0]
    # Keep neutral inside the comfort band if we have one.
    if cool_edge is not None and warm_edge is not None:
        neutral = max(cool_edge, min(warm_edge, neutral))
    return ComfortProfile(round(neutral, 1), cool_edge, warm_edge, list(ratings))


def _zero_crossing(pts: List[tuple]) -> Optional[float]:
    """Interpolate the temperature where the rating crosses from <=0 to >0 (cool→warm)."""
    for (f0, r0), (f1, r1) in zip(pts, pts[1:]):
        if r0 == 0:
            return f0
        if r1 == 0:
            return f1
        if r0 < 0 < r1:
            # linear interpolation of the temperature at rating 0
            frac = (0 - r0) / (r1 - r0)
            return f0 + frac * (f1 - f0)
    # A sample exactly at 0 anywhere?
    for f, r in pts:
        if r == 0:
            return f
    return None


def steps_around(neutral_f: Optional[float], spread_f: float = 6.0, n: int = 4) -> List[float]:
    """A comfort sweep centered on a prior neutral (e.g. the current setpoint), coolest-first."""
    if neutral_f is None:
        return list(DEFAULT_STEPS_F)
    half = spread_f
    lo = neutral_f - half
    step = (2 * half) / (n - 1) if n > 1 else 0.0
    return [round(lo + i * step, 1) for i in range(n)]
