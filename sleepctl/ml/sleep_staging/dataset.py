"""Parse the PhysioNet sleep-accel text files into a supervised staging dataset (v2).

Inputs per subject (raw data lives in the scratchpad, never in the repo):

    <ID>_heartrate.txt       "t_seconds,bpm"
    <ID>_labeled_sleep.txt   "t_seconds stage"   (-1 unscored, 0 wake, 1 N1, 2 N2, 3 N3, 5 REM)
    activity/<ID>_activity.txt
                             "epoch_start_s,pim,zcm,mad,std,pmax,n"  -- REAL actigraphy counts
                             reduced from the raw triaxial accelerometer. This replaces v1's
                             ``steps`` signal, which was ~96% zeros overnight.
    <ID>_ibi.txt             "t_seconds,ibi_ms"  -- OPTIONAL beat-to-beat intervals (the
                             DREAMT reduction writes it; sleep-accel has none). When present
                             every row also carries the HRV block (``features.FEATURE_NAMES_HRV``)
                             and its causal per-night normalisation; when absent the rows are
                             exactly the v2 ones.

For every scored 30 s epoch we build a trailing multi-scale feature row (shared verbatim
with inference via :mod:`features`) plus two targets:

    y_wake   : binary  0 = wake, 1 = sleep
    y_stage4 : 4-class 0 = wake, 1 = light (N1+N2), 2 = deep (N3), 3 = rem

Per-recording normalization statistics are computed **causally** — from the night *so far*,
never the whole night — because that is all the live controller's buffer can ever contain.
numpy/sklearn are training-time only; the feature computation itself stays pure-stdlib.
"""

from __future__ import annotations

import bisect
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .features import (
    ACT_FEATURES_ABSOLUTE,
    ACT_FEATURES_SCALEFREE,
    FEATURE_NAMES_HR,
    FEATURE_NAMES_HR_HRV,
    FEATURE_NAMES_HRMOTION,
    FEATURE_NAMES_HRMOTION_ABS,
    FEATURE_NAMES_HRV,
    FEATURE_NAMES_HRV_MOTION,
    HRV_NORM_KEYS,
    MAX_LOOKBACK_S,
    compute_features,
    feature_vector,
    hrv_bucket_summaries,
    stats_from_sorted,
)

EPOCH_S = 30.0
MIN_HR_SAMPLES = 3

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

STAGE4_LABELS = ["wake", "light", "deep", "rem"]


# --------------------------------------------------------------------------- parsing
def _parse_pairs(path: str) -> List[Sample]:
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


def parse_activity(path: str) -> List[Tuple[float, ...]]:
    """Read ``epoch_start_s,pim,zcm,mad,std,pmax,n`` into (t, pim, zcm, mad, std, pmax)."""
    out: List[Tuple[float, ...]] = []
    if not os.path.exists(path):
        return out
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace(",", " ").split()
            if len(parts) < 2:
                continue
            try:
                vals = [float(p) for p in parts[:6]]
            except ValueError:
                continue
            if len(vals) < 6:
                vals += [0.0] * (6 - len(vals))
            out.append(tuple(vals))
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


def _stage4(code: int) -> Optional[int]:
    if code == 0:
        return 0  # wake
    if code in (1, 2):
        return 1  # light (N1 + N2)
    if code == 3:
        return 2  # deep (N3)
    if code == 5:
        return 3  # rem
    return None   # -1 unscored / unknown


def _decimate(samples: List[Sample], min_gap_s: float) -> List[Sample]:
    """Thin a sample series to at most one sample per ``min_gap_s`` seconds."""
    if min_gap_s <= 0 or not samples:
        return samples
    out = [samples[0]]
    for s in samples[1:]:
        if s[0] - out[-1][0] >= min_gap_s:
            out.append(s)
    return out


def discover_subjects(data_dir: str) -> List[str]:
    """Subject IDs present in ``data_dir`` (every ``<ID>_labeled_sleep.txt``), sorted.

    The sleep-accel corpus has a fixed ID list (:data:`SUBJECT_IDS`); a DREAMT reduction
    has whatever participants were downloaded, so the loader discovers them.
    """
    out: List[str] = []
    try:
        names = os.listdir(data_dir)
    except OSError:
        return out
    for n in names:
        if n.endswith("_labeled_sleep.txt"):
            out.append(n[: -len("_labeled_sleep.txt")])
    return sorted(out)


def subjects_with_ibi(data_dir: str, subject_ids: Optional[Sequence[str]] = None) -> List[str]:
    """Subject IDs whose ``<ID>_ibi.txt`` exists and is non-empty."""
    out = []
    for sid in (subject_ids or discover_subjects(data_dir)):
        p = os.path.join(data_dir, f"{sid}_ibi.txt")
        if os.path.exists(p) and os.path.getsize(p) > 100:
            out.append(sid)
    return out


