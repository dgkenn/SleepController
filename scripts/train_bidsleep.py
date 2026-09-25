#!/usr/bin/env python3
"""Train the HR / HR+motion staging models on BIDSleep and compare them to the bundled ones.

    python3 scripts/train_bidsleep.py --data-dir <scratch>/bidsleep/reduced \\
        --sleep-accel-dir <scratch>/sleep_accel --cache-dir <scratch>/bidsleep/cache
    python3 scripts/train_bidsleep.py ... --write        # also export weights (see below)

Reads the reduction written by ``scripts/bidsleep_reduce.py`` (one recording per night, IDs
``Bidslab00_n1`` ...). Every night is its own sequence, but cross-validation folds are
grouped by SUBJECT, so no subject is ever split across train and test.

What it does:

1. Scores the BUNDLED ``wake_hr``/``stage4_hr`` and ``wake_hrmotion``/``stage4_hrmotion``
   weights (trained on PhysioNet sleep-accel) with the bundled ``hmm.json`` on every
   BIDSleep night. None of those subjects was ever seen by them, so every night is
   held-out for the bundled models.
2. Trains the same tree-ensemble + HMM pipeline (``sleepctl.ml.sleep_staging.train``) on
   BIDSleep -- and, with ``--sleep-accel-dir``, on BIDSleep + sleep-accel -- under
   subject-grouped K-fold CV, and scores the held-out BIDSleep subjects with the same
   metrics: 4-class kappa (unsmoothed and HMM-smoothed), wake precision/recall, deep (N3)
   and REM precision/recall, predicted deep/REM share of sleep, and per-night deep- and
   REM-minute errors.
3. With ``--write`` (and only if the new models beat the bundled ones on held-out subjects
   -- ``--force`` overrides), fits the chosen variant on all nights and writes
   ``wake_hr.json``, ``stage4_hr.json``, ``wake_hrmotion.json``, ``stage4_hrmotion.json``
   and ``hmm.json`` (re-estimated on the training corpus, smoothing tuned by the same CV)
   into ``--out``, plus ``cv_report_bidsleep.json`` next to the weights folder.

numpy/sklearn are training-time only; the exported JSON runs on the pure-stdlib runtime.
Dataset attribution: BIDSleep Apple Watch Dataset v1.0.1 (PhysioNet), ODC-By 1.0,
https://doi.org/10.13026/rees-1092.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np  # noqa: E402

from sleepctl.ml.sleep_staging import train as T  # noqa: E402
from sleepctl.ml.sleep_staging.dataset import (  # noqa: E402
    SUBJECT_IDS, StagingDataset, build_dataset, concat, discover_subjects,
    subjects_with_activity)
from sleepctl.ml.sleep_staging.features import (  # noqa: E402
    FEATURE_NAMES_HR, FEATURE_NAMES_HRMOTION, FEATURE_NAMES_HRMOTION_SCALEFREE)
from sleepctl.ml.sleep_staging.infer import blend_emission  # noqa: E402

EPOCH_MIN = 0.5
ATTRIBUTION = {
    "dataset": "BIDSleep Apple Watch Dataset v1.0.1 (PhysioNet)",
    "url": "https://physionet.org/content/bidsleep-dataset/1.0.1/",
    "doi": "10.13026/rees-1092",
    "license": "Open Data Commons Attribution License v1.0 (ODC-By 1.0)",
    "citation_note": "Cite the dataset DOI and PhysioNet (Goldberger et al., Circulation 2000).",
}
SLEEP_ACCEL_ATTRIBUTION = {
    "dataset": "Motion and heart rate from a wrist-worn wearable and labeled sleep from "
               "polysomnography (sleep-accel) v1.0.0 (PhysioNet), Walch et al. 2019",
    "doi": "10.13026/hmhs-py35",
    "license": "Open Data Commons Attribution License v1.0 (ODC-By 1.0)",
}


def _log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


# --------------------------------------------------------------------------- fast HMM path
def _label_night_fast(emissions, pw_raw, hmm, smoothing_epochs: int, temper: float,
                      prior_mode: str, smooth: bool):
    """Vectorised twin of ``train._label_night`` (same maths, all epochs at once).

    ``train._label_night`` re-runs the pure-python forward filter over the trailing window at
    every epoch -- exactly what the runtime does, but ~200k epochs x 8 smoothing trials is
    tens of minutes. This runs the same windowed recursion for every epoch in parallel with
    numpy; :func:`_check_fast_path` asserts it labels identically before it is used.
    """
    E = np.asarray(emissions, dtype=float)
    n = len(E)
    if n == 0:
        return []
    if not smooth:
        lab = E.argmax(axis=1)
        lab[np.asarray(pw_raw, dtype=float) >= 0.5] = 0
        return lab.tolist()
    trans = np.asarray(hmm["trans"], dtype=float)
    start = np.asarray(hmm["start"], dtype=float)
    prior = np.full(4, 0.25) if prior_mode == "uniform" else np.asarray(hmm["prior"], float)
    lik = (np.maximum(E, 1e-9) / np.maximum(prior, 1e-9)) ** float(temper)
    k = np.arange(n)
    lo = np.maximum(0, k - int(smoothing_epochs) + 1)
    alpha = np.maximum(start, 1e-12)[None, :] * lik[lo]
    alpha /= alpha.sum(axis=1, keepdims=True)
    for s in range(1, int(smoothing_epochs)):
        i = lo + s
        valid = i <= k
        if not valid.any():
            break
        pred = alpha @ trans
        post = np.maximum(pred, 1e-12) * lik[np.minimum(i, n - 1)]
        post /= post.sum(axis=1, keepdims=True)
        alpha = np.where(valid[:, None], post, alpha)
    lab = alpha.argmax(axis=1)
    lab[alpha[:, 0] >= 0.5] = 0
    return lab.tolist()


_SLOW_LABEL_NIGHT = T._label_night


def _check_fast_path() -> None:
    rng = np.random.default_rng(0)
    hmm = json.load(open(os.path.join(T.WEIGHTS_DIR, "hmm.json")))
    for trial in range(4):
        n = 90
        p4 = rng.dirichlet(np.ones(4) * 0.7, size=n)
        pw = rng.uniform(0, 1, size=n)
        em = [blend_emission(p, w) for p, w in zip(p4, pw)]
        for pm in ("uniform", "empirical"):
            for sm in (True, False):
                a = _SLOW_LABEL_NIGHT(em, pw, hmm, 20, 0.35, pm, sm)
                b = _label_night_fast(em, pw, hmm, 20, 0.35, pm, sm)
                if list(a) != list(b):
                    raise AssertionError(f"fast HMM path disagrees (trial {trial} {pm} {sm})")


# --------------------------------------------------------------------------- numpy forest
class NpForest:
    """Vectorised scorer for the exported JSON forests (same traversal as infer._Forest)."""

    def __init__(self, d: dict) -> None:
        self.names = list(d["feature_names"])
        self.classes = [int(c) for c in d["classes"]]
        self.trees = []
        for tr in d["trees"]:
            self.trees.append((np.asarray(tr["f"], dtype=np.int64), np.asarray(tr["t"], float),
                               np.asarray(tr["l"], dtype=np.int64),
                               np.asarray(tr["r"], dtype=np.int64),
                               np.asarray(tr["v"], float).reshape(-1, len(self.classes))))

    @classmethod
    def load(cls, path: str) -> "NpForest":
        with open(path) as fh:
            return cls(json.load(fh))

    def proba(self, X: np.ndarray) -> np.ndarray:
        n = X.shape[0]
        acc = np.zeros((n, len(self.classes)))
        rows = np.arange(n)
        for f, th, left, right, vals in self.trees:
            node = np.zeros(n, dtype=np.int64)
            while True:
                feat = f[node]
                inner = feat >= 0
                if not inner.any():
                    break
                idx = rows[inner]
                nd = node[idx]
                go_left = X[idx, feat[idx]] <= th[nd]
                node[idx] = np.where(go_left, left[nd], right[nd])
            acc += vals[left[node]]
        acc /= max(1, len(self.trees))
        s = acc.sum(axis=1, keepdims=True)
        s[s <= 0] = 1.0
        return acc / s


def external_cache(train_ds: StagingDataset, test_ds: StagingDataset, names: Sequence[str],
                   spec: dict, stride: int) -> dict:
    """Fit on all of ``train_ds``, predict every night of ``test_ds`` (a different corpus)."""
    X = np.asarray(train_ds.matrix(names), dtype=float)
    y4 = np.asarray(train_ds.y_stage4, dtype=int)
    yw = np.asarray(train_ds.y_wake, dtype=int)
    hmm = T.estimate_hmm(y4, train_ds.night_ids, train_ds.times)
    sl = slice(None, None, max(1, stride))
    wb, wn, st = T._fit_heads(spec, X[sl], y4[sl], yw[sl], both_wake=True)
    Xt = np.asarray(test_ds.matrix(names), dtype=float)
    p4 = T._ordered4(st.predict_proba(Xt), list(st.classes_))

    def _pw(m):
        cls = list(m.classes_)
        return m.predict_proba(Xt)[:, cls.index(0)]
    pb, pn = _pw(wb), _pw(wn)
    nights = []
    for nid, idx in T._subject_index(list(test_ds.night_ids), list(test_ds.times),
                                     range(len(test_ds))):
        nights.append(dict(subject=nid, y_true=[int(test_ds.y_stage4[i]) for i in idx],
                           times=[test_ds.times[i] for i in idx],
                           p4=[list(map(float, p4[i])) for i in idx],
                           pw_balanced=[float(pb[i]) for i in idx],
                           pw_natural=[float(pn[i]) for i in idx], hmm=hmm))
    return dict(nights=nights, hmm_full=hmm, n_rows=len(test_ds))


def bundled_cache(ds: StagingDataset, wake_path: str, stage_path: str, hmm: dict) -> dict:
    """A ``cv_emissions``-shaped cache of the BUNDLED model's predictions on ``ds``."""
    wake = NpForest.load(wake_path)
    stage = NpForest.load(stage_path)
    Xs = np.asarray(ds.matrix(stage.names), dtype=float)
    Xw = Xs if wake.names == stage.names else np.asarray(ds.matrix(wake.names), dtype=float)
    ps = stage.proba(Xs)
    p4 = np.zeros((len(ds), 4))
    for j, c in enumerate(stage.classes):
        if 0 <= c < 4:
            p4[:, c] = ps[:, j]
    p4 /= np.maximum(p4.sum(axis=1, keepdims=True), 1e-12)
    pwall = wake.proba(Xw)
    pw = pwall[:, wake.classes.index(0)] if 0 in wake.classes else np.zeros(len(ds))
    nights = []
    for nid, idx in T._subject_index(list(ds.night_ids), list(ds.times), range(len(ds))):
        nights.append(dict(subject=nid, y_true=[int(ds.y_stage4[i]) for i in idx],
                           times=[ds.times[i] for i in idx],
                           p4=[list(map(float, p4[i])) for i in idx],
                           pw_balanced=[float(pw[i]) for i in idx],
                           pw_natural=[float(pw[i]) for i in idx], hmm=hmm))
    return dict(nights=nights, hmm_full=hmm, n_rows=len(ds))


