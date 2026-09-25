#!/usr/bin/env python3
"""Score the LIVE staging path -- stager plus every post-processing step -- against EEG labels.

The CV numbers in ``cv_report_bidsleep.json`` describe the stager's own HMM output. What the
controller reports is that output after the actigraphy wake override, the beat-interval
rescorer, deep corroboration, the hypnogram constraint and the stage hold, all driven by the
onset detector's clock. This replays held-out nights through exactly those calls, one 30 s
tick at a time, and scores every step:

    # 1. fold models, grouped by subject, so every replayed night is held out
    python3 scripts/eval_live_staging.py train-folds --data-dir $SCRATCH/bidsleep/reduced \\
        --sleep-accel-dir $SCRATCH/sleep_accel --cache-dir $SCRATCH/bidsleep/cache \\
        --folds-dir $SCRATCH/live_eval/folds
    # 2. replay every held-out night once per variant (stager outputs are cached per night)
    python3 scripts/eval_live_staging.py replay --data-dir $SCRATCH/bidsleep/reduced \\
        --folds-dir $SCRATCH/live_eval/folds --out-dir $SCRATCH/live_eval/runs
    # 3. tables (all nights, and the tune / confirm subject halves)
    python3 scripts/eval_live_staging.py report --out-dir $SCRATCH/live_eval/runs

Per tick, as ``SleepController.decide`` does it: the frame carries the latest HR, the
movement index of the latest actigraphy batch, and the trailing 45 min of dense HR and batch
counts; ``estimate_sleep_stage`` -> ``constrain`` -> ``_hold_stage`` -> ``hypnogram.observe``,
then ``SleepOnsetDetector.evaluate`` on the adopted stage, with the 60-frame buffer. The rest
of ``decide`` (thermal, data-quality holds on channels BIDSleep does not record, bed exit) is
left out: it does not change a stage.

Input fidelity: BIDSleep is an Apple Watch on the wrist, HR every ~5 s and 30 s actigraphy
epochs. Each epoch is split into seven ~4.3 s batches at the epoch's mean absolute deviation
(the Verity sends about seven per 30 s). There are no beat intervals, so the rescorer never
fires here; it is judged on the recorded nights instead.

Dataset: BIDSleep Apple Watch Dataset v1.0.1 (PhysioNet), ODC-By 1.0,
https://doi.org/10.13026/rees-1092; sleep-accel (Walch et al. 2019, doi:10.13026/hmhs-py35,
ODC-By 1.0) joins every fold's training set. Data and fold weights stay in the scratch folder.
"""
from __future__ import annotations

import argparse
import bisect
import glob
import json
import os
import pickle
import random
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

EPOCH_S = 30.0
HISTORY_S = 45 * 60.0          # live_daemon._read_frame: read_history(minutes=45.0)
RECENT_FRAMES = 60             # loop.cycle: the controller's recent-frame buffer
BATCHES_PER_EPOCH = 7          # Verity PMD batches per 30 s (measured on the recorded nights)
SAMPLES_PER_BATCH = 52.0 * EPOCH_S / BATCHES_PER_EPOCH
FRESH_S = 30.0                 # adapters.wearable.fuse_sample max_age_s
STAGES = ["wake", "light", "deep", "rem"]
_IDX = {"awake": 0, "light": 1, "deep": 2, "rem": 3}