def subjects_with_activity(data_dir: str = DEFAULT_DATA_DIR,
                           subject_ids: Optional[Sequence[str]] = None) -> List[str]:
    """Subject IDs whose reduced-actigraphy file exists *and* is non-empty."""
    out = []
    for sid in (subject_ids or SUBJECT_IDS):
        p = os.path.join(data_dir, "activity", f"{sid}_activity.txt")
        if os.path.exists(p) and os.path.getsize(p) > 100:
            out.append(sid)
    return out


# --------------------------------------------------------------------------- dataset
@dataclass
class StagingDataset:
    """Rows aligned across all parallel outputs (one entry per scored epoch)."""

    rows: List[Dict[str, float]] = field(default_factory=list)
    y_wake: List[int] = field(default_factory=list)
    y_stage4: List[int] = field(default_factory=list)
    groups: List[str] = field(default_factory=list)        # subject (the CV split key)
    times: List[float] = field(default_factory=list)       # epoch start, seconds
    has_activity: List[int] = field(default_factory=list)  # 1 if actigraphy backed this row
    has_ibi: List[int] = field(default_factory=list)       # 1 if beat intervals backed this row
    #: sequence key for temporal ordering; differs from ``groups`` when the same night
    #: appears twice (e.g. dense + decimated copies in one training set)
    night_ids: List[str] = field(default_factory=list)

    def matrix(self, feature_names: Sequence[str]) -> List[List[float]]:
        return [feature_vector(r, feature_names) for r in self.rows]

    def extend(self, other: "StagingDataset") -> None:
        self.rows.extend(other.rows)
        self.y_wake.extend(other.y_wake)
        self.y_stage4.extend(other.y_stage4)
        self.groups.extend(other.groups)
        self.times.extend(other.times)
        self.has_activity.extend(other.has_activity)
        self.has_ibi.extend(other.has_ibi)
        self.night_ids.extend(other.night_ids)

    def __len__(self) -> int:
        return len(self.rows)


def build_subject_rows(
    subject_id: str,
    data_dir: str = DEFAULT_DATA_DIR,
    *,
    use_activity: bool = True,
    hr_decimate_s: float = 0.0,
    require_activity: bool = False,
    night_suffix: str = "",
    use_ibi: bool = True,
    require_ibi: bool = False,
) -> StagingDataset:
    """Feature rows + targets for one subject-night.

    ``hr_decimate_s`` thins the HR series (e.g. 60.0 → one sample/min) to simulate the
    sparse-HR path the live controller may hand us. ``use_ibi`` loads ``<ID>_ibi.txt`` when
    it exists and adds the HRV block to every row (no file → rows unchanged).
    """
    hr = _parse_pairs(os.path.join(data_dir, f"{subject_id}_heartrate.txt"))
    labels = _parse_labels(os.path.join(data_dir, f"{subject_id}_labeled_sleep.txt"))
    act: List[Tuple[float, ...]] = []
    if use_activity:
        act = parse_activity(os.path.join(data_dir, "activity", f"{subject_id}_activity.txt"))
    ibi: List[Sample] = []
    if use_ibi:
        ibi = _parse_pairs(os.path.join(data_dir, f"{subject_id}_ibi.txt"))

    ds = StagingDataset()
    if not hr or not labels:
        return ds
    if require_activity and not act:
        return ds
    if require_ibi and not ibi:
        return ds
    if hr_decimate_s:
        hr = _decimate(hr, hr_decimate_s)

    onset_t: Optional[float] = None
    for t, code in labels:
        if _stage4(code) in (1, 2, 3):
            onset_t = t
            break
    total_minutes = (labels[-1][0] / 60.0) if labels else None

    hr_ts = [t for t, _ in hr]
    act_ts = [s[0] for s in act]
    ibi_ts = [t for t, _ in ibi]
    # per-night HRV distribution source: 2 min bucket summaries, known from their end time
    buckets = hrv_bucket_summaries(ibi) if ibi else []

    # causal "night so far" state, advanced monotonically with the epochs
    hr_sorted: List[float] = []
    act_sorted: List[float] = []
    hrv_sorted: Dict[str, List[float]] = {k: [] for k, _src in HRV_NORM_KEYS}
    hr_i = 0
    act_i = 0
    ibi_i = 0
    b_i = 0

    for t, code in labels:
        s4 = _stage4(code)
        epoch_end = t + EPOCH_S
        # advance the causal history to epoch_end
        while hr_i < len(hr) and hr_ts[hr_i] <= epoch_end:
            bisect.insort(hr_sorted, hr[hr_i][1])
            hr_i += 1
        while act_i < len(act) and act_ts[act_i] <= epoch_end:
            bisect.insort(act_sorted, act[act_i][1])
            act_i += 1
        while ibi_i < len(ibi) and ibi_ts[ibi_i] <= epoch_end:
            ibi_i += 1
        while b_i < len(buckets) and buckets[b_i][0] <= epoch_end:
            for k, _src in HRV_NORM_KEYS:
                bisect.insort(hrv_sorted[k], buckets[b_i][1][k])
            b_i += 1
        if s4 is None:
            continue  # unscored: history still advanced above, row skipped

        norm_stats = stats_from_sorted(hr_sorted, act_sorted,
                                       hrv_sorted=hrv_sorted if ibi else None)
        # only the trailing lookback can matter -> slice for speed (identical results)
        lo_hr = bisect.bisect_left(hr_ts, epoch_end - MAX_LOOKBACK_S)
        lo_act = bisect.bisect_left(act_ts, epoch_end - MAX_LOOKBACK_S)
        lo_ibi = bisect.bisect_left(ibi_ts, epoch_end - MAX_LOOKBACK_S)
        feats = compute_features(
            hr[lo_hr:hr_i],
            act[lo_act:act_i] if act else None,
            epoch_end,
            norm_stats=norm_stats,
            minutes_since_start=t / 60.0,
            minutes_since_onset=((t - onset_t) / 60.0) if onset_t is not None else 0.0,
            total_minutes=total_minutes,
            include_activity=True,
            ibi_samples=ibi[lo_ibi:ibi_i] if ibi else None,
        )
        if feats["hr_n_samples"] < MIN_HR_SAMPLES:
            continue
        ds.rows.append(feats)
        ds.y_wake.append(0 if s4 == 0 else 1)
        ds.y_stage4.append(s4)
        ds.groups.append(subject_id)
        ds.night_ids.append(subject_id + night_suffix)
        ds.times.append(float(t))
        ds.has_activity.append(1 if feats.get("act_present", 0.0) > 0 else 0)
        ds.has_ibi.append(1 if feats.get("hrv_present_w10", 0.0) > 0 else 0)
    return ds


