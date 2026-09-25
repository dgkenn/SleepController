#!/usr/bin/env python3
"""Train the beat-interval (HRV) sleep-staging variants on a reduced DREAMT corpus.

    python3 scripts/train_dreamt.py --data-dir /path/to/reduced --out sleepctl/ml/sleep_staging/weights

Reads the reduction written by ``scripts/dreamt_reduce.py`` (HR, IBI, actigraphy counts
and PSG labels per participant) and trains, with participant-grouped cross-validation, the
same tree-ensemble + HMM machinery the shipped HR / HR+motion models use:

    wake_hrv.json / stage4_hrv.json          HR + HRV + SCALE-FREE motion  (the live path
                                             when the Verity streams PPI and ACC)
    wake_hrvonly.json / stage4_hrvonly.json  HR + HRV                       (PPI, no motion)
    hmm_dreamt.json                          transition matrix / priors estimated on DREAMT
    cv_report_dreamt.json                    what the models scored, per variant

The runtime (``sleepctl.ml.sleep_staging.infer``) picks these up automatically when the
files exist under weights/. Only the weight files may be committed; the reduced corpus is
covered by the PhysioNet Credentialed Health Data Use Agreement and stays off the repo.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, Optional, Sequence, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np  # noqa: E402

from sleepctl.ml.sleep_staging import train as T  # noqa: E402
from sleepctl.ml.sleep_staging.dataset import (  # noqa: E402
    StagingDataset, build_dataset, discover_subjects, subjects_with_ibi)
from sleepctl.ml.sleep_staging.features import (  # noqa: E402
    FEATURE_NAMES_HR_HRV, FEATURE_NAMES_HRV_MOTION)


def _select(tag: str, ds: StagingDataset, names: Sequence[str], grid, folds: int,
            fixed: Optional[dict] = None) -> Tuple[str, dict, dict, dict]:
    print(f"\n=== model selection [{tag}] ({len(ds)} epochs, {len(set(ds.groups))} participants) ===",
          flush=True)
    best = None
    for cname, spec in grid:
        t1 = time.time()
        cache = T.cv_emissions(ds, names, spec, n_folds=folds)
        res = T.score_emissions(cache)
        k = res["raw"]["kappa4"]
        flag = "" if T._exportable(spec) else "  (NOT exportable)"
        print(f"  {cname:<8} 4-class k={k:.3f} wake k={res['raw']['wake_kappa']:.3f} "
              f"acc4={res['raw']['acc4']:.3f}  [{time.time() - t1:.0f}s]{flag}", flush=True)
        if T._exportable(spec) and (best is None or k > best[3]["raw"]["kappa4"]):
            best = (cname, spec, cache, res)
    if best is None:
        raise RuntimeError("no exportable candidate produced a CV score")
    cname, spec, cache, _res = best
    if fixed:
        tuned = T.tune_smoothing(cache, epochs_grid=(fixed["epochs"],), temper_grid=(fixed["temper"],),
                                 prior_modes=(fixed["prior_mode"],))
    else:
        tuned = T.tune_smoothing(cache)
    res = tuned["res"]
    res["hmm"] = cache["hmm_full"]
    smoothing = dict(temper=tuned["temper"], epochs=tuned["epochs"],
                     prior_mode=tuned["prior_mode"], wake_mode=tuned["wake_mode"])
    return cname, spec, res, smoothing


def train(data_dir: str, out: str, folds: int = 5, quick: bool = False, jobs: int = 1,
          subjects: Optional[Sequence[str]] = None, skip_hrvonly: bool = False,
          cache_dir: Optional[str] = None, exportable_only: bool = False) -> Dict[str, object]:
    """``exportable_only`` drops the reference-only candidates (gradient boosting), which can
    never be installed but cost ~70% of the CV time on 216 features, single-threaded."""
    t0 = time.time()
    sids = list(subjects or discover_subjects(data_dir))
    with_ibi = subjects_with_ibi(data_dir, sids)
    print(f"participants: {len(sids)} reduced, {len(with_ibi)} with beat intervals", flush=True)
    if len(with_ibi) < 2:
        raise SystemExit("need at least two participants with <ID>_ibi.txt to cross-validate")
    folds = max(2, min(folds, len(with_ibi)))
    print("building HR + HRV + motion dataset ...", flush=True)
    ds = T.cached_dataset(cache_dir, f"dreamt|{','.join(with_ibi)}",
                          lambda: build_dataset(data_dir, with_ibi, use_activity=True,
                                                require_activity=False, use_ibi=True,
                                                require_ibi=True, jobs=jobs, verbose=True))
    print(f"  {len(ds)} epochs from {len(set(ds.groups))} participants ({time.time() - t0:.0f}s)")
    if not len(ds):
        raise SystemExit("the reduction produced no scorable epochs")
    grid = T._candidates(quick)
    if exportable_only:
        grid = [(c, sp) for c, sp in grid if T._exportable(sp)]
    results: Dict[str, dict] = {}
    chosen: Dict[str, Tuple[str, dict]] = {}

    cname, spec, res, smoothing = _select("HR+HRV+motion", ds, FEATURE_NAMES_HRV_MOTION, grid, folds)
    chosen["hrv"] = (cname, spec)
    results["hrv"] = res
    T.report(f"HR+HRV+motion CV [{cname}] smoothing={smoothing}", res)

    smoothing_only = smoothing
    if not skip_hrvonly:
        cname_o, spec_o, res_o, smoothing_only = _select(
            "HR+HRV (no motion)", ds, FEATURE_NAMES_HR_HRV, [(cname, spec)], folds, fixed=smoothing)
        chosen["hrvonly"] = (cname_o, spec_o)
        results["hrvonly"] = res_o
        T.report(f"HR+HRV CV [{cname_o}]", res_o)

    print("\n=== final fits + export ===", flush=True)
    os.makedirs(out, exist_ok=True)
    sizes: Dict[str, int] = {}

    def fit_and_export(names: Sequence[str], spec: dict, suffix: str, sm: dict) -> None:
        X = np.asarray(ds.matrix(names), dtype=float)
        y4 = np.asarray(ds.y_stage4, dtype=int)
        yw = np.asarray(ds.y_wake, dtype=int)
        natural = sm.get("wake_mode") == "natural"
        wake_bal, wake_nat, stage_m = T._fit_heads(spec, X, y4, yw, both_wake=natural)
        wake_m = wake_nat if natural else wake_bal
        sizes[f"wake_{suffix}.json"] = T.write_json(os.path.join(out, f"wake_{suffix}.json"),
                                                    T.export_forest(wake_m, names))
        sizes[f"stage4_{suffix}.json"] = T.write_json(os.path.join(out, f"stage4_{suffix}.json"),
                                                      T.export_forest(stage_m, names))

    fit_and_export(FEATURE_NAMES_HRV_MOTION, chosen["hrv"][1], "hrv", smoothing)
    if "hrvonly" in chosen:
        fit_and_export(FEATURE_NAMES_HR_HRV, chosen["hrvonly"][1], "hrvonly", smoothing_only)

    hmm = T.estimate_hmm(np.asarray(ds.y_stage4), ds.groups, ds.times)
    hmm["temper"] = float(smoothing["temper"])
    hmm["smoothing_epochs"] = int(smoothing["epochs"])
    hmm["emission_prior"] = [0.25] * 4 if smoothing["prior_mode"] == "uniform" else hmm["prior"]
    hmm["note"] = "estimated on DREAMT (PSG-labelled); the shipped hmm.json stays the sleep-accel one"
    sizes["hmm_dreamt.json"] = T.write_json(os.path.join(out, "hmm_dreamt.json"), hmm)

    print("\nweights/:")
    for k, v in sorted(sizes.items()):
        print(f"  {k:<26} {v / 1024:8.1f} KB")
    slim = {k: {kk: vv for kk, vv in v.items() if kk != "per_night"} for k, v in results.items()}
    slim["chosen"] = {k: {"name": c, "spec": s} for k, (c, s) in chosen.items()}
    slim["sizes_bytes"] = sizes
    slim["participants"] = len(set(ds.groups))
    slim["epochs"] = len(ds)
    report_path = os.path.normpath(os.path.join(out, "..", "cv_report_dreamt.json"))
    with open(report_path, "w") as fh:
        json.dump(slim, fh, indent=1, default=str)
    print(f"\nCV report written to {report_path}  ({time.time() - t0:.0f}s total)")
    return slim


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True, help="the reduced DREAMT folder")
    ap.add_argument("--out", default=T.WEIGHTS_DIR)
    ap.add_argument("--folds", type=int, default=T.N_FOLDS)
    ap.add_argument("--quick", action="store_true", help="tiny model grid (smoke test)")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--subjects", nargs="*", default=None)
    ap.add_argument("--skip-hrvonly", action="store_true")
    ap.add_argument("--cache-dir", default=None, help="pickled datasets (keep OUT of the repo)")
    args = ap.parse_args(argv)
    train(args.data_dir, args.out, folds=args.folds, quick=args.quick, jobs=args.jobs,
          subjects=args.subjects, skip_hrvonly=args.skip_hrvonly, cache_dir=args.cache_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