#: Post-processing variants, as tunable overrides on ``PREVIOUS`` (the post-processing as
#: shipped before this evaluation). ``shipped`` is that config unchanged.
VARIANTS: Dict[str, dict] = {
    "model_only": dict(est_stage_actigraphy_wake_enabled=False, autonomic_rescoring_enabled=False,
                       deep_corroboration=False, hypnogram_constraints=False, stage_hold_ticks=1),
    "+actigraphy_wake": dict(autonomic_rescoring_enabled=False, deep_corroboration=False,
                             hypnogram_constraints=False, stage_hold_ticks=1),
    "+corroboration": dict(autonomic_rescoring_enabled=False, hypnogram_constraints=False,
                           stage_hold_ticks=1),
    "+hypnogram": dict(autonomic_rescoring_enabled=False, stage_hold_ticks=1),
    "shipped": dict(),
    "shipped-actigraphy_wake": dict(est_stage_actigraphy_wake_enabled=False),
    "shipped-corroboration": dict(deep_corroboration=False),
    "shipped-hypnogram": dict(hypnogram_constraints=False),
    "shipped-hold": dict(stage_hold_ticks=1),
    # candidates
    "prov10": dict(provisional_onset_min=10.0),
    "prov15": dict(provisional_onset_min=15.0),
    "prov20": dict(provisional_onset_min=20.0),
    "awake1": dict(reentry_min_awake_min=1.0),
    "awake2": dict(reentry_min_awake_min=2.0),
    "reentry2": dict(reentry_light_min=2.0),
    "reentry0": dict(reentry_light_min=0.0),
    "hold3": dict(stage_hold_ticks=3),
    "prov5": dict(provisional_onset_min=5.0),
    "awake3": dict(reentry_min_awake_min=3.0),
    "awake5": dict(reentry_min_awake_min=5.0),
    "deepre2": dict(deep_reentry_light_min=2.0),
    "deepre5": dict(deep_reentry_light_min=5.0),
    "combo_a": dict(deep_corroboration=False, provisional_onset_min=10.0,
                    reentry_min_awake_min=2.0),
    "combo_b": dict(deep_corroboration=False, provisional_onset_min=10.0,
                    reentry_min_awake_min=2.0, reentry_light_min=2.0),
    "combo_c": dict(deep_corroboration=False, provisional_onset_min=5.0,
                    reentry_min_awake_min=2.0),
    "combo_d": dict(deep_corroboration=False, provisional_onset_min=10.0,
                    reentry_min_awake_min=3.0),
    "combo_a_nohold": dict(deep_corroboration=False, provisional_onset_min=10.0,
                           reentry_min_awake_min=2.0, stage_hold_ticks=1),
    "combo_a_noconstraint": dict(deep_corroboration=False, hypnogram_constraints=False),
    "combo_a_rec5": dict(deep_corroboration=False, provisional_onset_min=10.0,
                         reentry_min_awake_min=2.0, reentry_recurrent_awake_min=5.0),
    "combo_a_rec10": dict(deep_corroboration=False, provisional_onset_min=10.0,
                          reentry_min_awake_min=2.0, reentry_recurrent_awake_min=10.0),
    "combo_a_rec20": dict(deep_corroboration=False, provisional_onset_min=10.0,
                          reentry_min_awake_min=2.0, reentry_recurrent_awake_min=20.0),
}


def _log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def subject_of(night_id: str) -> str:
    return night_id.split("_n")[0]


