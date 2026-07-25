"""Train the v2 wearable sleep-staging models and export compact JSON weights.

Pipeline
--------
1. Build epoch rows from the PhysioNet sleep-accel recordings (:mod:`dataset`).
2. Grouped leave-subjects-out CV (5 folds, split by subject) over a small grid of tree
   ensembles (RandomForest / ExtraTrees / GradientBoosting); pick the best by 4-class kappa.
3. Evaluate the *shipped* pipeline under the same CV — 4-class head + binary wake head
   blended into an emission, then the online HMM forward filter from :mod:`infer` — and
   report epoch-level metrics with and without smoothing, plus controller-relevant
   per-night errors (deep minutes, sleep onset, TST, WASO, stage flip rate).
4. Refit on all subjects and export compact JSON to ``weights/``:
   ``wake_hr``/``stage4_hr``, ``wake_hrmotion``/``stage4_hrmotion``,
   and ``hmm.json``. The HR model trains on dense AND 1-sample/min copies of every night,
   so one model covers both streaming rates.

Run::

    python -m sleepctl.ml.sleep_staging.train --data-dir <scratchpad>/sleep_accel

numpy/sklearn are used here only; :mod:`features` and :mod:`infer` stay pure-stdlib.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pickle
import time
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from sklearn.ensemble import (
    ExtraTreesClassifier,
    GradientBoostingClassifier,
    RandomForestClassifier,
)
from sklearn.model_selection import GroupKFold

from .dataset import (
    DEFAULT_DATA_DIR,
    EPOCH_S,
    SUBJECT_IDS,
    StagingDataset,
    build_dataset,
    concat,
    subjects_with_activity,
)
from .features import (
    FEATURE_NAMES_HR,
    FEATURE_NAMES_HRMOTION,
    FEATURE_NAMES_HRMOTION_SCALEFREE,
)
from .infer import DEFAULT_SMOOTHING_EPOCHS, blend_emission, forward_filter

WEIGHTS_DIR = os.path.join(os.path.dirname(__file__), "weights")
#: feature-set fingerprint: a cached dataset built against different features is unusable
FEATURE_TAG = "{}_{}".format(
    len(FEATURE_NAMES_HRMOTION),
    hashlib.md5("|".join(FEATURE_NAMES_HRMOTION).encode()).hexdigest()[:10],
)
STAGE4_LABELS = ["wake", "light", "deep", "rem"]
N_FOLDS = 5
ONSET_EPOCHS = 20  # 10 min of sustained sleep defines onset


# --------------------------------------------------------------------------- model grid
def _candidates(quick: bool = False) -> List[Tuple[str, dict]]:
    grid: List[Tuple[str, dict]] = [
        ("rf_d8", dict(kind="rf", n_estimators=150, max_depth=8, min_samples_leaf=20)),
        ("rf_d12", dict(kind="rf", n_estimators=120, max_depth=12, min_samples_leaf=40)),
        ("et_d10", dict(kind="et", n_estimators=150, max_depth=10, min_samples_leaf=20)),
        ("et_d14", dict(kind="et", n_estimators=120, max_depth=14, min_samples_leaf=40)),
        ("et_d10n", dict(kind="et", n_estimators=150, max_depth=10, min_samples_leaf=20,
                         balanced=False)),
        ("rf_d8n", dict(kind="rf", n_estimators=150, max_depth=8, min_samples_leaf=20,
                        balanced=False)),
        # single-threaded and ~10x slower than the forests; kept as an honest benchmark
        # only (its additive score structure has no pure-stdlib runtime exporter)
        ("gb", dict(kind="gb", n_estimators=60, max_depth=3, learning_rate=0.15,
                    min_samples_leaf=20, subsample=0.6)),
    ]
    if quick:
        grid = grid[:1] + grid[2:3]
    return grid


def _make(spec: dict, balanced: bool, seed: int = 0):
    kind = spec["kind"]
    cw = "balanced" if balanced else None
    if kind == "rf":
        return RandomForestClassifier(
            n_estimators=spec["n_estimators"], max_depth=spec["max_depth"],
            min_samples_leaf=spec["min_samples_leaf"], class_weight=cw,
            n_jobs=-1, random_state=seed)
    if kind == "et":
        return ExtraTreesClassifier(
            n_estimators=spec["n_estimators"], max_depth=spec["max_depth"],
            min_samples_leaf=spec["min_samples_leaf"], class_weight=cw,
            n_jobs=-1, random_state=seed)
    if kind == "gb":
        return GradientBoostingClassifier(
            n_estimators=spec["n_estimators"], max_depth=spec["max_depth"],
            learning_rate=spec["learning_rate"], subsample=spec.get("subsample", 1.0),
            min_samples_leaf=spec["min_samples_leaf"], random_state=seed)
    raise ValueError(kind)


def _exportable(spec: dict) -> bool:
    """Only averaged-probability forests match the pure-stdlib runtime format."""
    return spec["kind"] in ("rf", "et")


# --------------------------------------------------------------------------- metrics
def cohen_kappa(y_true: Sequence[int], y_pred: Sequence[int], n_classes: int) -> float:
    n = len(y_true)
    if n == 0:
        return 0.0
    cm = np.zeros((n_classes, n_classes), dtype=float)
    for a, b in zip(y_true, y_pred):
        cm[int(a), int(b)] += 1.0
    po = np.trace(cm) / n
    pe = float((cm.sum(axis=0) * cm.sum(axis=1)).sum()) / (n * n)
    return 0.0 if pe >= 1.0 else (po - pe) / (1.0 - pe)


def binary_prf(y_true: Sequence[int], y_pred: Sequence[int], positive: int = 0):
    tp = sum(1 for a, b in zip(y_true, y_pred) if a == positive and b == positive)
    fp = sum(1 for a, b in zip(y_true, y_pred) if a != positive and b == positive)
    fn = sum(1 for a, b in zip(y_true, y_pred) if a == positive and b != positive)
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return prec, rec, f1


def per_class_recall(y_true, y_pred, n_classes: int) -> List[float]:
    out = []
    for c in range(n_classes):
        tot = sum(1 for a in y_true if a == c)
        hit = sum(1 for a, b in zip(y_true, y_pred) if a == c and b == c)
        out.append(hit / tot if tot else float("nan"))
    return out


def _onset_index(is_sleep: Sequence[int], run: int = ONSET_EPOCHS) -> Optional[int]:
    streak = 0
    for i, s in enumerate(is_sleep):
        streak = streak + 1 if s else 0
        if streak >= run:
            return i - run + 1
    return None


def night_summary(labels4: Sequence[int], times_s: Sequence[float]) -> Dict[str, float]:
    """Controller-relevant summary of one night's stage sequence."""
    ep_min = EPOCH_S / 60.0
    is_sleep = [1 if l != 0 else 0 for l in labels4]
    deep_min = sum(1 for l in labels4 if l == 2) * ep_min
    tst_min = sum(is_sleep) * ep_min
    oi = _onset_index(is_sleep)
    onset_min = (times_s[oi] / 60.0) if oi is not None else float("nan")
    waso_min = (
        sum(1 for l in labels4[oi:] if l == 0) * ep_min if oi is not None else float("nan")
    )
    changes = sum(1 for i in range(1, len(labels4)) if labels4[i] != labels4[i - 1])
    hours = max(1e-6, len(labels4) * ep_min / 60.0)
    return dict(deep_min=deep_min, tst_min=tst_min, onset_min=onset_min,
                waso_min=waso_min, flips_per_h=changes / hours)