# --------------------------------------------------------------------------- metrics
def _prf(yt: np.ndarray, yp: np.ndarray, c: int) -> Tuple[float, float]:
    tp = float(np.sum((yt == c) & (yp == c)))
    fp = float(np.sum((yt != c) & (yp == c)))
    fn = float(np.sum((yt == c) & (yp != c)))
    return (tp / (tp + fp) if tp + fp else 0.0), (tp / (tp + fn) if tp + fn else 0.0)


def full_metrics(cache: dict, sm: dict) -> dict:
    """Everything the comparison table needs, for one cache under one smoothing setting."""
    agg = {"y": [], "raw": [], "sm": []}
    nights = []
    for night in cache["nights"]:
        pw = night["pw_balanced"] if sm["wake_mode"] == "balanced" else night["pw_natural"]
        em = [blend_emission(p, w) for p, w in zip(night["p4"], pw)]
        lr = T._label_night(em, pw, night["hmm"], sm["epochs"], sm["temper"],
                            sm["prior_mode"], False)
        ls = T._label_night(em, pw, night["hmm"], sm["epochs"], sm["temper"],
                            sm["prior_mode"], True)
        agg["y"].extend(night["y_true"])
        agg["raw"].extend(lr)
        agg["sm"].extend(ls)
        yt = np.asarray(night["y_true"])
        rec = {"id": night["subject"]}
        for tag, lab in (("true", yt), ("raw", np.asarray(lr)), ("sm", np.asarray(ls))):
            sleep = max(1, int(np.sum(lab != 0)))
            rec[tag] = dict(deep_min=float(np.sum(lab == 2)) * EPOCH_MIN,
                            rem_min=float(np.sum(lab == 3)) * EPOCH_MIN,
                            deep_share=float(np.sum(lab == 2)) / sleep,
                            rem_share=float(np.sum(lab == 3)) / sleep)
        nights.append(rec)
    y = np.asarray(agg["y"])
    out: Dict[str, object] = {"n_epochs": int(len(y)), "n_nights": len(nights),
                              "smoothing": dict(sm)}
    for tag in ("raw", "sm"):
        yp = np.asarray(agg[tag])
        wp, wr = _prf(y, yp, 0)
        dp, dr = _prf(y, yp, 2)
        rp, rr = _prf(y, yp, 3)
        sl_t = max(1, int(np.sum(y != 0)))
        sl_p = max(1, int(np.sum(yp != 0)))

        def _mae(key):
            return float(np.mean([abs(n[tag][key] - n["true"][key]) for n in nights]))

        def _bias(key):
            return float(np.mean([n[tag][key] - n["true"][key] for n in nights]))
        out[tag] = dict(
            kappa4=T.cohen_kappa(y.tolist(), yp.tolist(), 4),
            acc4=float(np.mean(y == yp)),
            wake_kappa=T.cohen_kappa((y != 0).astype(int).tolist(), (yp != 0).astype(int).tolist(), 2),
            wake_prec=wp, wake_rec=wr, deep_prec=dp, deep_rec=dr, rem_prec=rp, rem_rec=rr,
            light_rec=_prf(y, yp, 1)[1],
            deep_share_true=float(np.sum(y == 2)) / sl_t, deep_share_pred=float(np.sum(yp == 2)) / sl_p,
            rem_share_true=float(np.sum(y == 3)) / sl_t, rem_share_pred=float(np.sum(yp == 3)) / sl_p,
            wake_share_true=float(np.mean(y == 0)), wake_share_pred=float(np.mean(yp == 0)),
            deep_min_mae=_mae("deep_min"), deep_min_bias=_bias("deep_min"),
            rem_min_mae=_mae("rem_min"), rem_min_bias=_bias("rem_min"),
            nights_deep_share_lt5=float(np.mean([n[tag]["deep_share"] < 0.05 for n in nights])),
            nights_rem_share_gt33=float(np.mean([n[tag]["rem_share"] > 0.33 for n in nights])),
        )
    return out