# --------------------------------------------------------------------------- 1. fold models
def train_folds(args) -> int:
    import numpy as np
    from sklearn.model_selection import GroupKFold

    import train_bidsleep as TB
    from sleepctl.ml.sleep_staging import train as T
    from sleepctl.ml.sleep_staging.dataset import (SUBJECT_IDS, concat, discover_subjects,
                                                   subjects_with_activity)
    from sleepctl.ml.sleep_staging.features import (FEATURE_NAMES_HR,
                                                    FEATURE_NAMES_HRMOTION_SCALEFREE)

    shipped = json.load(open(os.path.join(T.WEIGHTS_DIR, "hmm.json")))
    spec = dict(kind="rf", n_estimators=120, max_depth=12, min_samples_leaf=40)
    bid_ids = discover_subjects(args.data_dir)
    bid = TB.load_corpus(args.data_dir, bid_ids, args.cache_dir, "bidsleep", args.jobs,
                         subjects_with_activity(args.data_dir, bid_ids), True)
    sa = None
    if args.sleep_accel_dir:
        sa_ids = [s for s in SUBJECT_IDS if os.path.exists(
            os.path.join(args.sleep_accel_dir, f"{s}_labeled_sleep.txt"))]
        sa = TB.load_corpus(args.sleep_accel_dir, sa_ids, args.cache_dir, "sleepaccel",
                            args.jobs, subjects_with_activity(args.sleep_accel_dir, sa_ids), False)
    subjects = sorted({subject_of(n) for n in bid_ids})
    gkf = GroupKFold(n_splits=args.folds)
    splits = list(gkf.split(subjects, groups=subjects))
    for k, (_tr, te) in enumerate(splits):
        test = {subjects[i] for i in te}
        out = os.path.join(args.folds_dir, str(k))
        if os.path.exists(os.path.join(out, "hmm.json")):
            _log(f"fold {k}: exists")
            continue
        os.makedirs(out, exist_ok=True)

        def keep(ds):
            idx = [i for i, g in enumerate(ds.groups) if subject_of(g) not in test]
            return _subset(ds, idx)

        for suffix, parts, names in (
                ("hr", [keep(bid["hr"]), keep(bid["sparse"])]
                 + ([sa["hr"], sa["sparse"]] if sa else []), FEATURE_NAMES_HR),
                ("hrmotion", [keep(bid["motion"])] + ([sa["motion"]] if sa and "motion" in sa
                                                       else []),
                 FEATURE_NAMES_HRMOTION_SCALEFREE)):
            ds = TB.thin(concat(*parts), args.train_stride)
            X = np.asarray(ds.matrix(names), dtype=float)
            t0 = time.time()
            stage = T._make(spec, balanced=True)
            stage.fit(X, np.asarray(ds.y_stage4, dtype=int))
            wake = T._make(spec, balanced=False)           # the shipped wake head is "natural"
            wake.fit(X, np.asarray(ds.y_wake, dtype=int))
            T.write_json(os.path.join(out, f"stage4_{suffix}.json"), T.export_forest(stage, names))
            T.write_json(os.path.join(out, f"wake_{suffix}.json"), T.export_forest(wake, names))
            _log(f"fold {k} {suffix}: {len(ds)} rows, {time.time() - t0:.0f}s")
        dense = concat(*([keep(bid["hr"])] + ([sa["hr"]] if sa else [])))
        hmm = T.estimate_hmm(np.asarray(dense.y_stage4), dense.night_ids, dense.times)
        for key in ("temper", "smoothing_epochs", "emission_prior"):
            hmm[key] = shipped[key]
        T.write_json(os.path.join(out, "hmm.json"), hmm)
        with open(os.path.join(out, "test_subjects.json"), "w") as fh:
            json.dump(sorted(test), fh)
    return 0


def _subset(ds, idx):
    from sleepctl.ml.sleep_staging.dataset import StagingDataset
    out = StagingDataset()
    for name in ("rows", "y_wake", "y_stage4", "groups", "times", "has_activity", "has_ibi",
                 "night_ids"):
        src = getattr(ds, name)
        setattr(out, name, [src[i] for i in idx] if len(src) == len(ds) else list(src))
    return out


# --------------------------------------------------------------------------- 2. replay
class CachingStager:
    """The fold's stager behind a cache keyed by everything ``predict`` reads that can vary
    between variants of the same night: the tick and the onset clock."""

    def __init__(self, stager, cache: dict) -> None:
        self.stager = stager
        self.cache = cache
        self.available = stager.available
        self.hits = self.misses = 0

    def predict(self, hr_samples, activity_samples=None, minutes_since_start=None,
                minutes_since_onset=None, **kw):
        key = (round(float(hr_samples[-1][0]), 3) if hr_samples else None,
               None if minutes_since_start is None else round(minutes_since_start, 3),
               None if minutes_since_onset is None else round(minutes_since_onset, 3),
               bool(activity_samples))
        if key in self.cache:
            self.hits += 1
            return self.cache[key]
        self.misses += 1
        est = self.stager.predict(hr_samples, activity_samples=activity_samples,
                                  minutes_since_start=minutes_since_start,
                                  minutes_since_onset=minutes_since_onset, **kw)
        self.cache[key] = est
        return est


def load_night(data_dir: str, night: str) -> dict:
    hr = []
    for line in open(os.path.join(data_dir, f"{night}_heartrate.txt")):
        try:
            t, v = line.strip().split(",")[:2]
            hr.append((float(t), float(v)))
        except ValueError:
            continue
    hr.sort()
    labels = []
    for line in open(os.path.join(data_dir, f"{night}_labeled_sleep.txt")):
        p = line.split()
        if len(p) >= 2:
            labels.append((float(p[0]), int(float(p[1]))))
    labels.sort()
    act = []
    path = os.path.join(data_dir, "activity", f"{night}_activity.txt")
    if os.path.exists(path):
        for line in open(path):
            if line.startswith("#"):
                continue
            p = line.split(",")
            t0, mad = float(p[0]), float(p[3])
            for j in range(BATCHES_PER_EPOCH):
                act.append((t0 + EPOCH_S * (j + 1) / BATCHES_PER_EPOCH, mad * SAMPLES_PER_BATCH))
    act.sort()
    meta = json.load(open(os.path.join(data_dir, f"{night}_meta.json")))
    return dict(hr=hr, labels=labels, act=act, t0=float(meta.get("recStart_unix") or 0.0))