def _nanmean(vals: Sequence[float]) -> float:
    v = [x for x in vals if not math.isnan(x)]
    return sum(v) / len(v) if v else float("nan")


# --------------------------------------------------------------------------- CV pipeline
def _fit_heads(spec, Xtr, y4tr, ywtr, seed=0, both_wake: bool = False):
    """Fit the 4-class head and the dedicated binary wake head.

    ``both_wake`` also fits a natural-prior wake head so CV can choose between the balanced
    one (high recall, low precision) and the natural one (the reverse).
    """
    stage = _make(spec, balanced=spec.get("balanced", True), seed=seed)
    stage.fit(Xtr, y4tr)
    wake_bal = _make(spec, balanced=True, seed=seed)
    wake_bal.fit(Xtr, ywtr)
    wake_nat = None
    if both_wake:
        wake_nat = _make(spec, balanced=False, seed=seed)
        wake_nat.fit(Xtr, ywtr)
    return wake_bal, wake_nat, stage


def _ordered4(proba: np.ndarray, classes: Sequence[int]) -> np.ndarray:
    out = np.zeros((proba.shape[0], 4))
    for j, c in enumerate(classes):
        out[:, int(c)] = proba[:, j]
    s = out.sum(axis=1, keepdims=True)
    s[s <= 0] = 1.0
    return out / s


