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

from sleepctl.eval.in_bed import provenance, split_in_bed  # noqa: E402
from sleepctl.eval.performance import (evaluate, format_report,  # noqa: E402
                                       reference_discriminability)
from sleepctl.eval.reference_stagers import (calibrate_scale, cole_kripke,  # noqa: E402
                                             oakley, sadeh, webster_rescore)

_SLEEP = {"light", "deep", "rem"}

#: Below this many scorable epochs a night is reported but not pooled -- see the
#: guard in main() for what happened when it was.
MIN_EPOCHS_TO_JUDGE = 60


def _per_minute(night: dict):
    """(ours_asleep, counts) aligned per minute over the in-bed session.

    Movement on the frame is the fused 0..1 index; the wearable's own PIM counts are what the
    reference algorithms want, so movement is rescaled to a count-like magnitude. The absolute
    scale does not matter -- ``calibrate_scale`` fits it -- only that it is proportional.
    """
    ours: dict = {}
    counts: dict = {}   # ONLY minutes that actually carried an actigraphy sample
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
    # INTERSECTION, not union. A minute with no actigraphy used to enter the comparison with a
    # default count of 0.0, which Cole-Kripke reads as perfect stillness -- so the reference
    # scored absent data as confidently asleep, and every wake call we made there became a false
    # positive. On 2026-08-29, 26 of 2830 samples carried movement: the reference was scoring
    # ~337 fabricated epochs and returned a specificity of 1.000 off the back of them.
    #
    # This is the same rule the sleep/wake detector already states for its own channels: a
    # missing channel is dropped from the average rather than scored as zero, because treating
    # "no accelerometer" as positive evidence of sleep is how a sensor outage becomes a report
    # of a perfect night.
    keys = sorted(set(ours) & set(counts))
    # DROP THE EPOCHS THE SLEEPER WAS NOT IN BED FOR. Until bed exit could be detected at all, a
    # session ran on for hours after the sleeper got up, so "the night" as published routinely
    # contains a walking-around morning -- and scoring a sleep stager over it measures the wrong
    # thing. On 2026-08-27 that is 179 of 684 epochs, and including them takes kappa against
    # Cole-Kripke+Webster from 0.513 to 0.154. See sleepctl/eval/in_bed.py for why this is not a
    # flattering filter: it makes 2026-08-30 slightly worse.
    samples = [s for s in (night.get("raw_samples") or [])
               if str(s.get("controller_state")) not in ("idle", "None")]
    kept, dropped = split_in_bed(samples, keys)
    return ([ours.get(k) for k in kept], [counts.get(k, 0.0) for k in kept], kept, dropped)


def main() -> int:
    paths = sys.argv[1:]
    if not paths:
        print(__doc__)
        return 2
    totals = Counter()
    for p in paths:
        night = json.loads(Path(p).read_text())
        ours, counts, keys, dropped = _per_minute(night)
        if not any(c > 0 for c in counts):
            print(f"{night.get('night_date')}: no actigraphy -- cannot compare")
            continue
        # A night that survives the filters with a handful of epochs must not be averaged in as
        # an equal. 2026-08-29 carried actigraphy on 26 of 2830 samples and came out of the
        # filters with 7 scorable epochs; pooled with equal weight against two ~500-epoch nights
        # it moved the reported kappa by more than either real night did.
        if len(counts) < MIN_EPOCHS_TO_JUDGE:
            print(f"{night.get('night_date')}: only {len(counts)} scorable epoch(s) "
                  f"(actigraphy on {(night.get('sensor_capture') or {}).get('movement_present')} "
                  f"of {(night.get('sensor_capture') or {}).get('n_samples')} samples) "
                  f"-- too few to judge")
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
        # Print WHAT WAS SCORED alongside the scores. Two runs of this comparison over the
        # same night produced 456 and 684 epochs and could not be reconciled afterwards,
        # because neither run recorded its own denominator.
        print(f"\n=== {night.get('night_date')} ===")
        print(f"  {provenance(night, len(keys), len(dropped))}")
        # How much the reference's own wake calls rest on motion. A kappa quoted without this
        # overstates what was established -- see reference_discriminability.
        disc = reference_discriminability(counts, refs["cole_kripke_webster"])
        if disc.get("reference_wake_epochs"):
            print(f"  reference quality: {disc['reference_wake_epochs']} wake call(s), "
                  f"{disc['wake_calls_at_or_below_median_motion']} of them "
                  f"({disc['wake_calls_without_motion_evidence_frac']:.0%}) at or below the "
                  f"night's median movement; motion dynamic range "
                  f"p95/median = {disc['motion_dynamic_range']}")
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