def print_table(title: str, rows: List[Tuple[str, dict]]) -> None:
    print(f"\n{title}")
    cols = [("k4", "kappa4", 3), ("wakeP", "wake_prec", 2), ("wakeR", "wake_rec", 2),
            ("deepP", "deep_prec", 2), ("deepR", "deep_rec", 2), ("remP", "rem_prec", 2),
            ("remR", "rem_rec", 2), ("deep%", "deep_share_pred", 3), ("rem%", "rem_share_pred", 3),
            ("deepMAE", "deep_min_mae", 1), ("deepBias", "deep_min_bias", 1),
            ("remMAE", "rem_min_mae", 1), ("remBias", "rem_min_bias", 1)]
    hdr = f"  {'model':<34}{'':>4} " + " ".join(f"{c[0]:>8}" for c in cols)
    print(hdr)
    for name, m in rows:
        for tag in ("raw", "sm"):
            vals = " ".join(f"{m[tag][k]:>8.{p}f}" for _c, k, p in cols)
            print(f"  {name:<34}{tag:>4} {vals}")
    if rows:
        m = rows[0][1]
        print(f"  (truth: deep {m['raw']['deep_share_true']:.3f} / rem "
              f"{m['raw']['rem_share_true']:.3f} of sleep; wake {m['raw']['wake_share_true']:.3f}"
              f" of epochs; {m['n_nights']} nights, {m['n_epochs']} epochs)")


