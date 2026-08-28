#!/usr/bin/env python3
"""Measure the autonomic (HRV) channel against published nights, and say whether to trust it.

Runs entirely off the night JSON on the `night-data` branch -- no database, no box access:

    python3 scripts/calibrate_autonomic.py /path/to/nights/night-2026-08-*.json

Prints three things:
  1. per-feature AUC against the actigraphy reference, with the weights the data implies,
  2. the autonomic channel scored as a standalone detector,
  3. the decision that actually matters -- does FUSING it beat motion alone?

(3) is the gate for ``SleepWakeDetector.AUTONOMIC_DEFAULT``. A channel can carry real signal and
still make the combined detector worse, and only the fused comparison answers that.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleepctl.eval.autonomic_calibration import (composite_evaluation, format_report,
                                                 measure_features, proposed_weights)
from sleepctl.ml.sleep_wake import SleepWakeDetector


def _load(paths: List[str]):
    out = []
    for p in paths:
        try:
            with open(p) as fh:
                out.append((p.split("/")[-1].replace(".json", ""), json.load(fh)))
        except Exception as exc:  # a corrupt night should not hide the others
            print(f"  ! {p}: {exc!r}", file=sys.stderr)
    return out


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    nights = _load(argv[1:])
    if not nights:
        print("no nights loaded")
        return 1

    measured = measure_features(nights)
    print(format_report(measured))
    weights = proposed_weights(measured)
    survivors = [k for k, v in weights.items() if v > 0]

    print("\n" + "=" * 78)
    print("THE AUTONOMIC CHANNEL AS ONE SCORE")
    comp = composite_evaluation(nights)
    if not comp.get("n_epochs"):
        print("  no comparable epochs")
        return 0
    print(f"  epochs {comp['n_epochs']}  reference-wake fraction "
          f"{comp['reference_wake_fraction']}")
    print(f"  composite AUC {comp['composite_auc']}   (0.5 = carries nothing)")
    for n in comp["per_night_auc"]:
        print(f"    {n['night']}  {n['auc']}")
    best = comp.get("best_threshold") or {}
    print(f"  best threshold {best.get('threshold')}  ->  kappa {best.get('kappa')}, "
          f"accuracy {best.get('accuracy')}, wake-specificity {best.get('specificity_wake')}")
    print(f"  the shipped threshold is {SleepWakeDetector().threshold}")

    print("\n" + "=" * 78)
    print("THE DECISION")
    print("  NOT answerable by scoring the fused detector against this reference: the fusion")
    print("  reduces exactly to its motion channel (weights 1.0 vs 0.6 at a 0.5 threshold), and")
    print("  that channel IS Cole-Kripke+Webster -- the reference. It returns kappa 1.000 by")
    print("  construction, which measures an algorithm agreeing with itself.")
    print(f"  features carrying measurable signal: {survivors or 'NONE'}")
    strong = [k for k in survivors if k != "hr_from_ibi"]
    if not strong:
        print("  -> the only feature that separates is heart rate, which the CARDIAC channel")
        print("     already carries. Leave AUTONOMIC_DEFAULT off: switching it on would add")
        print("     weight, not information.")
    else:
        print(f"  -> beyond heart rate, these still separate: {strong}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