def _label_night(emissions, pw_raw, hmm, smoothing_epochs: int, temper: float,
                 prior_mode: str, smooth: bool):
    """Label one night from its emission run, exactly as :meth:`infer.SleepStager.predict`."""
    trans = hmm["trans"]
    start = hmm["start"]
    prior = [0.25] * 4 if prior_mode == "uniform" else hmm["prior"]
    out = []
    for k in range(len(emissions)):
        if smooth:
            lo = max(0, k - smoothing_epochs + 1)
            post = forward_filter(emissions[lo:k + 1], trans, start, prior, temper)
            p_wake = post[0]
        else:
            post = emissions[k]
            p_wake = float(pw_raw[k])
        lbl = int(np.argmax(post))
        if p_wake >= 0.5:
            lbl = 0
        out.append(lbl)
    return out


def estimate_hmm(y4: Sequence[int], groups: Sequence[str], times: Sequence[float]) -> dict:
    """4x4 transition matrix at 30 s epochs, plus start and prior distributions."""
    n = 4
    trans = np.ones((n, n)) * 0.5  # light Laplace smoothing
    prior = np.ones(n) * 1.0
    start = np.ones(n) * 1.0
    by_subj: Dict[str, List[int]] = {}
    for i, g in enumerate(groups):
        by_subj.setdefault(g, []).append(i)
    for _g, idx in by_subj.items():
        idx = sorted(idx, key=lambda i: times[i])
        start[y4[idx[0]]] += 1.0
        for k in range(len(idx)):
            prior[y4[idx[k]]] += 1.0
            if k == 0:
                continue
            if abs(times[idx[k]] - times[idx[k - 1]] - EPOCH_S) < 1e-6:
                trans[y4[idx[k - 1]], y4[idx[k]]] += 1.0
    trans = trans / trans.sum(axis=1, keepdims=True)
    return dict(
        classes=STAGE4_LABELS,
        epoch_s=EPOCH_S,
        trans=[[round(float(x), 6) for x in row] for row in trans],
        start=[round(float(x), 6) for x in (start / start.sum())],
        prior=[round(float(x), 6) for x in (prior / prior.sum())],
        smoothing_epochs=DEFAULT_SMOOTHING_EPOCHS,
    )


def _subject_index(night_ids: Sequence[str], times: Sequence[float], rows: Sequence[int]):
    """Group held-out rows into contiguous nights, ordered in time."""
    by: Dict[str, List[int]] = {}
    for i in rows:
        by.setdefault(night_ids[i], []).append(i)
    return [(sid, sorted(idx, key=lambda i: times[i])) for sid, idx in sorted(by.items())]


def cv_emissions_multi(ds: StagingDataset, feature_names: Sequence[str], spec: dict,
                       *, test_sets: Optional[Dict[str, StagingDataset]] = None,
                       n_folds: int = N_FOLDS, seed: int = 0) -> Dict[str, dict]:
    """Fit each grouped CV fold once and cache the held-out predictions per test variant.

    One (expensive) round of fits can then score e.g. dense-HR *and* decimated-HR test rows.
    Each cache is ``{"nights": [{subject, y_true, times, p4, pw_balanced, pw_natural, hmm}]}``
    so smoothing hyper-parameters can be re-scored without refitting.
    """
    sets = dict(test_sets or {"self": ds})
    X = np.asarray(ds.matrix(feature_names), dtype=float)
    y4 = np.asarray(ds.y_stage4, dtype=int)
    yw = np.asarray(ds.y_wake, dtype=int)
    groups = list(ds.groups)
    times = list(ds.times)
    n_folds = min(n_folds, len(set(groups)))

    prepared = {}
    for name, tds in sets.items():
        t = tds if tds is not None else ds
        prepared[name] = dict(
            X=np.asarray(t.matrix(feature_names), dtype=float),
            y4=np.asarray(t.y_stage4, dtype=int),
            groups=list(t.groups),
            nights=list(t.night_ids or t.groups),
            times=list(t.times),
        )
    out = {name: dict(nights=[]) for name in sets}

    gkf = GroupKFold(n_splits=n_folds)
    for tr_idx, te_idx in gkf.split(X, y4, groups=groups):
        hmm = estimate_hmm(y4[tr_idx], [groups[i] for i in tr_idx],
                           [times[i] for i in tr_idx])
        wake_bal, wake_nat, stage_m = _fit_heads(spec, X[tr_idx], y4[tr_idx], yw[tr_idx],
                                                 seed=seed, both_wake=True)
        test_subjects = {groups[i] for i in te_idx}
        for name, P in prepared.items():
            te_rows = [i for i, g in enumerate(P["groups"]) if g in test_subjects]
            if not te_rows:
                continue
            Xte = P["X"][te_rows]
            p4 = _ordered4(stage_m.predict_proba(Xte), list(stage_m.classes_))

            def _pw(model):
                cls = list(model.classes_)
                return model.predict_proba(Xte)[:, cls.index(0) if 0 in cls else 0]

            pw_bal = _pw(wake_bal)
            pw_nat = _pw(wake_nat)
            pos = {row: j for j, row in enumerate(te_rows)}
            for sid, idx in _subject_index(P["nights"], P["times"], te_rows):
                js = [pos[i] for i in idx]
                out[name]["nights"].append(dict(
                    subject=sid,
                    y_true=[int(P["y4"][i]) for i in idx],
                    times=[P["times"][i] for i in idx],
                    p4=[list(map(float, p4[j])) for j in js],
                    pw_balanced=[float(pw_bal[j]) for j in js],
                    pw_natural=[float(pw_nat[j]) for j in js],
                    hmm=hmm,
                ))
    hmm_full = estimate_hmm(y4, groups, times)
    for name in out:
        out[name]["hmm_full"] = hmm_full
        out[name]["n_rows"] = sum(len(n["y_true"]) for n in out[name]["nights"])
    return out


