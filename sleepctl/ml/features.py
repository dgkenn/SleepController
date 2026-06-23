"""Engineered features computed from rolling history.

These power the Phase-1 phenotype/correlation report and enrich the export. They are
derived per night from the preceding nights + that night's context. Pure stdlib.
"""

from __future__ import annotations

import statistics
from typing import Optional

from sleepctl.storage.repository import Repository


def _median(values):
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def engineer_features(repo: Repository) -> dict[str, dict]:
    """Return {night_date: {engineered_feature: value}} computed causally (past only)."""
    nights = repo.all_nights()
    out: dict[str, dict] = {}
    for i, n in enumerate(nights):
        prev = nights[max(0, i - 7):i]            # previous up-to-7 nights
        prev14 = nights[max(0, i - 14):i]
        ctx = repo.get_context(n.date)
        feats = {}

        # sleep-opportunity ratio + short-sleep flag
        opp = getattr(ctx, "sleep_opportunity_min", None)
        if opp and n.total_sleep_min:
            feats["sleep_opportunity_ratio"] = n.total_sleep_min / opp
        feats["short_sleep_flag"] = int(bool(getattr(ctx, "is_short_sleep_day", False)))

        # bedtime / wake-time consistency (stdev of hour over prior week)
        bt = [p.bedtime.hour + p.bedtime.minute / 60 for p in prev if p.bedtime]
        wt = [p.wake_time.hour + p.wake_time.minute / 60 for p in prev if p.wake_time]
        if len(bt) >= 2:
            feats["bedtime_consistency"] = statistics.pstdev(bt)
        if len(wt) >= 2:
            feats["waketime_consistency"] = statistics.pstdev(wt)

        # previous-night fragmentation
        if prev:
            last = prev[-1]
            feats["prev_fragmentation"] = (last.wake_events or 0) + (last.waso_min or 0) / 30.0

        # rolling HRV deviation vs 14d median; rolling wake-event trend
        base_hrv = _median([p.avg_hrv for p in prev14])
        if n.avg_hrv is not None and base_hrv is not None:
            feats["rolling_hrv_deviation"] = n.avg_hrv - base_hrv
        we = [p.wake_events for p in prev if p.wake_events is not None]
        if we:
            feats["rolling_wake_trend"] = statistics.fmean(we)

        # behavioral flags from context
        feats["late_night_work_flag"] = int(bool(getattr(ctx, "late_night_work", False)))
        feats["caffeine_flag"] = int(bool(getattr(ctx, "caffeine", False)))

        out[n.date] = feats
    return out
