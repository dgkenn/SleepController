#!/usr/bin/env python3
"""Resting heart rate while awake and at rest, with a 95% confidence interval, from real
logged data — not a model, not a guess: whatever the box has actually measured.

"Awake and at rest" is operationalized as three gates on `raw_samples`, ANDed together:

    presence = 1        -- in bed
    stage    = 'awake'  -- the stager did not call this asleep
    movement <= 0.06     -- the fused movement index bridge.py calls "essentially motionless"
                           (its own PIM->index mapping puts a body lying still at ~0.06;
                           see dashboard/api/app/bridge.py, STILLNESS_PIM_FLOOR)

This is not a clinical resting-HR protocol (5+ min seated, pre-caffeine, morning) -- it is
the honest thing available from overnight telemetry: real HR samples, from real stillness,
while genuinely awake, however many of those actually happened. Small samples get a wide,
clearly-flagged CI rather than a falsely confident one.

    python scripts/resting_hr_report.py --db path/to/sleepctl.db
    python scripts/resting_hr_report.py --db path/to/sleepctl.db --hours 168   # last week only
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta

# bridge.py's `_STILL_INDEX` -- the fused movement index for "essentially motionless".
REST_MOVEMENT_MAX = 0.06

# Two-tailed 95% Student's-t critical values by degrees of freedom. Small n gets an honestly
# wide CI instead of the normal approximation's false precision; df > 120 converges on 1.960
# closely enough that a lookup table stops being worth extending.
_T95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306,
    9: 2.262, 10: 2.228, 15: 2.131, 20: 2.086, 25: 2.060, 30: 2.042, 40: 2.021,
    60: 2.000, 120: 1.980,
}


def t_crit(df: int) -> float:
    """95% two-tailed t critical value for ``df`` degrees of freedom (linear interpolation
    between table entries; 1.960 -- the normal limit -- beyond df=120)."""
    if df <= 0:
        raise ValueError("degrees of freedom must be positive")
    if df in _T95:
        return _T95[df]
    keys = sorted(_T95)
    if df > keys[-1]:
        return 1.960
    lo = max(k for k in keys if k <= df)
    hi = min(k for k in keys if k >= df)
    frac = (df - lo) / (hi - lo)
    return _T95[lo] + frac * (_T95[hi] - _T95[lo])


def fetch_resting_hr_rows(conn: sqlite3.Connection, cutoff_iso: str) -> list:
    """The three-gate query, isolated so it can be exercised without going through main()."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT ts, heart_rate FROM raw_samples "
        "WHERE presence = 1 AND stage = 'awake' AND movement IS NOT NULL "
        "AND movement <= ? AND heart_rate IS NOT NULL AND ts >= ? ORDER BY ts",
        (REST_MOVEMENT_MAX, cutoff_iso),
    ).fetchall()


def summarize(rows: list) -> dict:
    """Pure computation over already-fetched rows: mean, 95% CI, coverage. No I/O."""
    hrs = [r["heart_rate"] for r in rows]
    n = len(hrs)
    mean = statistics.mean(hrs)
    sd = statistics.stdev(hrs) if n > 1 else 0.0
    sem = sd / (n ** 0.5) if n > 1 else 0.0
    if n > 1:
        tcrit = t_crit(n - 1)
        ci_lo, ci_hi = mean - tcrit * sem, mean + tcrit * sem
    else:
        ci_lo = ci_hi = mean

    ts = [datetime.fromisoformat(r["ts"]) for r in rows]
    span_min = (ts[-1] - ts[0]).total_seconds() / 60.0 if n > 1 else 0.0
    if n > 1:
        gaps = [(ts[i + 1] - ts[i]).total_seconds() for i in range(n - 1)]
        median_gap_s = statistics.median(gaps)
        # Samples are irregular (control-loop cadence, not a fixed sample rate), so approximate
        # actual measurement time as n * typical gap rather than assuming continuous coverage
        # across the whole span -- capped at the span itself for a very sparse, bursty series.
        covered_min = min(span_min, n * median_gap_s / 60.0)
    else:
        median_gap_s = 0.0
        covered_min = 0.0

    return {
        "n": n, "mean": mean, "sd": sd, "ci_lo": ci_lo, "ci_hi": ci_hi,
        "first_ts": ts[0] if ts else None, "last_ts": ts[-1] if ts else None,
        "span_min": span_min, "covered_min": covered_min, "median_gap_s": median_gap_s,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="path to sleepctl.db")
    ap.add_argument("--hours", type=float, default=24.0 * 90,
                    help="trailing lookback window in hours (default 90 days)")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    cutoff = (datetime.now() - timedelta(hours=args.hours)).isoformat()
    rows = fetch_resting_hr_rows(conn, cutoff)
    conn.close()

    if not rows:
        print(f"No qualifying samples (in bed, stage=awake, movement <= {REST_MOVEMENT_MAX}, "
              f"real HR) in the last {args.hours:.0f}h.")
        print("Either the Verity hasn't been worn long enough yet, or the stager hasn't "
              "reported an awake-at-rest window in that time.")
        return 1

    s = summarize(rows)
    print(f"Resting HR (awake, in bed, movement <= {REST_MOVEMENT_MAX}): "
          f"{s['mean']:.1f} bpm, 95% CI [{s['ci_lo']:.1f}, {s['ci_hi']:.1f}]")
    print(f"n = {s['n']} samples, sd = {s['sd']:.1f} bpm")
    print(f"spans {s['first_ts']} -> {s['last_ts']}  ({s['span_min']:.0f} min elapsed)")
    print(f"~{s['covered_min']:.0f} min of actual measurement inside that span "
          f"(median inter-sample gap {s['median_gap_s']:.0f}s)")
    if s["n"] < 10:
        print("CAUTION: n < 10 -- this CI is wide and provisional; do not treat it as settled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