def cv_emissions(ds: StagingDataset, feature_names: Sequence[str], spec: dict,
                 *, test_ds: Optional[StagingDataset] = None,
                 n_folds: int = N_FOLDS, seed: int = 0) -> dict:
    """Single-test-set convenience wrapper around :func:`cv_emissions_multi`."""
    return cv_emissions_multi(ds, feature_names, spec,
                              test_sets={"self": test_ds if test_ds is not None else ds},
                              n_folds=n_folds, seed=seed)["self"]


def score_emissions(cache: dict, *, smoothing_epochs: int = DEFAULT_SMOOTHING_EPOCHS,
                    temper: float = 1.0, prior_mode: str = "uniform",
                    wake_mode: str = "balanced") -> dict:
    """Score cached fold predictions with and without the HMM forward filter."""
    agg: Dict[str, List[int]] = {k: [] for k in ("y_true", "raw", "sm")}
    per_night: List[dict] = []
    for night in cache["nights"]:
        pw = night["pw_balanced"] if wake_mode == "balanced" else night["pw_natural"]
        em = [blend_emission(p, w) for p, w in zip(night["p4"], pw)]
        hmm = night["hmm"]
        lab_raw = _label_night(em, pw, hmm, smoothing_epochs, temper, prior_mode, False)
        lab_sm = _label_night(em, pw, hmm, smoothing_epochs, temper, prior_mode, True)
        per_night.append(dict(
            subject=night["subject"], n=len(em),
            true=night_summary(night["y_true"], night["times"]),
            pred=night_summary(lab_sm, night["times"]),
            raw=night_summary(lab_raw, night["times"])))
        agg["y_true"].extend(night["y_true"])
        agg["raw"].extend(lab_raw)
        agg["sm"].extend(lab_sm)

    out: Dict[str, object] = dict(n_rows=len(agg["y_true"]), per_night=per_night,
                                  smoothing=dict(temper=temper, prior_mode=prior_mode,
                                                 epochs=smoothing_epochs,
                                                 wake_mode=wake_mode))
    for tag in ("raw", "sm"):
        yt = agg["y_true"]
        yp = agg[tag]
        wt = [0 if v == 0 else 1 for v in yt]
        wp = [0 if v == 0 else 1 for v in yp]
        prec, rec, f1 = binary_prf(wt, wp, positive=0)
        out[tag] = dict(
            acc4=sum(1 for a, b in zip(yt, yp) if a == b) / max(1, len(yt)),
            kappa4=cohen_kappa(yt, yp, 4),
            recall4=per_class_recall(yt, yp, 4),
            wake_acc=sum(1 for a, b in zip(wt, wp) if a == b) / max(1, len(wt)),
            wake_kappa=cohen_kappa(wt, wp, 2),
            wake_prec=prec, wake_rec=rec, wake_f1=f1,
        )
    # controller-relevant per-night errors (averaged over held-out subject-nights)
    for tag, key in (("raw", "raw"), ("sm", "pred")):
        out[tag]["night"] = dict(
            deep_mae=_nanmean([abs(p[key]["deep_min"] - p["true"]["deep_min"]) for p in per_night]),
            deep_bias=_nanmean([p[key]["deep_min"] - p["true"]["deep_min"] for p in per_night]),
            onset_mae=_nanmean([abs(p[key]["onset_min"] - p["true"]["onset_min"]) for p in per_night]),
            tst_mae=_nanmean([abs(p[key]["tst_min"] - p["true"]["tst_min"]) for p in per_night]),
            waso_mae=_nanmean([abs(p[key]["waso_min"] - p["true"]["waso_min"]) for p in per_night]),
            flips_pred=_nanmean([p[key]["flips_per_h"] for p in per_night]),
            flips_true=_nanmean([p["true"]["flips_per_h"] for p in per_night]),
        )
    return out


