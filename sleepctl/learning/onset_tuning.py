"""Learn the ONSET maneuver per-person — what induction warmth puts YOU to sleep fastest.

This closes the last open-loop phase. The induction cascade warms the bed a little first
(cutaneous warming → distal vasodilation → faster onset; Raymann/Van Someren), then cools as you
drift off. *How much* warmth is optimal is personal — for a hot sleeper a big warm nudge may
backfire, for others it's what tips them over. Rather than hold a static config value, this learns
the warm-nudge magnitude that minimizes YOUR measured sleep-onset latency, and ACTIVELY EXPLORES
(jitters the nudge a little each night) so the latency-vs-warmth curve keeps getting sampled.

  • learn_onset(records, mode=...): bins the recorded warm nudges by the onset latency they
    produced and picks the fastest-onset setting, shrunk toward the default by sample size and
    clamped. Segments by night mode when there's enough data (a short night may want a different,
    faster onset than a full night), else pools across modes.
  • next_onset_warm_f(best, night): adds a small rotating ±jitter so the curve gets sampled
    (deterministic by night — no randomness in the control loop).

Constraint-aware, conservative + bounded (0–2.5 °F above neutral); needs enough nights before it
moves off the default.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import List, Optional

ONSET_WARM_BOUNDS = (0.0, 2.5)          # °F above neutral; 0 = no warm nudge
_EXPLORE_PATTERN = (0.0, 0.5, -0.5)     # rotated by night so the curve is sampled around best


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass
class OnsetManeuver:
    onset_warm_f: float          # learned best warm-nudge magnitude (°F above neutral)
    direction: str               # warmer | cooler | neutral (vs the default)
    n: int
    mode: Optional[str]          # which night mode this was learned for (None = pooled)
    is_personalized: bool
    rationale: str

    def to_dict(self) -> dict:
        return {"onset_warm_f": round(self.onset_warm_f, 2), "direction": self.direction,
                "n": self.n, "mode": self.mode, "is_personalized": self.is_personalized,
                "rationale": self.rationale}


def _filter_mode(records: List[dict], mode: Optional[str], min_nights: int) -> List[dict]:
    """Segment by night mode when that mode has enough data; otherwise pool across all modes so a
    rarely-seen mode still gets a sensible (shared) estimate."""
    if mode is None:
        return records
    seg = [r for r in records if (r.get("night_type") or "normal") == mode]
    return seg if len(seg) >= min_nights else records


def learn_onset(records: List[dict], base_f: float = 1.0, min_nights: int = 8,
                min_per_bucket: int = 2, mode: Optional[str] = None,
                bounds=ONSET_WARM_BOUNDS) -> OnsetManeuver:
    """records: [{'onset_warm_f': float, 'onset_latency_min': float, 'night_type': str}, ...].
    Returns the warm-nudge magnitude that gave you the fastest onset (shrunk toward the default,
    clamped). When ``mode`` is given and has enough nights, learns for that mode specifically."""
    pool = [r for r in records
            if r.get("onset_latency_min") is not None and r.get("onset_warm_f") is not None]
    usable = _filter_mode(pool, mode, min_nights)
    n = len(usable)
    if n < min_nights:
        return OnsetManeuver(base_f, "neutral", n, mode, False,
                             f"learning — {n}/{min_nights} nights with a measured onset")

    buckets = defaultdict(list)
    for r in usable:
        buckets[round(float(r["onset_warm_f"]) * 2) / 2].append(float(r["onset_latency_min"]))
    cand = [(f, sum(v) / len(v)) for f, v in buckets.items() if len(v) >= min_per_bucket]
    if not cand:
        return OnsetManeuver(base_f, "neutral", n, mode, False,
                             "not enough repeated warm nudges yet to compare")

    best_raw = min(cand, key=lambda c: c[1])[0]            # fastest-onset warm nudge tried
    shrink = min(1.0, (n - min_nights + 1) / 8.0)
    best = round(_clamp(base_f + (best_raw - base_f) * shrink, *bounds), 2)
    is_pers = abs(best - base_f) >= 0.25
    direction = "warmer" if best > base_f else "cooler" if best < base_f else "neutral"
    fastest = min(cand, key=lambda c: c[1])[1]
    rationale = (f"a {direction} onset nudge (~+{best:.1f} °F) gets you to sleep fastest "
                 f"(~{fastest:.0f} min)" if is_pers else "your default onset warmth already fits")
    return OnsetManeuver(best, direction, n, mode, is_pers, rationale)


def next_onset_warm_f(best_f: float, night_index: int, bounds=ONSET_WARM_BOUNDS,
                      jitter: float = 0.5) -> float:
    """Tonight's onset warm nudge: the learned best plus a small rotating exploration jitter so the
    latency-vs-warmth curve keeps getting sampled. Deterministic by night index."""
    delta = _EXPLORE_PATTERN[night_index % len(_EXPLORE_PATTERN)]
    return round(_clamp(best_f + jitter * delta, *bounds), 2)


def onset_records(repo, nights: int = 40) -> List[dict]:
    """Join the per-night wake-log's recorded onset warm nudge with the measured sleep-onset
    latency from the night summary (and the night mode, for constraint-aware learning)."""
    try:
        from app import bridge
        logs = bridge.read_wake_logs(repo.conn, nights)
    except Exception:
        return []
    lat_by_date = {n.date: getattr(n, "sleep_onset_latency_min", None)
                   for n in repo.recent_nights(nights)}
    out: List[dict] = []
    for row in logs:
        warm = row.get("onset_warm_f")
        lat = lat_by_date.get(row["date"])
        if warm is None or lat is None:
            continue
        out.append({"onset_warm_f": warm, "onset_latency_min": lat,
                    "night_type": row.get("night_type") or "normal"})
    return out