# --------------------------------------------------------------------------- datasets
def subject_of(night_id: str) -> str:
    return night_id.split("_n")[0]


def regroup(ds: StagingDataset) -> StagingDataset:
    """Group key = subject (the CV split), sequence key (night_ids) stays the night."""
    ds.groups = [subject_of(g) for g in ds.groups]
    return ds


def thin(ds: StagingDataset, stride: int) -> StagingDataset:
    """Every ``stride``-th epoch of each night (consecutive epochs are near-duplicates)."""
    if stride <= 1:
        return ds
    out = StagingDataset()
    seen: Dict[str, int] = {}
    for i in range(len(ds)):
        k = ds.night_ids[i]
        j = seen.get(k, 0)
        seen[k] = j + 1
        if j % stride:
            continue
        out.rows.append(ds.rows[i])
        out.y_wake.append(ds.y_wake[i])
        out.y_stage4.append(ds.y_stage4[i])
        out.groups.append(ds.groups[i])
        out.times.append(ds.times[i])
        out.has_activity.append(ds.has_activity[i])
        out.has_ibi.append(ds.has_ibi[i])
        out.night_ids.append(ds.night_ids[i])
    return out


def load_corpus(data_dir: str, ids: Sequence[str], cache_dir: Optional[str], tag: str,
                jobs: int, motion_ids: Sequence[str], multi_night: bool) -> Dict[str, StagingDataset]:
    t0 = time.time()
    out = {}
    key = ",".join(ids)
    out["hr"] = T.cached_dataset(cache_dir, f"{tag}|hr|{key}", lambda: build_dataset(
        data_dir, ids, use_activity=False, use_ibi=False, jobs=jobs))
    out["sparse"] = T.cached_dataset(cache_dir, f"{tag}|sparse60|{key}", lambda: build_dataset(
        data_dir, ids, use_activity=False, use_ibi=False, hr_decimate_s=60.0,
        night_suffix="#sparse", jobs=jobs))
    if motion_ids:
        mkey = ",".join(motion_ids)
        out["motion"] = T.cached_dataset(cache_dir, f"{tag}|motion|{mkey}", lambda: build_dataset(
            data_dir, motion_ids, use_activity=True, require_activity=True, use_ibi=False,
            jobs=jobs))
    for k in out:
        if multi_night:
            regroup(out[k])
        _log(f"  {tag}/{k}: {len(out[k])} epochs, {len(set(out[k].groups))} subjects, "
             f"{len(set(out[k].night_ids))} nights")
    _log(f"  {tag} built in {time.time() - t0:.0f}s")
    return out


