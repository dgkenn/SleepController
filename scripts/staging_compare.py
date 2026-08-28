"""Compare our sleep staging against published, PSG-validated actigraphy algorithms.

    python scripts/staging_compare.py night-2026-08-27.json [more.json ...]

Reads night JSONs from the `night-data` branch, reconstructs per-minute activity counts from the
wearable's actigraphy, and scores our sleep/wake calls against Cole-Kripke and Sadeh. See
``sleepctl.eval.reference_stagers`` for why those two and how the comparison is read.

The output is deliberately not a single accuracy number: actigraphy cannot see quiet wakefulness,
so a symmetric score would flatter us in precisely the direction the references are known to be
wrong. Disagreements are split into the strong direction (independent motion says awake, we said
asleep -- a real blind spot) and the weak one (we said awake while motion was quiet -- where we
may well be right, since we have HR and HRV and they do not).
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleepctl.eval.performance import evaluate, format_report  # noqa: E402
from sleepctl.eval.reference_stagers import (calibrate_scale, cole_kripke,  # noqa: E402
                                             oakley, sadeh, webster_rescore)

_SLEEP = {"light", "deep", "rem"}


def _per_minute(night: dict):
    """(ours_asleep, counts) aligned per minute over the in-bed session.

    Movement on the frame is the fused 0..1 index; the wearable's own PIM counts are what the
    reference algorithms want, so movement is rescaled to a count-like magnitude. The absolute
    scale does not matter -- ``calibrate_scale`` fits it -- only that it is proportional.
    """
    ours: dict = {}
    counts: dict = {}
    for s in night.get("raw_samples") or []:
        if str(s.get("controller_state")) in ("idle", "None"):
            continue
        minute = str(s.get("ts"))[:16]
        stage = str(s.get("stage"))
        if stage in _SLEEP:
            ours.setdefault(minute, True)
        elif stage == "awake":
            ours[minute] = False          # an awake label wins within the minute
        mv = s.get("movement")
        if mv is not None:
            counts[minute] = max(counts.get(minute, 0.0), float(mv) * 1000.0)
    keys = sorted(set(ours) | set(counts))
    return ([ours.get(k) for k in keys], [counts.get(k, 0.0) for k in keys], keys)


def main() -> int:
    paths = sys.argv[1:]
    if not paths:
        print(__doc__)
        return 2
    totals = Counter()
    for p in paths:
        night = json.loads(Path(p).read_text())
        ours, counts, keys = _per_minute(night)
        if not any(c > 0 for c in counts):
            print(f"{night.get('night_date')}: no actigraphy -- cannot compare")
            continue
        # Standardized evaluation (Menghini et al., SLEEP 2021) against each reference. This
        # replaces the ad-hoc "strong/weak disagreement" counting that used to live here, which
        # was an informal re-derivation of sensitivity and specificity with the summary-measure
        # and agreement analyses missing entirely.
        refs = {
            "cole_kripke": cole_kripke(counts, scale=calibrate_scale(counts, algorithm=cole_kripke)),
            "cole_kripke_webster": webster_rescore(
                cole_kripke(counts, scale=calibrate_scale(counts, algorithm=cole_kripke))),
            "sadeh": sadeh(counts, scale=calibrate_scale(counts, algorithm=sadeh)),
            "oakley": oakley(counts, scale=calibrate_scale(counts, algorithm=oakley)),
        }
        print(f"\n=== {night.get('night_date')} ===")
        for name, ref_sleep in refs.items():
            res = evaluate(ours, ref_sleep)
            print(format_report(res, label=str(night.get("night_date")), reference=name))
            ebe = res["epoch_by_epoch"]
            if ebe.get("n_epochs"):
                totals[f"{name}_acc"] += ebe["accuracy"]
                totals[f"{name}_spec"] += (ebe["specificity"] or 0.0)
                totals[f"{name}_sens"] += (ebe["sensitivity"] or 0.0)
                totals[f"{name}_kappa"] += (ebe["kappa"] or 0.0)
                totals[f"{name}_n"] += 1
        continue
        res = compare(ours, counts)
        if not res.get("n"):
            print(f"{night.get('night_date')}: no overlapping labelled minutes")
            continue
        print(f"\n=== {night.get('night_date')}  ({res['n']} labelled minutes; count scale "
              f"CK {res['scale_cole_kripke']}, Sadeh {res['scale_sadeh']}) ===")
        for name in ("cole_kripke", "cole_kripke_webster", "sadeh", "oakley"):
            a = res[name]
            print(f"  {name:20} agreement {a['agreement']:.3f}  "
                  f"(both asleep {a['both_sleep']}, both awake {a['both_wake']})")
            print(f"    STRONG disagreement -- motion says awake, we said asleep: "
                  f"{a['missed_wake_we_called_sleep']}")
            print(f"    weak   disagreement -- we said awake, motion quiet:       "
                  f"{a['we_called_wake_ref_quiet']}")
            totals[f"{name}_strong"] += a["missed_wake_we_called_sleep"]
            totals[f"{name}_weak"] += a["we_called_wake_ref_quiet"]
            totals[f"{name}_agree"] += a["agreement"]
            totals[f"{name}_nights"] += 1
        print(f"  the two references disagree with each other on {res['references_disagree']} "
              f"minute(s) -- our label cannot be judged there")
        totals["n"] += res["n"]
        totals["ref_disagree"] += res["references_disagree"]

    names = ("cole_kripke", "cole_kripke_webster", "sadeh", "oakley")
    if any(totals[f"{n}_n"] for n in names):
        print("\n=== MEAN ACROSS NIGHTS (Menghini framework) ===")
        print(f"  {'reference':22} {'acc':>6} {'sens':>6} {'spec':>6} {'kappa':>6}")
        for name in names:
            k = totals[f"{name}_n"]
            if not k:
                continue
            print(f"  {name:22} {totals[f'{name}_acc']/k:6.3f} {totals[f'{name}_sens']/k:6.3f} "
                  f"{totals[f'{name}_spec']/k:6.3f} {totals[f'{name}_kappa']/k:6.3f}")
        print("  sensitivity = detecting SLEEP; specificity = detecting WAKE.")
        print("  On a mostly-asleep night accuracy is dominated by sensitivity, which is why the")
        print("  framework reports both -- and why kappa, being chance-corrected, is the honest one.")
    if totals["n"]:
        print(f"\n=== ACROSS ALL NIGHTS ({totals['n']} minutes) ===")
        for name in ("cole_kripke", "cole_kripke_webster", "sadeh", "oakley"):
            s, w = totals[f"{name}_strong"], totals[f"{name}_weak"]
            nn = totals[f"{name}_nights"] or 1
            print(f"  {name:20} mean agreement {totals[f'{name}_agree']/nn:.3f} | "
                  f"strong disagreements {s} ({100*s/totals['n']:.1f}%), "
                  f"weak {w} ({100*w/totals['n']:.1f}%)")
        print(f"  references disagree with each other on {totals['ref_disagree']} "
              f"({100*totals['ref_disagree']/totals['n']:.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