def _truth(code: int) -> Optional[int]:
    return {0: 0, 1: 1, 2: 1, 3: 2, 5: 3}.get(code)


def replay_night(night: dict, stager, overrides: dict, use_motion: bool = True) -> dict:
    """One night through the live staging calls. Returns per-tick truth / model / final."""
    from dashboard.api.app.bridge import actigraphy_movement_index
    from sleepctl.config import AppConfig
    from sleepctl.controller import state_estimator as SE
    from sleepctl.controller.controller import SleepController
    from sleepctl.controller.hypnogram import constrain
    from sleepctl.models import SensorFrame, SleepStage

    cfg = AppConfig.default()
    for k, v in overrides.items():
        setattr(cfg.tunables, k, v)
    ctl = SleepController(cfg)
    SE._STAGER, SE._STAGER_LOADED, SE._RESCORER = stager, True, None
    base = night["t0"]
    hr, act = night["hr"], (night["act"] if use_motion else [])
    hr_t = [base + t for t, _ in hr]
    hr_abs = [(base + t, v) for t, v in hr]
    act_t = [base + t for t, _ in act]
    act_abs = [(base + t, v) for t, v in act]
    truth = {int(round(t / EPOCH_S)): _truth(c) for t, c in night["labels"]}
    n_ticks = int(round(night["labels"][-1][0] / EPOCH_S)) + 1 if night["labels"] else 0
    bed = datetime.fromtimestamp(base, tz=timezone.utc).replace(tzinfo=None)
    onset = None
    recent: list = []
    out = dict(truth=[], model=[], final=[], source=[], reason=[], onset_tick=None)
    min_hr = int(getattr(cfg.tunables, "stager_min_hr_samples", 5))
    for i in range(n_ticks):
        t_abs = base + EPOCH_S * (i + 1)
        now = bed + timedelta(seconds=EPOCH_S * (i + 1))
        j = bisect.bisect_right(hr_t, t_abs)
        cur_hr = hr_abs[j - 1][1] if j and t_abs - hr_t[j - 1] <= FRESH_S else None
        a = bisect.bisect_right(act_t, t_abs)
        mv = (actigraphy_movement_index(act_abs[a - 1][1])
              if a and t_abs - act_t[a - 1] <= FRESH_S else None)
        f = SensorFrame(timestamp=now, heart_rate=cur_hr, hrv=None, movement=mv,
                        respiratory_rate=None, presence=None, stage=SleepStage.UNKNOWN,
                        data_age_seconds=5.0)
        f.hr_history = hr_abs[bisect.bisect_left(hr_t, t_abs - HISTORY_S):j] or None
        if act:
            f.activity_history = act_abs[bisect.bisect_left(act_t, t_abs - HISTORY_S):a] or None
            f.activity_units = "counts" if f.activity_history else None
        base_hr, _ = ctl._sleep_baseline(recent)
        mss = (now - bed).total_seconds() / 60.0
        mso = (now - onset).total_seconds() / 60.0 if onset is not None else None
        # The stager's own label for this tick, before any post-processing: the same call
        # estimate_sleep_stage makes (a cache hit when it makes it too).
        h = [(float(t), float(v)) for t, v in (f.hr_history or [])]
        m = (stager.predict(h, activity_samples=f.activity_history if act else None,
                            minutes_since_start=mss, minutes_since_onset=mso,
                            ibi_samples=None)
             if len(h) >= min_hr else None)
        est = SE.estimate_sleep_stage(f, base_hr, recent, cfg, minutes_since_start=mss,
                                      minutes_since_onset=mso, resting_hr=None)
        reason = None
        if est is not None:
            est = constrain(est, now, cfg, ctl.hypnogram, onset)
            reason = ctl.hypnogram.last_reason
            est = ctl._hold_stage(est, cfg)
            f.stage, f.stage_confidence, src = est
            f.stage_source = src
            ctl.hypnogram.observe(f.stage, now, cfg)
        else:
            src = None
        if onset is None:
            ev = ctl.onset_detector.evaluate(f, recent, now, bed_entry_time=bed)
            if ev is not None:
                onset = ev.timestamp
                out["onset_tick"] = i
                out["onset_min"] = round((onset - bed).total_seconds() / 60.0, 1)
        recent.append(f)
        if len(recent) > RECENT_FRAMES:
            recent.pop(0)
        if truth.get(i) is None:
            continue
        out["truth"].append(truth[i])
        out["model"].append(STAGES.index(m.stage_label) if m is not None else -1)
        out["final"].append(_IDX.get(f.stage.value, -1))
        out["source"].append(src)
        out["reason"].append(reason)
    return out