def tune_smoothing(cache: dict, *, epochs_grid=(20,),
                   temper_grid=(0.2, 0.35),
                   prior_modes=("uniform", "empirical"),
                   wake_modes=("balanced", "natural"), verbose: bool = True) -> dict:
    """Pick the wake head + forward-filter hyper-parameters by the same grouped CV.

    The objective is the mean of the *smoothed* 4-class kappa and the smoothed wake kappa.
    Optimizing 4-class kappa alone reliably picks a very sticky filter that wins on stage
    structure while collapsing wake recall — but the controller needs wake for sleep onset
    and WASO, so both are weighted equally.
    """
    trials = []
    for wm in wake_modes:
        for pm in prior_modes:
            for ne in epochs_grid:
                for tp in temper_grid:
                    res = score_emissions(cache, smoothing_epochs=ne, temper=tp,
                                          prior_mode=pm, wake_mode=wm)
                    k4 = res["sm"]["kappa4"]
                    kw = res["sm"]["wake_kappa"]
                    trials.append(dict(kappa=k4, wake_kappa=kw, score=0.5 * (k4 + kw),
                                       temper=tp, epochs=ne, prior_mode=pm, wake_mode=wm,
                                       res=res, deep_mae=res["sm"]["night"]["deep_mae"]))
    top = max(t["score"] for t in trials)
    # tie-break on total deep-sleep-minutes error: the controller steers on realized deep,
    # so among statistically indistinguishable settings prefer the least deep-biased one
    best = min((t for t in trials if t["score"] >= top - 0.01),
               key=lambda t: t["deep_mae"])
    if verbose:
        print(f"  tuned: wake_head={best['wake_mode']} temper={best['temper']} "
              f"epochs={best['epochs']} prior={best['prior_mode']} -> smoothed "
              f"4-class k={best['kappa']:.3f} wake k={best['wake_kappa']:.3f}", flush=True)
    return best


def cv_evaluate(ds: StagingDataset, feature_names: Sequence[str], spec: dict,
                *, smoothing: Optional[dict] = None,
                test_ds: Optional[StagingDataset] = None,
                n_folds: int = N_FOLDS, seed: int = 0) -> dict:
    """Convenience wrapper: fit CV folds and score with the given smoothing settings."""
    cache = cv_emissions(ds, feature_names, spec, test_ds=test_ds, n_folds=n_folds,
                         seed=seed)
    s = smoothing or {}
    res = score_emissions(cache, smoothing_epochs=s.get("epochs", DEFAULT_SMOOTHING_EPOCHS),
                          temper=s.get("temper", 1.0),
                          prior_mode=s.get("prior_mode", "uniform"),
                          wake_mode=s.get("wake_mode", "balanced"))
    res["hmm"] = cache["hmm_full"]
    return res


# --------------------------------------------------------------------------- export
def export_forest(model, feature_names: Sequence[str], thresh_round: int = 4,
                  prob_round: int = 4) -> dict:
    """Serialize an RF/ET into flat per-tree arrays (see infer._Forest)."""
    classes = [int(c) for c in model.classes_]
    n_classes = len(classes)
    trees = []
    for est in model.estimators_:
        t = est.tree_
        f: List[int] = []
        th: List[float] = []
        left: List[int] = []
        right: List[int] = []
        vals: List[float] = []
        n_leaves = 0
        for i in range(t.node_count):
            cl = int(t.children_left[i])
            if cl == -1:  # leaf
                v = np.asarray(t.value[i]).ravel().astype(float)
                s = v.sum()
                p = (v / s) if s > 0 else np.ones(n_classes) / n_classes
                f.append(-1)
                th.append(0.0)
                left.append(n_leaves)
                right.append(-1)
                vals.extend(round(float(x), prob_round) for x in p)
                n_leaves += 1
            else:
                f.append(int(t.feature[i]))
                th.append(round(float(t.threshold[i]), thresh_round))
                left.append(cl)
                right.append(int(t.children_right[i]))
        trees.append(dict(f=f, t=th, l=left, r=right, v=vals))
    return dict(
        type="forest",
        kind=type(model).__name__,
        feature_names=list(feature_names),
        classes=classes,
        n_trees=len(trees),
        trees=trees,
    )


def write_json(path: str, obj: dict) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh, separators=(",", ":"))
    return os.path.getsize(path)