def build_dataset(
    data_dir: str = DEFAULT_DATA_DIR,
    subject_ids: Optional[Sequence[str]] = None,
    *,
    use_activity: bool = True,
    hr_decimate_s: float = 0.0,
    require_activity: bool = False,
    night_suffix: str = "",
    verbose: bool = False,
    use_ibi: bool = True,
    require_ibi: bool = False,
    jobs: int = 1,
) -> StagingDataset:
    """Rows for every subject. ``jobs > 1`` builds subjects in a process pool (the HRV block
    makes a DREAMT night ~1 min of feature work); results are concatenated in ID order."""
    combined = StagingDataset()
    sids = list(subject_ids or SUBJECT_IDS)
    kw = dict(data_dir=data_dir, use_activity=use_activity, hr_decimate_s=hr_decimate_s,
              require_activity=require_activity, night_suffix=night_suffix,
              use_ibi=use_ibi, require_ibi=require_ibi)
    if jobs and jobs > 1 and len(sids) > 1:
        import multiprocessing
        with multiprocessing.Pool(min(int(jobs), len(sids))) as pool:
            subs = pool.map(_build_subject_kw, [(sid, kw) for sid in sids])
    else:
        subs = [build_subject_rows(sid, **kw) for sid in sids]
    for sid, sub in zip(sids, subs):
        if verbose:
            print(f"  {sid}: {len(sub)} epochs"
                  f"{' (+activity)' if sub.has_activity and sub.has_activity[0] else ''}"
                  f"{' (+ibi)' if sub.has_ibi and any(sub.has_ibi) else ''}",
                  flush=True)
        combined.extend(sub)
    return combined


def _build_subject_kw(args) -> StagingDataset:
    sid, kw = args
    return build_subject_rows(sid, **kw)


def concat(*datasets: StagingDataset) -> StagingDataset:
    """Stack datasets (e.g. dense + decimated copies of the same nights)."""
    out = StagingDataset()
    for d in datasets:
        out.extend(d)
    return out


__all__ = [
    "StagingDataset",
    "concat",
    "build_dataset",
    "build_subject_rows",
    "subjects_with_activity",
    "subjects_with_ibi",
    "discover_subjects",
    "parse_activity",
    "SUBJECT_IDS",
    "DEFAULT_DATA_DIR",
    "STAGE4_LABELS",
    "EPOCH_S",
    "FEATURE_NAMES_HR",
    "FEATURE_NAMES_HRMOTION",
    "FEATURE_NAMES_HRMOTION_ABS",
    "FEATURE_NAMES_HRV",
    "FEATURE_NAMES_HR_HRV",
    "FEATURE_NAMES_HRV_MOTION",
    "ACT_FEATURES_SCALEFREE",
    "ACT_FEATURES_ABSOLUTE",
]