_FOLD_STAGERS: Dict[str, object] = {}


def _fold_stager(folds_dir: str, fold: str):
    from sleepctl.ml.sleep_staging.infer import SleepStager
    if fold not in _FOLD_STAGERS:
        _FOLD_STAGERS[fold] = SleepStager.load(os.path.join(folds_dir, fold))
    return _FOLD_STAGERS[fold]


def _replay_job(job) -> str:
    night, fold, args, variants = job
    path = os.path.join(args.out_dir, "nights", f"{night}.pkl")
    done = {}
    if os.path.exists(path):
        done = pickle.load(open(path, "rb"))
    again = set((args.recompute or "").split(","))
    todo = [v for v in variants if v not in done.get("variants", {}) or v in again]
    if not todo:
        return f"{night}: cached"
    cache_path = os.path.join(args.out_dir, "stager_cache", f"{night}.pkl")
    cache = pickle.load(open(cache_path, "rb")) if os.path.exists(cache_path) else {}
    stager = CachingStager(_fold_stager(args.folds_dir, fold), cache)
    data = load_night(args.data_dir, night)
    res = done.setdefault("variants", {})
    done.update(night=night, subject=subject_of(night), fold=fold)
    t0 = time.time()
    for v in todo:
        res[v] = replay_night(data, stager, all_variants()[v])
    tmp = path + ".tmp"
    pickle.dump(done, open(tmp, "wb"))
    os.replace(tmp, path)
    if stager.misses:
        pickle.dump(cache, open(cache_path + ".tmp", "wb"))
        os.replace(cache_path + ".tmp", cache_path)
    return f"{night}: {len(todo)} variants, {stager.misses} stager calls, {time.time() - t0:.0f}s"


#: The post-processing as shipped before this evaluation. Every variant above is written
#: relative to it, so the table stays comparable whatever the current defaults are.
PREVIOUS = dict(deep_corroboration=True, provisional_onset_min=0.0, reentry_min_awake_min=0.0,
                reentry_recurrent_awake_min=0.0)


def all_variants() -> Dict[str, dict]:
    out = {k: dict(PREVIOUS, **v) for k, v in VARIANTS.items()}
    out["current"] = {}            # whatever the code ships now
    return out


def replay(args) -> int:
    from multiprocessing import Pool
    os.makedirs(os.path.join(args.out_dir, "nights"), exist_ok=True)
    os.makedirs(os.path.join(args.out_dir, "stager_cache"), exist_ok=True)
    fold_of = {}
    for d in sorted(glob.glob(os.path.join(args.folds_dir, "*", "test_subjects.json"))):
        for s in json.load(open(d)):
            fold_of[s] = os.path.basename(os.path.dirname(d))
    nights = sorted(os.path.basename(p)[:-len("_meta.json")]
                    for p in glob.glob(os.path.join(args.data_dir, "*_meta.json")))
    nights = [n for n in nights if subject_of(n) in fold_of
              and os.path.exists(os.path.join(args.data_dir, "activity", f"{n}_activity.txt"))]
    if args.only_folds:
        keep = set(args.only_folds.split(","))
        nights = [n for n in nights if fold_of[subject_of(n)] in keep]
    if args.limit:
        random.Random(0).shuffle(nights)
        nights = sorted(nights[:args.limit])
    variants = args.variants.split(",") if args.variants else list(all_variants())
    jobs = [(n, fold_of[subject_of(n)], args, variants) for n in nights]
    # one fold at a time per worker keeps each process to one loaded stager
    jobs.sort(key=lambda j: (j[1], j[0]))
    _log(f"{len(jobs)} nights x {len(variants)} variants")
    with Pool(args.jobs, maxtasksperchild=40) as pool:
        for k, msg in enumerate(pool.imap_unordered(_replay_job, jobs, chunksize=1)):
            _log(f"[{k + 1}/{len(jobs)}] {msg}")
    return 0