def cached_dataset(cache_dir: Optional[str], key: str, build) -> StagingDataset:
    """Build a dataset once and reuse it across runs (raw data + cache stay off-repo)."""
    if not cache_dir:
        return build()
    os.makedirs(cache_dir, exist_ok=True)
    tag = hashlib.md5((key + "|" + FEATURE_TAG).encode()).hexdigest()[:16]
    path = os.path.join(cache_dir, f"ds_{tag}.pkl")
    if os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                ds = pickle.load(fh)
            print(f"  (loaded {len(ds)} epochs from cache)", flush=True)
            return ds
        except Exception:  # noqa: BLE001 — a corrupt cache must never be fatal
            pass
    ds = build()
    try:
        with open(path, "wb") as fh:
            pickle.dump(ds, fh, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception:  # noqa: BLE001
        pass
    return ds


def _sm(smoothing: dict) -> dict:
    """Smoothing settings dict -> :func:`score_emissions` keyword arguments."""
    return dict(smoothing_epochs=smoothing["epochs"], temper=smoothing["temper"],
                prior_mode=smoothing["prior_mode"], wake_mode=smoothing["wake_mode"])


# --------------------------------------------------------------------------- reporting
def _fmt_block(name: str, m: dict) -> str:
    r = m["recall4"]
    n = m["night"]
    return (
        f"  {name:<10} wake: acc={m['wake_acc']:.3f} k={m['wake_kappa']:.3f} "
        f"P={m['wake_prec']:.3f} R={m['wake_rec']:.3f} F1={m['wake_f1']:.3f} | "
        f"4-class: acc={m['acc4']:.3f} k={m['kappa4']:.3f} "
        f"rec[w/l/d/r]={r[0]:.2f}/{r[1]:.2f}/{r[2]:.2f}/{r[3]:.2f}\n"
        f"             night MAE: deep={n['deep_mae']:.1f}m (bias {n['deep_bias']:+.1f}) "
        f"onset={n['onset_mae']:.1f}m TST={n['tst_mae']:.1f}m WASO={n['waso_mae']:.1f}m | "
        f"flips/h pred={n['flips_pred']:.1f} true={n['flips_true']:.1f}"
    )


def report(title: str, res: dict) -> None:
    print(f"\n{title}  (n={res['n_rows']} epochs)")
    print(_fmt_block("unsmoothed", res["raw"]))
    print(_fmt_block("HMM", res["sm"]))


# --------------------------------------------------------------------------- main
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Train v2 wearable sleep-staging models")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--out", default=WEIGHTS_DIR)
    ap.add_argument("--folds", type=int, default=N_FOLDS)
    ap.add_argument("--quick", action="store_true", help="tiny model grid (smoke test)")
    ap.add_argument("--subjects", nargs="*", default=None)
    ap.add_argument("--skip-sparse", action="store_true")
    ap.add_argument("--cache-dir", default=None,
                    help="directory for pickled datasets (keep OUT of the repo)")
    ap.add_argument("--candidates", default=None,
                    help="comma-separated subset of the model grid to search")
    ap.add_argument("--hr-spec", default=None,
                    help="skip HR-only model selection and use this candidate directly")
    ap.add_argument("--no-motion", action="store_true",
                    help="skip the HR+motion variant (e.g. actigraphy still reducing)")
    args = ap.parse_args(argv)

    t0 = time.time()
    subs = list(args.subjects or SUBJECT_IDS)
    with_act = subjects_with_activity(args.data_dir, subs)
    print(f"subjects: {len(subs)} total, {len(with_act)} with reduced actigraphy")

    cache_dir = args.cache_dir
    print("building HR-only dataset ...", flush=True)
    ds_hr = cached_dataset(
        cache_dir, f"hr|{','.join(subs)}",
        lambda: build_dataset(args.data_dir, subs, use_activity=False))
    print(f"  {len(ds_hr)} epochs from {len(set(ds_hr.groups))} subjects "
          f"({time.time() - t0:.0f}s)")

    ds_sparse = None
    ds_union = ds_hr
    if not args.skip_sparse:
        print("building decimated (1 sample/min) HR dataset ...", flush=True)
        ds_sparse = cached_dataset(
            cache_dir, f"sparse60|{','.join(subs)}",
            lambda: build_dataset(args.data_dir, subs, use_activity=False,
                                  hr_decimate_s=60.0, night_suffix="#sparse"))
        print(f"  {len(ds_sparse)} epochs ({time.time() - t0:.0f}s)")
        # The shipped HR model trains on dense AND decimated copies of every night, so it
        # works whether the controller streams ~1 sample/2 s or ~1 sample/min.
        ds_union = concat(ds_hr, ds_sparse)

    ds_motion = None
    if len(with_act) >= args.folds and not args.no_motion:
        print("building HR+motion dataset ...", flush=True)
        ds_motion = cached_dataset(
            cache_dir, f"motion|{','.join(with_act)}",
            lambda: build_dataset(args.data_dir, with_act, use_activity=True,
                                  require_activity=True))
        print(f"  {len(ds_motion)} epochs from {len(set(ds_motion.groups))} subjects "
              f"({time.time() - t0:.0f}s)")

    results: Dict[str, dict] = {}
    chosen: Dict[str, Tuple[str, dict]] = {}

    def _grid() -> List[Tuple[str, dict]]:
        g = _candidates(args.quick)
        if args.candidates:
            want = {c.strip() for c in args.candidates.split(",")}
            g = [(c, s) for c, s in g if c in want]
        return g

    def select(tag: str, ds: StagingDataset, names: Sequence[str],
               fixed: Optional[dict] = None, grid: Optional[List] = None):
        """Pick the ensemble by unsmoothed 4-class kappa, then tune the forward filter."""
        print(f"\n=== model selection [{tag}] ===", flush=True)
        best = None
        for cname, spec in (grid or _grid()):
            t1 = time.time()
            cache = cv_emissions(ds, names, spec, n_folds=args.folds)
            res = score_emissions(cache)
            k = res["raw"]["kappa4"]
            flag = "" if _exportable(spec) else "  (NOT exportable to the stdlib runtime)"
            print(f"  {cname:<8} 4-class k={k:.3f} wake k={res['raw']['wake_kappa']:.3f} "
                  f"acc4={res['raw']['acc4']:.3f}  [{time.time() - t1:.0f}s]{flag}",
                  flush=True)
            if _exportable(spec) and (best is None or k > best[3]["raw"]["kappa4"]):
                best = (cname, spec, cache, res)
        if best is None:
            raise RuntimeError("no exportable candidate produced a CV score")
        cname, spec, cache, _res = best
        if fixed:  # reuse the production (HR-only) filter settings; only the wake head varies
            tuned = tune_smoothing(cache, epochs_grid=(fixed["epochs"],),
                                   temper_grid=(fixed["temper"],),
                                   prior_modes=(fixed["prior_mode"],))
        else:
            tuned = tune_smoothing(cache)
        res = tuned["res"]
        res["hmm"] = cache["hmm_full"]
        smoothing = dict(temper=tuned["temper"], epochs=tuned["epochs"],
                         prior_mode=tuned["prior_mode"], wake_mode=tuned["wake_mode"])
        return cname, spec, res, smoothing

    # ---- HR-only (production-critical: the live controller runs this path) ----
    # The shipped model trains on dense AND 1-sample/min copies of every night; a single
    # round of CV fits is scored against both test rates.
    if args.hr_spec:
        grid = [(c, sp) for c, sp in _candidates(False) if c == args.hr_spec]
        if not grid:
            raise SystemExit(f"unknown --hr-spec {args.hr_spec}")
        cname, spec = grid[0]
        print(f"\n=== HR-only: using pre-selected model {cname} ===", flush=True)
    else:
        cname, spec, res_sel, _sm0 = select("HR-only", ds_hr, FEATURE_NAMES_HR)
        results["hr_selection"] = res_sel
    chosen["hr"] = (cname, spec)

    test_sets = {"dense": ds_hr}
    if ds_sparse is not None:
        test_sets["sparse"] = ds_sparse
    caches = cv_emissions_multi(ds_union, FEATURE_NAMES_HR, spec,
                                test_sets=test_sets, n_folds=args.folds)
    tuned = tune_smoothing(caches["dense"])
    smooth_hr = dict(temper=tuned["temper"], epochs=tuned["epochs"],
                     prior_mode=tuned["prior_mode"], wake_mode=tuned["wake_mode"])
    res = tuned["res"]
    res["hmm"] = caches["dense"]["hmm_full"]
    results["hr"] = res
    report(f"HR-only CV [{cname}] tested on DENSE HR  smoothing={smooth_hr}", res)
    if "sparse" in caches:
        res_sp = score_emissions(caches["sparse"], **_sm(smooth_hr))
        results["hr_sparseTest"] = res_sp
        report("HR-only CV [same model] tested on SPARSE HR (1 sample/min)", res_sp)

    # ---- HR+motion ----
    smooth_motion = smooth_hr
    motion_names = FEATURE_NAMES_HRMOTION
    if ds_motion is not None:
        print(f"\n=== HR+motion: model {cname} (reusing the HR-only selection) ===",
              flush=True)
        cache_m = cv_emissions(ds_motion, FEATURE_NAMES_HRMOTION, spec,
                               n_folds=args.folds)
        tuned_m = tune_smoothing(cache_m, epochs_grid=(smooth_hr["epochs"],),
                                 temper_grid=(smooth_hr["temper"],),
                                 prior_modes=(smooth_hr["prior_mode"],))
        smooth_motion = dict(temper=tuned_m["temper"], epochs=tuned_m["epochs"],
                             prior_mode=tuned_m["prior_mode"],
                             wake_mode=tuned_m["wake_mode"])
        res_m = tuned_m["res"]
        res_m["hmm"] = cache_m["hmm_full"]
        chosen["hrmotion"] = (cname, spec)
        results["hrmotion"] = res_m
        report(f"HR+motion CV, unit-matched counts + scale-free [{cname}]", res_m)

        # transfer-safe alternative: nothing depends on the counts' units
        res_sf = cv_evaluate(ds_motion, FEATURE_NAMES_HRMOTION_SCALEFREE, spec,
                             smoothing=smooth_motion, n_folds=args.folds)
        results["hrmotion_scalefree"] = res_sf
        report(f"HR+motion CV, SCALE-FREE features only [{cname}]", res_sf)
        if res_sf["sm"]["kappa4"] > res_m["sm"]["kappa4"] + 0.005:
            print("  -> scale-free-only wins on CV; shipping it as the HR+motion model",
                  flush=True)
            motion_names = FEATURE_NAMES_HRMOTION_SCALEFREE

    # ---- final fits on all subjects + export ----
    print("\n=== final fits + export ===", flush=True)
    os.makedirs(args.out, exist_ok=True)
    sizes: Dict[str, int] = {}

    def fit_and_export(ds: StagingDataset, names: Sequence[str], spec: dict, suffix: str,
                       smoothing: dict):
        X = np.asarray(ds.matrix(names), dtype=float)
        y4 = np.asarray(ds.y_stage4, dtype=int)
        yw = np.asarray(ds.y_wake, dtype=int)
        natural = smoothing.get("wake_mode") == "natural"
        wake_bal, wake_nat, stage_m = _fit_heads(spec, X, y4, yw, both_wake=natural)
        wake_m = wake_nat if natural else wake_bal
        sizes[f"wake_{suffix}.json"] = write_json(
            os.path.join(args.out, f"wake_{suffix}.json"), export_forest(wake_m, names))
        sizes[f"stage4_{suffix}.json"] = write_json(
            os.path.join(args.out, f"stage4_{suffix}.json"), export_forest(stage_m, names))

    fit_and_export(ds_union, FEATURE_NAMES_HR, chosen["hr"][1], "hr", smooth_hr)
    if ds_motion is not None:
        fit_and_export(ds_motion, motion_names, chosen["hrmotion"][1],
                       "hrmotion", smooth_motion)
    # no separate sparse-only export: the shipped HR model already trains on both rates

    hmm = estimate_hmm(np.asarray(ds_hr.y_stage4), ds_hr.groups, ds_hr.times)
    # smoothing hyper-parameters chosen by the same grouped CV (see tune_smoothing)
    hmm["temper"] = float(smooth_hr["temper"])
    hmm["smoothing_epochs"] = int(smooth_hr["epochs"])
    hmm["emission_prior"] = (
        [0.25] * 4 if smooth_hr["prior_mode"] == "uniform" else hmm["prior"]
    )
    hmm["note"] = (
        "emission_prior is the prior the class-balanced heads were trained under; "
        "temper<1 compensates for the strong overlap between consecutive 30 s emissions"
    )
    sizes["hmm.json"] = write_json(os.path.join(args.out, "hmm.json"), hmm)

    total = sum(sizes.values())
    print("\nweights/:")
    for k, v in sorted(sizes.items()):
        print(f"  {k:<26} {v / 1024:8.1f} KB")
    print(f"  {'TOTAL':<26} {total / 1024:8.1f} KB ({total / 1e6:.2f} MB)")
    print("\nHMM transition matrix (rows=from, cols=to; wake/light/deep/rem):")
    for lbl, row in zip(STAGE4_LABELS, hmm["trans"]):
        print(f"  {lbl:<6} " + " ".join(f"{x:.4f}" for x in row))

    summary_path = os.path.normpath(os.path.join(args.out, "..", "cv_report.json"))
    slim = {k: {kk: vv for kk, vv in v.items() if kk != "per_night"}
            for k, v in results.items()}
    slim["chosen"] = {k: {"name": c, "spec": s} for k, (c, s) in chosen.items()}
    slim["sizes_bytes"] = sizes
    with open(summary_path, "w") as fh:
        json.dump(slim, fh, indent=1, default=str)
    print(f"\nCV report written to {summary_path}")
    print(f"done in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
