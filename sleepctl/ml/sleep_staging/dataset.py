"""Parse the PhysioNet sleep-accel text files into a supervised staging dataset.

For each subject we read HR, steps (activity counts), and PSG sleep labels, compute a
per-recording sleep-HR baseline (20th percentile of that recording's HR), and for every
scored 30 s epoch build a trailing-window feature row (shared with inference via
:mod:`features`) plus two targets:

    y_wake   : binary  0 = wake, 1 = sleep
    y_stage4 : 4-class 0 = wake, 1 = light (N1+N2), 2 = deep (N3), 3 = rem

Epochs that are unscored (label -1) or have fewer than 3 HR samples in the trailing
window are dropped. numpy is used here (training-time only); the feature computation
itself stays pure-stdlib.

PSG stage codes: -1 unscored, 0 wake, 1 N1, 2 N2, 3 N3, 5 REM.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .features import (
    FEATURE_NAMES_HR,
    FEATURE_NAMES_HRMOTION,
    compute_features,
    feature_vector,
)

EPOCH_S = 30.0
MIN_HR_SAMPLES = 3
BASELINE_PCTL = 20.0

Sample = Tuple[float, float]

SUBJECT_IDS = [
    "1066528", "1360686", "1449548", "1455390", "1818471", "2598705",
    "2638030", "3509524", "3997827", "4018081", "4314139", "4426783",
    "46343", "5132496", "5383425", "5498603", "5797046", "6220552",
    "759667", "7749105", "781756", "8000685", "8173033", "8258170",
    "844359", "8530312", "8686948", "8692923", "9106476", "9618981",
    "9961348",
]

DEFAULT_DATA_DIR = (
    "/tmp/claude-0/-home-user-SleepController/"
    "e6ce5980-b2d3-50b8-a237-9df8d193f1a3/scratchpad/sleep_accel"
)


def _percentile(sorted_vals: List[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _parse_csv_pairs(path: str) -> List[Sample]:
    out: List[Sample] = []
    if not os.path.exists(path):
        return out
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            parts = line.replace(",", " ").split()
            if len(parts) < 2:
                continue
            try:
                out.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
    out.sort(key=lambda s: s[0])
    return out


def _parse_labels(path: str) -> List[Tuple[float, int]]:
    out: List[Tuple[float, int]] = []
    if not os.path.exists(path):
        return out
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                out.append((float(parts[0]), int(float(parts[1]))))
            except ValueError:
                continue
    out.sort(key=lambda s: s[0])
    return out


# stage code -> 4-class label
def _stage4(code: int) -> Optional[int]:
    if code == 0:
        return 0  # wake
    if code in (1, 2):
        return 1  # light
    if code == 3:
        return 2  # deep
    if code == 5:
        return 3  # rem
    return None  # -1 unscored / unknown


@dataclass
class StagingDataset:
    """Rows aligned across all three parallel outputs."""

    rows: List[Dict[str, float]] = field(default_factory=list)  # full feature dicts
    y_wake: List[int] = field(default_factory=list)
    y_stage4: List[int] = field(default_factory=list)
    groups: List[str] = field(default_factory=list)

    def matrix(self, feature_names: List[str]) -> List[List[float]]:
        return [feature_vector(r, feature_names) for r in self.rows]

    def __len__(self) -> int:
        return len(self.rows)


def build_subject_rows(
    subject_id: str,
    data_dir: str = DEFAULT_DATA_DIR,
    window_s: float = 600.0,
) -> StagingDataset:
    hr = _parse_csv_pairs(os.path.join(data_dir, f"{subject_id}_heartrate.txt"))
    steps = _parse_csv_pairs(os.path.join(data_dir, f"{subject_id}_steps.txt"))
    labels = _parse_labels(os.path.join(data_dir, f"{subject_id}_labeled_sleep.txt"))

    ds = StagingDataset()
    if not hr or not labels:
        return ds

    # recording sleep-HR baseline: 20th percentile of HR in this recording
    hr_vals = sorted(v for _, v in hr)
    baseline = _percentile(hr_vals, BASELINE_PCTL)

    # sleep onset: first scored sleep epoch
    onset_t: Optional[float] = None
    for t, code in labels:
        if _stage4(code) in (1, 2, 3):
            onset_t = t
            break

    total_minutes = (labels[-1][0] / 60.0) if labels else None

    for t, code in labels:
        s4 = _stage4(code)
        if s4 is None:
            continue  # unscored / unknown
        epoch_end = t + EPOCH_S
        feats = compute_features(
            hr,
            steps,
            epoch_end,
            hr_baseline=baseline,
            minutes_since_start=t / 60.0,
            minutes_since_onset=((t - onset_t) / 60.0) if onset_t is not None else 0.0,
            total_minutes=total_minutes,
            window_s=window_s,
            include_activity=True,
        )
        if feats["hr_n_samples"] < MIN_HR_SAMPLES:
            continue
        ds.rows.append(feats)
        ds.y_wake.append(0 if s4 == 0 else 1)
        ds.y_stage4.append(s4)
        ds.groups.append(subject_id)
    return ds


def build_dataset(
    data_dir: str = DEFAULT_DATA_DIR,
    subject_ids: Optional[List[str]] = None,
    window_s: float = 600.0,
) -> StagingDataset:
    subject_ids = subject_ids or SUBJECT_IDS
    combined = StagingDataset()
    for sid in subject_ids:
        sub = build_subject_rows(sid, data_dir=data_dir, window_s=window_s)
        combined.rows.extend(sub.rows)
        combined.y_wake.extend(sub.y_wake)
        combined.y_stage4.extend(sub.y_stage4)
        combined.groups.extend(sub.groups)
    return combined


__all__ = [
    "StagingDataset",
    "build_dataset",
    "build_subject_rows",
    "SUBJECT_IDS",
    "DEFAULT_DATA_DIR",
    "FEATURE_NAMES_HR",
    "FEATURE_NAMES_HRMOTION",
]