# --------------------------------------------------------------------------- 3. metrics
def metrics(pairs_by_night: List[dict], key: str) -> dict:
    """Pooled epoch metrics plus per-night minute errors (wake/light/deep/rem = 0..3)."""
    cm = [[0] * 5 for _ in range(4)]          # column 4: no label (stager not yet running)
    dmae, dbias, rmae, rbias = [], [], [], []
    for n in pairs_by_night:
        yt, yp = n["truth"], n[key]
        for a, b in zip(yt, yp):
            cm[a][b if 0 <= b < 4 else 4] += 1
        for cls, mae, bias in ((2, dmae, dbias), (3, rmae, rbias)):
            e = (sum(1 for b in yp if b == cls) - sum(1 for a in yt if a == cls)) * 0.5
            mae.append(abs(e))
            bias.append(e)
    tot = sum(map(sum, cm))
    po = sum(cm[i][i] for i in range(4)) / tot
    rows = [sum(cm[i]) for i in range(4)]
    cols = [sum(cm[i][j] for i in range(4)) for j in range(5)]
    pe = sum(rows[i] * cols[i] for i in range(4)) / (tot * tot)
    kappa = (po - pe) / (1 - pe)

    def pr(c):
        p = cm[c][c] / cols[c] if cols[c] else float("nan")
        r = cm[c][c] / rows[c] if rows[c] else float("nan")
        return p, r
    sleep_t = sum(rows[1:])
    sleep_p = sum(cols[1:4])
    out = {"kappa": kappa, "acc": po, "n_epochs": tot, "n_nights": len(pairs_by_night)}
    for c, name in enumerate(STAGES):
        out[f"{name}_p"], out[f"{name}_r"] = pr(c)
    wp, wr = out["wake_p"], out["wake_r"]
    out["wake_f1"] = 2 * wp * wr / (wp + wr) if wp + wr else 0.0
    out["deep_share"] = cols[2] / sleep_p if sleep_p else float("nan")
    out["rem_share"] = cols[3] / sleep_p if sleep_p else float("nan")
    out["deep_share_true"] = rows[2] / sleep_t
    out["rem_share_true"] = rows[3] / sleep_t
    out["wake_share"] = cols[0] / tot
    out["wake_share_true"] = rows[0] / tot
    out["deep_mae"] = sum(dmae) / len(dmae)
    out["deep_bias"] = sum(dbias) / len(dbias)
    out["rem_mae"] = sum(rmae) / len(rmae)
    out["rem_bias"] = sum(rbias) / len(rbias)
    return out


def _load_runs(out_dir: str) -> List[dict]:
    return [pickle.load(open(p, "rb"))
            for p in sorted(glob.glob(os.path.join(out_dir, "nights", "*.pkl")))]


def _half(runs: List[dict], which: str) -> List[dict]:
    """Subject halves: 'tune' = folds 0-2, 'confirm' = folds 3-4 (whole subjects)."""
    if which == "all":
        return runs
    tune = {"0", "1", "2"}
    return [r for r in runs if (r["fold"] in tune) == (which == "tune")]


def boot_delta(runs, a: str, b: str, fields, n: int = 1000, key: str = "final") -> dict:
    """Subject-level bootstrap of metric(b) - metric(a): mean and 95% interval."""
    subs = sorted({r["subject"] for r in runs})
    by = {s: [r for r in runs if r["subject"] == s] for s in subs}
    rng = random.Random(0)
    draws = {f: [] for f in fields}
    for _ in range(n):
        pick = [x for s in (rng.choice(subs) for _ in subs) for x in by[s]]
        ma = metrics([r["variants"][a] for r in pick], key)
        mb = metrics([r["variants"][b] for r in pick], key)
        for f in fields:
            draws[f].append(mb[f] - ma[f])
    out = {}
    for f in fields:
        d = sorted(draws[f])
        out[f] = (sum(d) / n, d[int(0.025 * n)], d[int(0.975 * n) - 1])
    return out