# --------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True, help="BIDSleep reduction (bidsleep_reduce.py)")
    ap.add_argument("--sleep-accel-dir", default=None,
                    help="sleep-accel folder (fetch_sleep_accel.py + reduce_motion_activity.py) "
                         "to also train the BIDSleep + sleep-accel combination")
    ap.add_argument("--out", default=T.WEIGHTS_DIR)
    ap.add_argument("--bundled-dir", default=T.WEIGHTS_DIR,
                    help="the weights to compare against (default: the bundled ones)")
    ap.add_argument("--cache-dir", default=None, help="pickled datasets (keep OUT of the repo)")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2)))
    ap.add_argument("--train-stride", type=int, default=2,
                    help="train on every Nth epoch per night (all epochs are scored)")
    ap.add_argument("--candidates", default="rf_d12,rf_d12_l150,et_d14_l100,rf_d16_l150",
                    help="model grid searched for the HR-only model")
    ap.add_argument("--skip-motion", action="store_true")
    ap.add_argument("--write", action="store_true", help="export weights when they win")
    ap.add_argument("--force", action="store_true", help="export even if they do not win")
    ap.add_argument("--report", default=None, help="where to write the JSON report")
    args = ap.parse_args(argv)
    t0 = time.time()
    _check_fast_path()
    T._label_night = _label_night_fast  # score_emissions / tune_smoothing use the fast twin

    grid_all = dict(T._candidates(False))
    grid_all.update({
        "rf_d12_l150": dict(kind="rf", n_estimators=120, max_depth=12, min_samples_leaf=150),
        "et_d14_l100": dict(kind="et", n_estimators=120, max_depth=14, min_samples_leaf=100),
        "rf_d16_l150": dict(kind="rf", n_estimators=120, max_depth=16, min_samples_leaf=150),
        "rf_d12_l300": dict(kind="rf", n_estimators=120, max_depth=12, min_samples_leaf=300),
    })
    grid = [(c, grid_all[c]) for c in args.candidates.split(",") if c in grid_all]

    # ---- data
    bid_ids = discover_subjects(args.data_dir)
    bid_mo = subjects_with_activity(args.data_dir, bid_ids) if not args.skip_motion else []
    _log(f"BIDSleep: {len(bid_ids)} nights ({len({subject_of(i) for i in bid_ids})} subjects), "
         f"{len(bid_mo)} with actigraphy")
    bid = load_corpus(args.data_dir, bid_ids, args.cache_dir, "bidsleep", args.jobs, bid_mo, True)
    sa = None
    if args.sleep_accel_dir:
        sa_ids = [s for s in SUBJECT_IDS
                  if os.path.exists(os.path.join(args.sleep_accel_dir, f"{s}_labeled_sleep.txt"))]
        sa_mo = subjects_with_activity(args.sleep_accel_dir, sa_ids) if not args.skip_motion else []
        _log(f"sleep-accel: {len(sa_ids)} subjects, {len(sa_mo)} with actigraphy")
        sa = load_corpus(args.sleep_accel_dir, sa_ids, args.cache_dir, "sleepaccel", args.jobs,
                         sa_mo if len(sa_mo) >= args.folds else [], False)

    report: Dict[str, object] = {"attribution": ATTRIBUTION, "folds": args.folds,
                                 "train_stride": args.train_stride,
                                 "bidsleep_nights": len(bid_ids),
                                 "bidsleep_subjects": len({subject_of(i) for i in bid_ids}),
                                 "bidsleep_nights_with_motion": len(bid_mo),
                                 "cv": "subject-grouped K-fold (GroupKFold), whole subjects held out"}
    if sa:
        report["attribution_sleep_accel"] = SLEEP_ACCEL_ATTRIBUTION

    # ---- 1. bundled models on BIDSleep (every night is unseen by them)
    bhmm = json.load(open(os.path.join(args.bundled_dir, "hmm.json")))
    bsm = dict(epochs=int(bhmm.get("smoothing_epochs", 20)), temper=float(bhmm.get("temper", 1.0)),
               prior_mode="uniform" if bhmm.get("emission_prior") == [0.25] * 4 else "empirical",
               wake_mode="balanced")
    _log(f"scoring bundled models (smoothing {bsm}) ...")
    bundled: Dict[str, dict] = {}
    for name, dsk, var in (("hr_dense", "hr", "hr"), ("hr_sparse", "sparse", "hr"),
                           ("hrmotion", "motion", "hrmotion")):
        if dsk not in bid:
            continue
        cache = bundled_cache(bid[dsk], os.path.join(args.bundled_dir, f"wake_{var}.json"),
                              os.path.join(args.bundled_dir, f"stage4_{var}.json"), bhmm)
        bundled[name] = full_metrics(cache, bsm)
        # (no bundled score on sleep-accel: those models were trained on it; their own
        # subject-grouped CV numbers are in cv_report.json)
    report["bundled_on_bidsleep"] = bundled

    # ---- 2. new models, subject-grouped CV
    results: Dict[str, dict] = {}
    chosen: Dict[str, dict] = {}

    def cv(train_ds: StagingDataset, names: Sequence[str], spec: dict,
           tests: Dict[str, StagingDataset]) -> Dict[str, dict]:
        # heads fit on every Nth epoch; the fold HMM still sees every 30 s transition
        return T.cv_emissions_multi(train_ds, names, spec, test_sets=tests,
                                    n_folds=args.folds, fit_stride=args.train_stride)

    _log(f"=== HR-only model selection on BIDSleep dense HR: {[c for c, _ in grid]} ===")
    best = None
    for cname, spec in grid:
        t1 = time.time()
        c = cv(bid["hr"], FEATURE_NAMES_HR, spec, {"self": bid["hr"]})["self"]
        r = T.score_emissions(c)
        _log(f"  {cname:<12} raw k4={r['raw']['kappa4']:.3f} wake k={r['raw']['wake_kappa']:.3f}"
             f" rec4={[round(x, 2) for x in r['raw']['recall4']]} [{time.time() - t1:.0f}s]")
        if best is None or r["raw"]["kappa4"] > best[2]:
            best = (cname, spec, r["raw"]["kappa4"])
    cname, spec, _k = best
    chosen["spec"] = {"name": cname, "spec": spec}
    _log(f"  -> {cname}")

    variants = [("bidsleep", [bid])]
    if sa:
        variants.append(("bidsleep+sleepaccel", [bid, sa]))
    smoothing_by_variant: Dict[str, dict] = {}
    for vname, corpora in variants:
        _log(f"=== [{vname}] HR-only: dense + 1/min copies, CV by subject ===")
        train_ds = concat(*[c[k] for c in corpora for k in ("hr", "sparse")])
        tests = {"bid_dense": bid["hr"], "bid_sparse": bid["sparse"]}
        if sa:
            tests["sa_dense"] = sa["hr"]
        caches = cv(train_ds, FEATURE_NAMES_HR, spec, tests)
        tuned = T.tune_smoothing(caches["bid_dense"])
        sm = dict(temper=tuned["temper"], epochs=tuned["epochs"], prior_mode=tuned["prior_mode"],
                  wake_mode=tuned["wake_mode"])
        smoothing_by_variant[vname] = sm
        results[f"{vname}/hr_dense"] = full_metrics(caches["bid_dense"], sm)
        results[f"{vname}/hr_sparse"] = full_metrics(caches["bid_sparse"], sm)
        if "sa_dense" in caches and caches["sa_dense"]["nights"]:
            results[f"{vname}/hr_dense_on_sleepaccel"] = full_metrics(caches["sa_dense"], sm)
        elif sa:
            # external check: fit on all of BIDSleep, score every sleep-accel night (the corpus
            # the bundled model was trained on; its own CV numbers are in cv_report.json)
            _log(f"  [{vname}] external test on sleep-accel ...")
            ext = external_cache(train_ds, sa["hr"], FEATURE_NAMES_HR, spec, args.train_stride)
            results[f"{vname}/hr_dense_on_sleepaccel_external"] = full_metrics(ext, sm)
        if "motion" in bid:
            for fname, names in (("hrmotion", FEATURE_NAMES_HRMOTION),
                                 ("hrmotion_scalefree", FEATURE_NAMES_HRMOTION_SCALEFREE)):
                mcorp = [c for c in corpora if "motion" in c]
                _log(f"=== [{vname}] {fname} ===")
                mc = cv(concat(*[c["motion"] for c in mcorp]), names, spec,
                        {"bid": bid["motion"]})["bid"]
                t2 = T.tune_smoothing(mc, epochs_grid=(sm["epochs"],), temper_grid=(sm["temper"],),
                                      prior_modes=(sm["prior_mode"],), verbose=False)
                msm = dict(sm, wake_mode=t2["wake_mode"])
                smoothing_by_variant[f"{vname}/{fname}"] = msm
                results[f"{vname}/{fname}"] = full_metrics(mc, msm)
        _log(f"  [{vname}] done ({time.time() - t0:.0f}s)")
    report["new_cv_on_bidsleep"] = results
    report["smoothing"] = smoothing_by_variant
    report["chosen"] = chosen

    # ---- tables
    print_table("HR-only, DENSE HR (~1 sample / 5 s), held-out BIDSleep subjects",
                [("bundled (sleep-accel)", bundled["hr_dense"])]
                + [(k.split("/")[0], v) for k, v in results.items() if k.endswith("/hr_dense")])
    print_table("HR-only, SPARSE HR (1 sample / min), held-out BIDSleep subjects",
                [("bundled (sleep-accel)", bundled["hr_sparse"])]
                + [(k.split("/")[0], v) for k, v in results.items() if k.endswith("/hr_sparse")])
    if "hrmotion" in bundled:
        print_table("HR + motion, held-out BIDSleep subjects",
                    [("bundled (sleep-accel)", bundled["hrmotion"])]
                    + [(k, v) for k, v in results.items() if "/hrmotion" in k])
    sa_rows = [(k, v) for k, v in results.items() if "on_sleepaccel" in k]
    if sa_rows:
        print_table("HR-only on held-out sleep-accel subjects (bundled: see cv_report.json)", sa_rows)

    # ---- 3. pick + gate + export
    def score(m):  # smoothed metrics are what the controller sees
        return m["sm"]["kappa4"]
    pick = max((v for v, _c in variants), key=lambda v: score(results[f"{v}/hr_dense"]))
    new_hr = results[f"{pick}/hr_dense"]
    old_hr = bundled["hr_dense"]
    wins = {
        "kappa_sm": new_hr["sm"]["kappa4"] > old_hr["sm"]["kappa4"],
        "kappa_raw": new_hr["raw"]["kappa4"] > old_hr["raw"]["kappa4"],
        "deep_recall_sm": new_hr["sm"]["deep_rec"] > old_hr["sm"]["deep_rec"],
        "sparse_kappa_sm": results[f"{pick}/hr_sparse"]["sm"]["kappa4"] > bundled["hr_sparse"]["sm"]["kappa4"],
        "no_big_wake_regression": new_hr["sm"]["wake_kappa"] > old_hr["sm"]["wake_kappa"] - 0.05,
        "no_big_rem_regression": new_hr["sm"]["rem_rec"] > old_hr["sm"]["rem_rec"] - 0.10,
    }
    motion_key = None
    if "hrmotion" in bundled:
        cands = [k for k in results if k.startswith(pick + "/hrmotion")]
        if cands:
            motion_key = max(cands, key=lambda k: score(results[k]))
            # Prefer the SCALE-FREE motion block unless the unit-matched counts are clearly
            # better: BIDSleep's watch samples at 33-65 Hz (PIM is a per-epoch SUM over
            # samples) and the live Verity is an upper-arm 52 Hz sensor whose counts arrive
            # per BLE batch, so absolute counts do not transfer; within-night ranks do.
            sf = pick + "/hrmotion_scalefree"
            if sf in results and score(results[sf]) >= score(results[motion_key]) - 0.01:
                motion_key = sf
            wins["motion_kappa_sm"] = results[motion_key]["sm"]["kappa4"] > bundled["hrmotion"]["sm"]["kappa4"]
            wins["motion_deep_recall_sm"] = (results[motion_key]["sm"]["deep_rec"]
                                             > bundled["hrmotion"]["sm"]["deep_rec"])
    report["gate"] = {"picked": pick, "motion_variant": motion_key, "checks": wins,
                      "passed": all(wins.values())}
    _log(f"gate: picked {pick}, motion {motion_key}: {wins}")

    if args.write and (all(wins.values()) or args.force):
        _log("=== final fits on all nights + export ===")
        corpora = dict(variants)[pick]
        sm = smoothing_by_variant[pick]
        full_hr = concat(*[c[k] for c in corpora for k in ("hr", "sparse")])
        sizes: Dict[str, int] = {}

        def fit_export(ds, names, suffix, smx):
            X = np.asarray(ds.matrix(names), dtype=float)
            y4 = np.asarray(ds.y_stage4, dtype=int)
            yw = np.asarray(ds.y_wake, dtype=int)
            natural = smx.get("wake_mode") == "natural"
            wb, wn, st = T._fit_heads(spec, X, y4, yw, both_wake=natural)
            wm = wn if natural else wb
            sizes[f"wake_{suffix}.json"] = T.write_json(os.path.join(args.out, f"wake_{suffix}.json"),
                                                        T.export_forest(wm, names))
            sizes[f"stage4_{suffix}.json"] = T.write_json(
                os.path.join(args.out, f"stage4_{suffix}.json"), T.export_forest(st, names))

        fit_export(thin(full_hr, args.train_stride), FEATURE_NAMES_HR, "hr", sm)
        if motion_key:
            names = (FEATURE_NAMES_HRMOTION_SCALEFREE if motion_key.endswith("scalefree")
                     else FEATURE_NAMES_HRMOTION)
            full_mo = concat(*[c["motion"] for c in corpora if "motion" in c])
            fit_export(thin(full_mo, args.train_stride), names, "hrmotion",
                       smoothing_by_variant[motion_key])
        dense = concat(*[c["hr"] for c in corpora])
        hmm = T.estimate_hmm(np.asarray(dense.y_stage4), dense.night_ids, dense.times)
        hmm["temper"] = float(sm["temper"])
        hmm["smoothing_epochs"] = int(sm["epochs"])
        hmm["emission_prior"] = [0.25] * 4 if sm["prior_mode"] == "uniform" else hmm["prior"]
        hmm["note"] = (f"estimated per night on {pick} (BIDSleep v1.0.1, ODC-By 1.0, "
                       "doi:10.13026/rees-1092); emission_prior is the prior the class-balanced "
                       "heads were trained under; temper<1 compensates for the overlap between "
                       "consecutive 30 s emissions")
        sizes["hmm.json"] = T.write_json(os.path.join(args.out, "hmm.json"), hmm)
        report["exported"] = {"sizes_bytes": sizes, "trained_on": pick,
                              "hmm_trans": hmm["trans"], "hmm_prior": hmm["prior"]}
        for k, v in sorted(sizes.items()):
            print(f"  {k:<24} {v / 1024:9.1f} KB")
    else:
        report["exported"] = None

    path = args.report or os.path.normpath(os.path.join(args.out, "..", "cv_report_bidsleep.json"))
    with open(path, "w") as fh:
        json.dump(_round(report), fh, indent=1)
    _log(f"report -> {path} ({time.time() - t0:.0f}s total)")
    return 0


def _round(o):
    if isinstance(o, (bool, np.bool_)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        o = float(o)
        return round(o, 4) if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _round(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_round(v) for v in o]
    return o


if __name__ == "__main__":
    sys.exit(main())
