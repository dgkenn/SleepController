"""Train + evaluate + export the wearable sleep-staging models.

Fits, for BOTH feature variants (HR-only and HR+motion):
  * a binary wake model      (wake vs sleep), and
  * a 4-class stage model    (wake / light / deep / rem),
each a standardized, L2-regularized (multinomial) softmax logistic regression with
balanced class weights. Models are evaluated with leave-subjects-out grouped
cross-validation, then refit on ALL subjects and exported as compact JSON weights.

numpy is used HERE (training only). The runtime path (features.py + infer.py) stays
pure-stdlib.

CLI::

    python -m sleepctl.ml.sleep_staging.train --help
    python -m sleepctl.ml.sleep_staging.train --data-dir DIR --folds 5
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Dict, List, Optional, Tuple

import numpy as np

from .dataset import (
    FEATURE_NAMES_HR,
    FEATURE_NAMES_HRMOTION,
    DEFAULT_DATA_DIR,
    build_dataset,
)

WEIGHTS_DIR = os.path.join(os.path.dirname(__file__), "weights")


# --------------------------------------------------------------------------- model
def _standardize_fit(X: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std = np.where(std > 1e-8, std, 1.0)
    return mean, std


def _softmax(Z: np.ndarray) -> np.ndarray:
    Z = Z - Z.max(axis=1, keepdims=True)
    E = np.exp(Z)
    return E / E.sum(axis=1, keepdims=True)


def train_softmax(
    X: np.ndarray,
    y: np.ndarray,
    classes: List[int],
    l2: float = 1e-2,
    lr: float = 0.5,
    n_iter: int = 800,
    class_weight_power: float = 0.5,
) -> Tuple[np.ndarray, np.ndarray]:
    """Full-batch gradient-descent softmax regression. Returns (W[K,D], b[K]).

    ``class_weight_power`` interpolates the inverse-frequency class weights:
    0.0 = unweighted (maximizes accuracy), 1.0 = fully balanced (maximizes minority
    recall). ~0.5 gives the best Cohen's-kappa / accuracy tradeoff on this dataset.
    """
    n, d = X.shape
    k = len(classes)
    cls_index = {c: i for i, c in enumerate(classes)}
    y_idx = np.array([cls_index[int(v)] for v in y])
    Y = np.zeros((n, k))
    Y[np.arange(n), y_idx] = 1.0

    if class_weight_power > 0:
        counts = np.array([(y_idx == i).sum() for i in range(k)], dtype=float)
        counts = np.where(counts > 0, counts, 1.0)
        cw = (n / (k * counts)) ** class_weight_power
        sample_w = cw[y_idx]
    else:
        sample_w = np.ones(n)
    sw = sample_w.reshape(-1, 1)
    sw_sum = sample_w.sum()

    W = np.zeros((d, k))
    b = np.zeros(k)
    for _ in range(n_iter):
        Z = X @ W + b
        P = _softmax(Z)
        G = (P - Y) * sw  # [n,k]
        gradW = X.T @ G / sw_sum + l2 * W
        gradb = G.sum(axis=0) / sw_sum
        W -= lr * gradW
        b -= lr * gradb
    return W.T, b  # [K,D], [K]


def predict_labels(X: np.ndarray, W: np.ndarray, b: np.ndarray, classes: List[int]) -> np.ndarray:
    P = _softmax(X @ W.T + b)
    return np.array([classes[i] for i in P.argmax(axis=1)])


# --------------------------------------------------------------------------- metrics
def cohen_kappa(y_true: np.ndarray, y_pred: np.ndarray, labels: List[int]) -> float:
    n = len(y_true)
    if n == 0:
        return 0.0
    idx = {l: i for i, l in enumerate(labels)}
    k = len(labels)
    cm = np.zeros((k, k))
    for t, p in zip(y_true, y_pred):
        cm[idx[int(t)], idx[int(p)]] += 1
    po = np.trace(cm) / n
    row = cm.sum(axis=1) / n
    col = cm.sum(axis=0) / n
    pe = float((row * col).sum())
    if abs(1.0 - pe) < 1e-12:
        return 0.0
    return (po - pe) / (1.0 - pe)


def accuracy(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    if len(y_true) == 0:
        return 0.0
    return float((y_true == y_pred).mean())


def prf_for_class(y_true: np.ndarray, y_pred: np.ndarray, pos: int) -> Tuple[float, float, float]:
    tp = int(((y_pred == pos) & (y_true == pos)).sum())
    fp = int(((y_pred == pos) & (y_true != pos)).sum())
    fn = int(((y_pred != pos) & (y_true == pos)).sum())
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return prec, rec, f1


def per_class_recall(y_true: np.ndarray, y_pred: np.ndarray, labels: List[int]) -> Dict[int, float]:
    out = {}
    for l in labels:
        _, rec, _ = prf_for_class(y_true, y_pred, l)
        out[l] = rec
    return out


# --------------------------------------------------------------------------- CV
def group_folds(groups: List[str], n_folds: int) -> List[np.ndarray]:
    uniq = sorted(set(groups))
    fold_of_subject = {s: i % n_folds for i, s in enumerate(uniq)}
    groups_arr = np.array(groups)
    folds = []
    for f in range(n_folds):
        subs = [s for s in uniq if fold_of_subject[s] == f]
        mask = np.isin(groups_arr, subs)
        folds.append(np.where(mask)[0])
    return folds


def cross_val_oof(
    X: np.ndarray,
    y: np.ndarray,
    classes: List[int],
    groups: List[str],
    n_folds: int,
    l2: float,
    class_weight_power: float,
) -> np.ndarray:
    """Return out-of-fold predictions aligned to y."""
    folds = group_folds(groups, n_folds)
    oof = np.zeros_like(y)
    for test_idx in folds:
        train_idx = np.setdiff1d(np.arange(len(y)), test_idx)
        mean, std = _standardize_fit(X[train_idx])
        Xtr = (X[train_idx] - mean) / std
        Xte = (X[test_idx] - mean) / std
        W, b = train_softmax(Xtr, y[train_idx], classes, l2=l2,
                             class_weight_power=class_weight_power)
        oof[test_idx] = predict_labels(Xte, W, b, classes)
    return oof


# --------------------------------------------------------------------------- export
def export_weights(
    path: str,
    feature_names: List[str],
    mean: np.ndarray,
    std: np.ndarray,
    classes: List[int],
    W: np.ndarray,
    b: np.ndarray,
    ndigits: int = 6,
) -> int:
    def r(x):
        return round(float(x), ndigits)

    obj = {
        "feature_names": list(feature_names),
        "mean": [r(v) for v in mean],
        "std": [r(v) for v in std],
        "classes": [int(c) for c in classes],
        "coef": [[r(v) for v in row] for row in W],
        "intercept": [r(v) for v in b],
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh, separators=(",", ":"))
    return os.path.getsize(path)


# --------------------------------------------------------------------------- driver
def _fmt(x: float) -> str:
    return f"{x:.3f}"


def run(data_dir: str, n_folds: int, l2: float, weights_dir: str,
        class_weight_power: float) -> int:
    print(f"Building dataset from {data_dir} ...")
    ds = build_dataset(data_dir=data_dir)
    n_subj = len(set(ds.groups))
    print(f"  {len(ds)} epochs across {n_subj} subjects\n")
    if len(ds) == 0:
        print("No data — did you run scripts/fetch_sleep_accel.py?")
        return 1

    y_wake = np.array(ds.y_wake)
    y_stage4 = np.array(ds.y_stage4)
    wake_classes = [0, 1]              # 0 wake, 1 sleep
    stage_classes = [0, 1, 2, 3]       # wake, light, deep, rem
    stage_names = {0: "wake", 1: "light", 2: "deep", 3: "rem"}

    variants = [
        ("hr", FEATURE_NAMES_HR),
        ("hrmotion", FEATURE_NAMES_HRMOTION),
    ]

    print("=" * 70)
    print(f"GROUPED CROSS-VALIDATION  (leave-subjects-out, {n_folds} folds)")
    print("=" * 70)

    summary_lines = []
    for vname, feat_names in variants:
        X = np.array(ds.matrix(feat_names), dtype=float)

        # ---- wake (binary) ----
        oof_w = cross_val_oof(X, y_wake, wake_classes, ds.groups, n_folds, l2,
                              class_weight_power)
        w_acc = accuracy(y_wake, oof_w)
        w_kappa = cohen_kappa(y_wake, oof_w, wake_classes)
        w_prec, w_rec, w_f1 = prf_for_class(y_wake, oof_w, pos=0)  # wake = class 0

        # ---- stage4 ----
        oof_s = cross_val_oof(X, y_stage4, stage_classes, ds.groups, n_folds, l2,
                              class_weight_power)
        s_acc = accuracy(y_stage4, oof_s)
        s_kappa = cohen_kappa(y_stage4, oof_s, stage_classes)
        s_recall = per_class_recall(y_stage4, oof_s, stage_classes)

        print(f"\n--- variant: {vname}  ({len(feat_names)} features) ---")
        print(f"  WAKE  acc={_fmt(w_acc)}  kappa={_fmt(w_kappa)}  "
              f"prec={_fmt(w_prec)}  recall={_fmt(w_rec)}  F1={_fmt(w_f1)}")
        print(f"  4CLS  acc={_fmt(s_acc)}  kappa={_fmt(s_kappa)}")
        rec_str = "  ".join(f"{stage_names[c]}={_fmt(s_recall[c])}" for c in stage_classes)
        print(f"        per-class recall: {rec_str}")

        summary_lines.append(
            f"{vname:9s} | wake acc {_fmt(w_acc)} kappa {_fmt(w_kappa)} "
            f"P/R/F1 {_fmt(w_prec)}/{_fmt(w_rec)}/{_fmt(w_f1)} "
            f"| 4cls acc {_fmt(s_acc)} kappa {_fmt(s_kappa)}"
        )

    # ---- refit on ALL subjects + export ----
    print("\n" + "=" * 70)
    print("REFIT ON ALL SUBJECTS + EXPORT")
    print("=" * 70)
    exported = []
    for vname, feat_names in variants:
        X = np.array(ds.matrix(feat_names), dtype=float)
        mean, std = _standardize_fit(X)
        Xs = (X - mean) / std

        Ww, bw = train_softmax(Xs, y_wake, wake_classes, l2=l2,
                              class_weight_power=class_weight_power)
        pw = export_weights(
            os.path.join(weights_dir, f"wake_{vname}.json"),
            feat_names, mean, std, wake_classes, Ww, bw,
        )
        Ws, bs = train_softmax(Xs, y_stage4, stage_classes, l2=l2,
                              class_weight_power=class_weight_power)
        ps = export_weights(
            os.path.join(weights_dir, f"stage4_{vname}.json"),
            feat_names, mean, std, stage_classes, Ws, bs,
        )
        exported.append((f"wake_{vname}.json", pw))
        exported.append((f"stage4_{vname}.json", ps))

    for name, size in exported:
        print(f"  {os.path.join(weights_dir, name)}  ({size} bytes)")

    print("\nSummary:")
    for line in summary_lines:
        print("  " + line)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="dir with the .txt files")
    ap.add_argument("--folds", type=int, default=5, help="grouped CV folds (by subject)")
    ap.add_argument("--l2", type=float, default=1e-2, help="L2 regularization strength")
    ap.add_argument("--class-weight-power", type=float, default=0.5,
                    help="inverse-freq class-weight exponent (0=accuracy, 1=balanced)")
    ap.add_argument("--weights-dir", default=WEIGHTS_DIR, help="output dir for JSON weights")
    args = ap.parse_args(argv)
    return run(args.data_dir, args.folds, args.l2, args.weights_dir,
               args.class_weight_power)


if __name__ == "__main__":
    raise SystemExit(main())