COLS = [("kappa", "k", 3), ("wake_p", "wakeP", 2), ("wake_r", "wakeR", 2),
        ("wake_f1", "wakeF1", 3), ("light_p", "lightP", 2), ("light_r", "lightR", 2),
        ("deep_p", "deepP", 2), ("deep_r", "deepR", 2), ("rem_p", "remP", 2),
        ("rem_r", "remR", 2), ("deep_share", "deep%", 3), ("rem_share", "rem%", 3),
        ("deep_mae", "deepMAE", 1), ("deep_bias", "deepBias", 1), ("rem_mae", "remMAE", 1),
        ("rem_bias", "remBias", 1)]


def report(args) -> int:
    runs = _load_runs(args.out_dir)
    wanted = args.variants.split(",") if args.variants else list(all_variants())
    result = {}
    for half in ("all", "tune", "confirm"):
        sel = _half(runs, half)
        if not sel:
            continue
        m0 = metrics([r["variants"]["shipped"] for r in sel], "model")
        print(f"\n== {half}: {len(sel)} nights, {len({r['subject'] for r in sel})} subjects; "
              f"truth deep {m0['deep_share_true']:.3f} rem {m0['rem_share_true']:.3f} of sleep, "
              f"wake {m0['wake_share_true']:.3f} of epochs")
        print(f"  {'variant':<28}" + "".join(f"{c:>9}" for _k, c, _p in COLS))
        rows = {"stager ticks (shipped run)": m0}
        for v in [v for v in wanted if all(v in r["variants"] for r in sel)]:
            rows[v] = metrics([r["variants"][v] for r in sel], "final")
        for name, m in rows.items():
            print(f"  {name:<28}" + "".join(f"{m[k]:>9.{p}f}" for k, _c, p in COLS))
        result[half] = rows
    if args.boot:
        base = "shipped"
        fields = ["kappa", "wake_f1", "wake_p", "wake_r", "deep_r", "deep_p", "rem_p", "deep_mae"]
        for half in ("tune", "confirm", "all"):
            sel = _half(runs, half)
            print(f"\n== bootstrap over subjects ({half}), delta vs {base}")
            for v in args.boot.split(","):
                d = boot_delta(sel, base, v, fields, n=args.n_boot)
                print(f"  {v:<28}" + "  ".join(f"{f} {m:+.3f} [{lo:+.3f},{hi:+.3f}]"
                                               for f, (m, lo, hi) in d.items()))
    reasons = Counter()
    lost = Counter()
    for r in runs:
        p = r["variants"]["shipped"]
        for tr, rs, src in zip(p["truth"], p["reason"], p["source"]):
            if rs:
                reasons[rs] += 1
                lost[(rs, STAGES[tr])] += 1
    print("\nhypnogram reclassifications (shipped):", dict(reasons.most_common()))
    print("  by true stage:", {f"{a}/{b}": n for (a, b), n in sorted(lost.items())})
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(result, fh, indent=1, default=float)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("train-folds")
    a.add_argument("--data-dir", required=True)
    a.add_argument("--sleep-accel-dir", default=None)
    a.add_argument("--cache-dir", default=None)
    a.add_argument("--folds-dir", required=True)
    a.add_argument("--folds", type=int, default=5)
    a.add_argument("--train-stride", type=int, default=2)
    a.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    b = sub.add_parser("replay")
    b.add_argument("--data-dir", required=True)
    b.add_argument("--folds-dir", required=True)
    b.add_argument("--out-dir", required=True)
    b.add_argument("--variants", default=None, help="comma list (default: all)")
    b.add_argument("--limit", type=int, default=0, help="a random subset of nights")
    b.add_argument("--only-folds", default=None, help="comma list of fold names")
    b.add_argument("--recompute", default="current",
                   help="variants replayed again even when stored ('current' follows the code)")
    b.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    c = sub.add_parser("report")
    c.add_argument("--out-dir", required=True)
    c.add_argument("--variants", default=None)
    c.add_argument("--boot", default=None, help="variants to bootstrap against shipped")
    c.add_argument("--n-boot", type=int, default=500)
    c.add_argument("--json", default=None)
    args = ap.parse_args(argv)
    return {"train-folds": train_folds, "replay": replay, "report": report}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
