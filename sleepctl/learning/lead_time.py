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


def _measured_cool_lag(repo) -> Optional[float]:
    """The in-bed self-test's measured minutes-to-cool (plateau), clamped to sane lag bounds."""
    try:
        cal = repo.get_thermal_calibration()
    except Exception:
        return None
    if not cal or cal.get("cool_lag_min") is None:
        return None
    return max(_LAG_BOUNDS[0], min(_LAG_BOUNDS[1], float(cal["cool_lag_min"])))


def build_lead_time_profile(repo, need_min_wake_events: float = 1.0,
                            target_prevention: float = 0.75, min_events: int = 4
                            ) -> LeadTimeProfile:
    """Construct the lead-time profile from three signals, in order of authority:

      1. the learned thermal-response lag (sets the floor),
      2. MEASURED pre-cool efficacy per window (the closed loop): if anticipatory cooling at
         the current lead still isn't preventing awakenings, lengthen that window's lead; if
         it reliably prevents, trim it toward the floor,
      3. a global outcome bump if the user is still waking more than the benchmark.
    """
    # Resolve any pending efficacy labels first so the rates are up to date.
    try:
        repo.resolve_precool_events()
    except Exception:
        pass

    lag = learn_response_lag(repo)
    learned = lag is not None
    # The in-bed self-test measures the cool effect-latency directly (controlled, night-one
    # available). Prefer it over the generic preset when overnight inference has no signal yet.
    measured = False
    if not learned:
        cal_lag = _measured_cool_lag(repo)
        if cal_lag is not None:
            lag, measured = cal_lag, True
    lag = lag if (learned or measured) else _DEFAULT_LAG_MIN

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

    profile = LeadTimeProfile.from_lag(
        lag, bump=bump,
        source=("learned" if learned else ("measured" if measured else "preset")))

    # Closed-loop adjustment from measured prevention rates.
    try:
        eff = repo.precool_efficacy()
    except Exception:
        eff = {}
    adjusted = False
    for wtype, lead in list(profile.leads.items()):
        e = eff.get(wtype)
        if not e or e.get("n", 0) < min_events or e.get("rate") is None:
            continue
        rate = e["rate"]
        if rate < target_prevention:
            # not preventing enough -> start cooling earlier (proportional to the shortfall)
            new = lead * (1.0 + 0.5 * (target_prevention - rate))
        elif rate > 0.92:
            # reliably preventing -> trim toward the response-lag floor to avoid over-cooling
            new = max(profile.response_lag_min, lead * 0.95)
        else:
            continue
        profile.leads[wtype] = round(max(_LEAD_BOUNDS[0], min(_LEAD_BOUNDS[1], new)), 1)
        adjusted = True

    if adjusted or (learned and bump > 1.0):
        profile.source = "blended"
    return profile
