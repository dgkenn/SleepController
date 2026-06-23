"""Lead-time learning — how far AHEAD of each vulnerable window to start cooling.

Pre-emptive cooling only prevents an awakening if it starts early enough that the bed has
actually cooled by the time the danger arrives. There are two timescales:

  1. the user's physical THERMAL-RESPONSE LAG -- minutes from a cooling command until the
     bed temperature meaningfully drops (learned from their own data), and
  2. the CHARACTER of each window -- a gradual circadian warming needs a long run-up, a
     sudden cycle-boundary arousal needs only a short one.

The lead time for a window = response_lag + a window-specific margin, then scaled up if the
user is still waking too often (outcome feedback). Everything starts from an evidence-based
preset and is adjusted from there, so prevention works on night one and sharpens with data.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

# Window-character margins added on top of the learned response lag (minutes).
_BASE_MARGIN = {
    "cycle_boundary": 2.0,    # brief, sudden -> minimal extra run-up
    "recurring": 6.0,         # personal recurring wake -> moderate
    "circadian": 12.0,        # gradual core-temp nadir/rise -> long run-up
    "warm_threshold": 0.0,    # react as the bed approaches the threshold
}
_DEFAULT_LAG_MIN = 12.0
_LAG_BOUNDS = (5.0, 20.0)
_LEAD_BOUNDS = (3.0, 35.0)


@dataclass
class LeadTimeProfile:
    response_lag_min: float
    leads: dict           # window_type -> minutes
    source: str = "preset"

    def lead_for(self, window_type: Optional[str]) -> float:
        if window_type is None:
            return self.leads.get("cycle_boundary", _DEFAULT_LAG_MIN)
        return self.leads.get(window_type, self.response_lag_min)

    @classmethod
    def from_lag(cls, lag_min: float, bump: float = 1.0, source: str = "preset"
                 ) -> "LeadTimeProfile":
        lag = max(_LAG_BOUNDS[0], min(_LAG_BOUNDS[1], lag_min))
        leads = {}
        for w, margin in _BASE_MARGIN.items():
            v = (lag + margin) * bump
            leads[w] = round(max(_LEAD_BOUNDS[0], min(_LEAD_BOUNDS[1], v)), 1)
        return cls(response_lag_min=round(lag, 1), leads=leads, source=source)

    @classmethod
    def evidence_default(cls) -> "LeadTimeProfile":
        return cls.from_lag(_DEFAULT_LAG_MIN, source="preset")


def _parse_ts(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def learn_response_lag(repo, lookback: int = 6000, drop_f: float = 0.8) -> Optional[float]:
    """Median minutes from a COOLING command to a >= ``drop_f`` bed-temp drop, from the
    user's own ``raw_samples`` (commanded_level decreases -> bed_temp_f falls)."""
    try:
        rows = repo.conn.execute(
            "SELECT ts, bed_temp_f, commanded_level FROM raw_samples "
            "WHERE bed_temp_f IS NOT NULL AND commanded_level IS NOT NULL "
            "ORDER BY id ASC LIMIT ?",
            (lookback,),
        ).fetchall()
    except Exception:
        return None

    samples = []
    for r in rows:
        ts = _parse_ts(r["ts"] if hasattr(r, "keys") else r[0])
        bed = r["bed_temp_f"] if hasattr(r, "keys") else r[1]
        lvl = r["commanded_level"] if hasattr(r, "keys") else r[2]
        if ts is not None and bed is not None and lvl is not None:
            samples.append((ts, float(bed), int(lvl)))

    lags: List[float] = []
    for i in range(1, len(samples)):
        # a cooling command = commanded level dropped vs the previous sample
        if samples[i][2] < samples[i - 1][2] - 2:
            t0, bed0 = samples[i][0], samples[i][1]
            for j in range(i + 1, min(i + 60, len(samples))):
                if samples[j][1] <= bed0 - drop_f:
                    lags.append((samples[j][0] - t0).total_seconds() / 60.0)
                    break
    if len(lags) < 3:
        return None
    return statistics.median(lags)


def build_lead_time_profile(repo, need_min_wake_events: float = 1.0) -> LeadTimeProfile:
    """Construct the lead-time profile: evidence preset adjusted by the learned response lag
    and how often the user is still waking (outcome feedback)."""
    lag = learn_response_lag(repo)
    learned = lag is not None
    lag = lag if learned else _DEFAULT_LAG_MIN

    # Outcome feedback: if recent nights still wake more than the benchmark (>=1), be more
    # anticipatory (scale leads up, bounded). If consistently <=1, leave as-is.
    bump = 1.0
    try:
        nights = repo.recent_nights(14)
        we = [n.wake_events for n in nights if n.wake_events is not None]
        if we:
            avg = sum(we) / len(we)
            if avg > need_min_wake_events:
                bump = min(1.4, 1.0 + 0.15 * (avg - need_min_wake_events))
    except Exception:
        pass

    src = "learned" if learned else "preset"
    if learned and bump > 1.0:
        src = "blended"
    return LeadTimeProfile.from_lag(lag, bump=bump, source=src)
