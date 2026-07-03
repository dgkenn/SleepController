"""Single source of truth for thermal REACH-TIME (how long the bed takes to actually arrive).

The Pod's level slews slowly and asymmetrically (see ``docs/THERMAL_LATENCY.md``): warming near
neutral is ~4 levels/min, cooling ~1.5 levels/min (down to ~0.8 once already cold). So any
maneuver that wants a temperature by a deadline must START early enough:

    reach_time ~= lag + |Δlevel| / rate

where ``rate`` and ``lag`` are chosen per-direction (heat vs cool). Existing code models only the
LAG (``controller.warm_lead_min = heat_lag + 2``); this module adds the *traverse* term so the
onset cascade and (optionally) the wake runway can size themselves to the bed's real speed.

Pure + no I/O. ``from_repo`` reads the measured self-test calibration and, if a large-enough
continuous ``thermal_samples`` dataset exists, prefers per-direction rates derived from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sleepctl.controller.calibration import fahrenheit_to_level

# Evidence defaults (docs/THERMAL_LATENCY.md — near-neutral rates, conservative lags).
DEFAULT_HEAT_RATE = 4.0   # levels/min warming near neutral
DEFAULT_COOL_RATE = 1.5   # levels/min cooling near neutral
DEFAULT_HEAT_LAG_MIN = 2.0
DEFAULT_COOL_LAG_MIN = 3.0

# Sane clamp bounds so a noisy/degenerate measurement can't produce absurd reach-times.
HEAT_RATE_BOUNDS = (0.5, 10.0)   # levels/min
COOL_RATE_BOUNDS = (0.3, 6.0)    # levels/min
LAG_BOUNDS = (0.0, 15.0)         # minutes

# Level equality epsilon: below this a move is treated as a no-op (0 minutes).
_LEVEL_EPS = 0.5

# thermal_samples derivation thresholds.
_MIN_SAMPLES = 20            # need at least this many rows before trusting the live dataset
_MIN_PAIRS_PER_DIR = 4       # per-direction consecutive pairs required to derive a rate
_MAX_PAIR_GAP_MIN = 10.0     # ignore pairs spanning more than this (stale/discontinuous)


def _clamp(v: Optional[float], lo: float, hi: float) -> Optional[float]:
    if v is None:
        return None
    return max(lo, min(hi, float(v)))


@dataclass
class ThermalLatencyModel:
    """Per-direction rate + lag with a reach-time calculator."""

    heat_rate: float = DEFAULT_HEAT_RATE
    cool_rate: float = DEFAULT_COOL_RATE
    heat_lag_min: float = DEFAULT_HEAT_LAG_MIN
    cool_lag_min: float = DEFAULT_COOL_LAG_MIN

    def minutes_to_reach(self, from_level: float, to_level: float) -> float:
        """Minutes for the bed to traverse ``from_level`` -> ``to_level``.

        ``lag + |Δlevel| / rate``; a higher target level means warming (heat rate/lag), a lower
        one means cooling (cool rate/lag). Returns 0.0 for a no-op (|Δ| below epsilon).
        """
        delta = float(to_level) - float(from_level)
        if abs(delta) < _LEVEL_EPS:
            return 0.0
        if delta > 0:  # to_level warmer -> warming
            rate, lag = self.heat_rate, self.heat_lag_min
        else:
            rate, lag = self.cool_rate, self.cool_lag_min
        rate = rate if rate and rate > 0 else 1.0
        return float(lag) + abs(delta) / rate

    def minutes_to_reach_f(self, from_f: float, to_f: float) -> float:
        """Same as ``minutes_to_reach`` but takes °F endpoints (converted via calibration)."""
        return self.minutes_to_reach(fahrenheit_to_level(from_f), fahrenheit_to_level(to_f))

    def lead_minutes(self, from_level: float, to_level: float, margin_min: float = 2.0) -> float:
        """Reach-time plus a safety margin, floored at 0 — how early to START the maneuver."""
        return max(0.0, self.minutes_to_reach(from_level, to_level) + float(margin_min))

    # -- constructors ---------------------------------------------------------
    @classmethod
    def from_rates(cls, heat_lvl_per_min, cool_lvl_per_min,
                   heat_lag, cool_lag) -> "ThermalLatencyModel":
        """Build from measured rates/lags; None (or non-positive rate) falls back to the default,
        and every value is clamped to sane bounds. Rates are taken by magnitude (a cooling ramp is
        recorded as a negative levels/min, but reach-time uses the speed)."""
        def _rate(v, default, bounds):
            if v is None:
                return default
            v = abs(float(v))
            if v <= 0:
                return default
            return _clamp(v, *bounds)

        def _lag(v, default):
            if v is None:
                return default
            return _clamp(v, *LAG_BOUNDS)

        return cls(
            heat_rate=_rate(heat_lvl_per_min, DEFAULT_HEAT_RATE, HEAT_RATE_BOUNDS),
            cool_rate=_rate(cool_lvl_per_min, DEFAULT_COOL_RATE, COOL_RATE_BOUNDS),
            heat_lag_min=_lag(heat_lag, DEFAULT_HEAT_LAG_MIN),
            cool_lag_min=_lag(cool_lag, DEFAULT_COOL_LAG_MIN),
        )

    @classmethod
    def from_repo(cls, repo) -> "ThermalLatencyModel":
        """Prefer the in-bed self-test calibration; if a large-enough continuous
        ``thermal_samples`` dataset exists, prefer per-direction median rates derived from it.
        Falls back to evidence defaults for anything missing. Fully defensive (never raises)."""
        heat_rate = cool_rate = heat_lag = cool_lag = None
        try:
            cal = repo.get_thermal_calibration() or {}
        except Exception:
            cal = {}
        if cal:
            heat_rate = cal.get("heat_levels_per_min")
            cool_rate = cal.get("cool_levels_per_min")
            heat_lag = cal.get("heat_lag_min")
            cool_lag = cal.get("cool_lag_min")
        # Continuous dataset supersedes the one-shot ramp for the RATE term when we have enough.
        try:
            s_heat, s_cool = cls._rates_from_samples(repo)
            if s_heat is not None:
                heat_rate = s_heat
            if s_cool is not None:
                cool_rate = s_cool
        except Exception:
            pass
        return cls.from_rates(heat_rate, cool_rate, heat_lag, cool_lag)

    @staticmethod
    def _rates_from_samples(repo):
        """Median |levels/min| per direction from consecutive same-direction device_level moves in
        the ``thermal_samples`` table. Returns ``(heat_rate, cool_rate)`` magnitudes, either of
        which may be None when there isn't enough signal. Best-effort; classification is by the
        SIGN of the measured level change (robust to the recorded direction label)."""
        conn = getattr(repo, "conn", None)
        if conn is None:
            return None, None
        try:
            rows = conn.execute(
                "SELECT ts, device_level FROM thermal_samples "
                "WHERE device_level IS NOT NULL ORDER BY ts ASC"
            ).fetchall()
        except Exception:
            return None, None
        if not rows or len(rows) < _MIN_SAMPLES:
            return None, None

        from datetime import datetime as _dt

        def _parse(ts):
            try:
                return _dt.fromisoformat(str(ts))
            except Exception:
                return None

        heat_rates: list[float] = []
        cool_rates: list[float] = []
        prev_t = prev_lvl = None
        for row in rows:
            ts = row["ts"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
            lvl = row["device_level"] if (isinstance(row, dict) or hasattr(row, "keys")) else row[1]
            t = _parse(ts)
            if t is None or lvl is None:
                prev_t, prev_lvl = None, None
                continue
            lvl = float(lvl)
            if prev_t is not None and prev_lvl is not None:
                dt_min = (t - prev_t).total_seconds() / 60.0
                dlvl = lvl - prev_lvl
                if 0.0 < dt_min <= _MAX_PAIR_GAP_MIN and abs(dlvl) >= 1.0:
                    rate = abs(dlvl) / dt_min
                    if dlvl > 0:
                        heat_rates.append(rate)
                    else:
                        cool_rates.append(rate)
            prev_t, prev_lvl = t, lvl

        def _median(vals):
            if len(vals) < _MIN_PAIRS_PER_DIR:
                return None
            s = sorted(vals)
            n = len(s)
            return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0

        return _median(heat_rates), _median(cool_rates)


def minutes_to_reach_f(from_f: float, to_f: float,
                       model: Optional[ThermalLatencyModel] = None) -> float:
    """Convenience: reach-time between two °F endpoints using ``model`` (defaults if None)."""
    m = model or ThermalLatencyModel()
    return m.minutes_to_reach_f(from_f, to_f)
