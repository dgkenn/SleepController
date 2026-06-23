"""Build the ML-ready feature table by joining the dataset layers.

One row per night: the setpoint that produced it (joined by version) + schedule/ambient
context + per-night sensor aggregates as INPUTS, and the sleep outcomes as TARGETS. This
is what ``sleepctl export`` dumps and what the model trains on.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from statistics import fmean
from typing import Optional

from sleepctl.storage.repository import Repository

# Controllable setpoint knobs the optimizer may vary.
SETPOINT_FEATURES = [
    "neutral_f", "deep_bias_f", "rem_warm_offset_f", "wake_ramp_f", "composite_bed_weight",
]
# Context features held fixed when optimizing a setpoint for a given night.
CONTEXT_FEATURES = ["outdoor_temp_f", "sleep_opportunity_min", "is_short_sleep_day",
                    "mean_bed_temp_f", "mean_room_temp_f"]
# Outcomes the model predicts.
OUTCOMES = ["total_sleep_min", "deep_pct", "rem_pct", "wake_events", "waso_min",
            "sleep_efficiency", "avg_hrv", "sleep_onset_latency_min"]


@dataclass
class FeatureRow:
    date: str
    setpoint_version: Optional[int]
    # setpoint knobs
    neutral_f: Optional[float] = None
    deep_bias_f: Optional[float] = None
    rem_warm_offset_f: Optional[float] = None
    wake_ramp_f: Optional[float] = None
    composite_bed_weight: Optional[float] = None
    # context
    outdoor_temp_f: Optional[float] = None
    sleep_opportunity_min: Optional[float] = None
    is_short_sleep_day: Optional[int] = None
    mean_bed_temp_f: Optional[float] = None
    mean_room_temp_f: Optional[float] = None
    # confounder flags (for training filters; not model inputs)
    illness: Optional[int] = None
    travel: Optional[int] = None
    alcohol: Optional[int] = None
    late_night_work: Optional[int] = None
    # subjective labels (optional)
    subjective_quality: Optional[float] = None
    grogginess: Optional[float] = None
    # count of manual temperature overrides on this night (confounder for auto attribution)
    manual_overrides: Optional[int] = None
    # outcomes
    total_sleep_min: Optional[float] = None
    deep_pct: Optional[float] = None
    rem_pct: Optional[float] = None
    wake_events: Optional[float] = None
    waso_min: Optional[float] = None
    sleep_efficiency: Optional[float] = None
    avg_hrv: Optional[float] = None
    sleep_onset_latency_min: Optional[float] = None


def _mean_or_none(values):
    vals = [v for v in values if v is not None]
    return fmean(vals) if vals else None


def _manual_override_counts(repo: Repository) -> dict[str, int]:
    rows = repo.conn.execute(
        "SELECT night_date, COUNT(*) AS c FROM actions WHERE source='manual' GROUP BY night_date"
    ).fetchall()
    return {r["night_date"]: r["c"] for r in rows}


def build_feature_rows(repo: Repository) -> list[FeatureRow]:
    nights = repo.all_nights()
    setpoints = repo.setpoints_by_version()
    manual_counts = _manual_override_counts(repo)
    rows: list[FeatureRow] = []
    for n in nights:
        sp = setpoints.get(n.setpoint_version) if n.setpoint_version is not None else None
        ctx = repo.get_context(n.date)
        samples = repo.samples_for_night(n.date)
        mean_bed = _mean_or_none([s.bed_temp_f for s in samples])
        mean_room = _mean_or_none([s.room_temp_f for s in samples])

        total = n.total_sleep_min or 0.0
        deep_pct = (n.deep_min / total) if (n.deep_min is not None and total) else None
        rem_pct = (n.rem_min / total) if (n.rem_min is not None and total) else None

        rows.append(FeatureRow(
            date=n.date,
            setpoint_version=n.setpoint_version,
            neutral_f=getattr(sp, "neutral_f", None),
            deep_bias_f=getattr(sp, "deep_bias_f", None),
            rem_warm_offset_f=getattr(sp, "rem_warm_offset_f", None),
            wake_ramp_f=getattr(sp, "wake_ramp_f", None),
            composite_bed_weight=getattr(sp, "composite_bed_weight", None),
            outdoor_temp_f=getattr(ctx, "outdoor_temp_f", None),
            sleep_opportunity_min=getattr(ctx, "sleep_opportunity_min", None),
            is_short_sleep_day=(int(ctx.is_short_sleep_day)
                                if ctx and ctx.is_short_sleep_day is not None else None),
            mean_bed_temp_f=mean_bed,
            mean_room_temp_f=mean_room,
            illness=(int(ctx.illness) if ctx and ctx.illness is not None else None),
            travel=(int(ctx.travel) if ctx and ctx.travel is not None else None),
            alcohol=(int(ctx.alcohol) if ctx and ctx.alcohol is not None else None),
            late_night_work=(int(ctx.late_night_work)
                             if ctx and ctx.late_night_work is not None else None),
            subjective_quality=getattr(ctx, "subjective_quality", None),
            grogginess=getattr(ctx, "grogginess", None),
            manual_overrides=manual_counts.get(n.date, 0),
            total_sleep_min=n.total_sleep_min,
            deep_pct=deep_pct,
            rem_pct=rem_pct,
            wake_events=(float(n.wake_events) if n.wake_events is not None else None),
            waso_min=n.waso_min,
            sleep_efficiency=n.sleep_efficiency,
            avg_hrv=n.avg_hrv,
            sleep_onset_latency_min=n.sleep_onset_latency_min,
        ))
    return rows


def export_csv(repo: Repository, path: str) -> int:
    """Write the feature table to CSV (stdlib). Returns the number of rows."""
    rows = build_feature_rows(repo)
    fields = list(FeatureRow.__dataclass_fields__.keys())
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))
    return len(rows)


def export_parquet(repo: Repository, path: str) -> int:
    """Write the feature table to parquet (requires pandas+pyarrow)."""
    try:
        import pandas as pd  # noqa
    except Exception as exc:  # pragma: no cover - optional dep
        raise RuntimeError("parquet export requires the `ml` extra (pandas+pyarrow).") from exc
    rows = build_feature_rows(repo)
    df = pd.DataFrame([asdict(r) for r in rows])
    df.to_parquet(path)
    return len(rows)
