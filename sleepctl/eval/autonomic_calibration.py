"""Measure whether the autonomic (HRV) channel actually carries wake information.

WHY THIS EXISTS
---------------
``sleepctl.ml.sleep_wake`` extracts eight HRV features from the Verity's beat-to-beat
intervals and combines them with hand-set weights. Those weights and directions came from the
autonomic sleep literature, not from this sleeper's data, and the module says so plainly: its
``AUTONOMIC_DEFAULT = False`` docstring records that on synthetic data the channel scored 47.6%
accuracy -- worse than useless -- and that it stays off "until it has been calibrated against
real nights. Turning this on without that calibration would be substituting a plausible story
for a measurement."

The export that made those nights readable landed later, and it works: 2026-08-23 through
2026-08-27 carry 553, 566, 394, 9 and 780 HRV windows. This module is the measurement that was
deferred.

WHAT IT MEASURES AGAINST
------------------------
There is no polysomnography here, so the reference is Cole-Kripke with Webster rescoring over
the armband's own actigraphy counts -- the PSG-validated pairing, run in the modality it was
validated for. Using actigraphy as the stand-in reference when PSG is unavailable is the
standard fallback in this literature, and it is legitimate HERE for a specific reason: the
autonomic features come from inter-beat intervals and the reference comes from an
accelerometer. Two different modalities, so an HRV feature that separates the reference's wake
epochs from its sleep epochs is carrying real information rather than echoing its own label.

That is also the limit of the claim. The reference is itself ~88%-accurate against PSG and is
documented to be weak exactly where we care most (scoring quiet wakefulness as sleep), so an
AUC here is evidence about a feature's DIRECTION and RELATIVE strength, not an absolute accuracy.

HOW THE NUMBERS ARE POOLED
--------------------------
AUC is computed per night and then averaged, never by pooling raw feature values across nights.
Every feature in this set is normalised against the night's own distribution -- pooling absolute
RMSSD across nights would measure between-night differences in resting vagal tone, which has
nothing to do with within-night wake detection. Per-night AUC also makes the consistency check
possible: a feature that helps on three nights and hurts on two is noise, whatever its mean.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from sleepctl.ml.sleep_wake import (AUTONOMIC_FEATURE_NAMES, _WAKE_DOWN_FEATURES,
                                    _WAKE_UP_FEATURES)

#: A feature must clear this |AUC - 0.5| margin, averaged over nights, to be worth any weight.
#: Below it the separation is indistinguishable from sampling noise on a handful of nights.
MIN_AUC_MARGIN = 0.05

#: ...and it must point the same way on at least this fraction of the nights measured. A feature
#: whose direction flips night to night is not a signal, however large its average.
MIN_DIRECTION_CONSISTENCY = 0.6

#: Epochs whose heart rate sits at or above this are dropped before anything is measured. They
#: are not sleep and they are not in-bed wake either -- on 2026-08-27 they are a walking-around
#: morning that the controller recorded as a session, because nothing could end one (see
#: sleepctl/controller/bed_exit.py). Leaving them in lets ordinary daytime physiology masquerade
#: as this sleeper's wake distribution and inflates every AUC in the report.
OUT_OF_BED_HR = 95.0

#: Nights with fewer usable epochs than this are dropped: 2026-08-26 produced 9 HRV windows
#: (an armband dropout), and an AUC over 9 epochs is a coin flip with a decimal point.
MIN_EPOCHS_PER_NIGHT = 60


def _ts_minute_utc(ts: object) -> Optional[int]:
    """Minute index of a NAIVE-LOCAL timestamp read as if it were UTC.

    Deliberately wrong on its own -- ``align_night`` subtracts the night's UTC offset to make it
    right. Keeping the naive read separate from the correction is what makes the correction
    visible instead of hidden inside a library's idea of "local".
    """
    try:
        return int(datetime.fromisoformat(str(ts)).replace(tzinfo=timezone.utc).timestamp() // 60)
    except Exception:
        return None


def infer_utc_offset_s(night: Dict[str, object]) -> Optional[int]:
    """Recover the recording timezone's UTC offset from the night's own two clocks.

    THE PROBLEM THIS SOLVES. A published night carries two incompatible time conventions, exactly
    as ``storage/schema.py`` warns: ``raw_samples.ts`` is a NAIVE LOCAL string, while
    ``hrv_windows[].t`` is absolute epoch seconds derived from the AWARE UTC ``rr_intervals``.
    On the box the two reconcile for free, because Python resolves a naive timestamp against the
    system clock and the box IS in the recording timezone. Anywhere else -- a UTC container
    reading the published JSON, which is the entire point of publishing it -- the naive read is
    silently off by the whole UTC offset, and every epoch gets someone else's heart rate.

    That is not hypothetical: read this way, 2026-08-27 reported a mean SLEEPING heart rate of
    104 bpm against 69 bpm while awake. Physiologically impossible, and the only clue that the
    alignment was four hours out.

    Returns the timezone's UTC offset in seconds, in the same sign convention as
    ``datetime.utcoffset()`` (negative west of Greenwich), or None when the data cannot decide.

    THE METHOD. Both series contain heart rate -- the device's own reading in ``raw_samples`` and
    ``hr_from_ibi`` computed from the intervals. Slide one against the other in 15-minute steps
    (every real UTC offset is a multiple of 15 minutes) and take the shift that minimises the
    disagreement. It needs no timezone database, no configuration, and it fails loudly by
    returning None when the two series do not overlap enough to decide.
    """
    wins = night.get("hrv_windows") or []
    raw: Dict[int, float] = {}
    for smp in night.get("raw_samples") or []:
        if smp.get("heart_rate") is None:
            continue
        key = _ts_minute_utc(smp.get("ts"))
        if key is not None:
            raw[key] = float(smp["heart_rate"])
    if not raw or not wins:
        return None
    scored: List[Tuple[int, float]] = []
    for step in range(-56, 57):                      # +/- 14 h, the full range of real offsets
        off = step * 900
        errs = [abs(raw[k] - float(w["hr_from_ibi"]))
                for w in wins
                if w.get("hr_from_ibi") is not None
                and (k := int((float(w["t"]) - off) // 60)) in raw]
        if len(errs) < _MIN_OVERLAP_FOR_OFFSET:
            continue
        scored.append((off, sum(errs) / len(errs), len(errs)))
    if not scored:
        return None
    scored.sort(key=lambda x: x[1])
    best_off, best_err, best_n = scored[0]
    # AMBIGUITY IS A REASON TO REFUSE, NOT TO PICK ONE. A heart-rate series with a repeating
    # structure can match equally well at several shifts, and returning whichever came first is
    # how you get a full night of confident, wrong numbers -- the failure this whole function
    # exists to close. Rival shifts within an hour of the winner are ignored (heart rate is
    # autocorrelated, so its immediate neighbours always score well); anything further out that
    # matches nearly as well means the data cannot decide.
    rival = next((err for off, err, n in scored[1:]
                  if abs(off - best_off) >= _AMBIGUITY_SEPARATION_S
                  # A shift that only overlaps a fraction of the night is not a real rival --
                  # it is scoring a short, easy stretch. On 2026-08-24 a nine-hour shift came
                  # second on 261 overlapping minutes against the winner's 551.
                  and n >= _AMBIGUITY_MIN_OVERLAP_RATIO * best_n), None)
    if rival is not None and rival < best_err * (1.0 + _AMBIGUITY_RELATIVE_MARGIN) + 0.01:
        return None
    # Reported in the STANDARD sign -- what `datetime.utcoffset()` returns, negative in the
    # Americas -- so this and the exported `local_utc_offset_s` are the same quantity and
    # `align_night` can consume either without knowing which it got. The search above shifts in
    # the opposite direction (it slides the absolute clock onto the naive one), hence the negation.
    return -best_off


#: Minutes of overlap required before an offset candidate is even considered. Too few and the
#: best-scoring shift is whichever one happens to line up two quiet minutes.
_MIN_OVERLAP_FOR_OFFSET = 50

#: How far a rival shift must sit from the winner before it counts as a genuine alternative
#: rather than the winner's own autocorrelation tail.
_AMBIGUITY_SEPARATION_S = 3600

#: ...and how much worse it must be, proportionally, for the winner to be believed. Relative
#: rather than absolute: on the published nights the winning shift scores a mean error of 3.6 bpm
#: on one night and 18.4 on another, so no single bpm figure separates "clearly better" from
#: "tied" on both.
_AMBIGUITY_RELATIVE_MARGIN = 0.05

#: A rival must also overlap a comparable share of the night to count as one at all.
_AMBIGUITY_MIN_OVERLAP_RATIO = 0.7


def align_night(night: Dict[str, object],
                utc_offset_s: Optional[int] = None) -> List[Dict[str, object]]:
    """Line up a published night's HRV windows with its actigraphy, heart rate and our own stage.

    Returns one dict per HRV window: the feature values, ``counts`` (armband movement scaled the
    way the shadow-mode export scales it, so the two agree), ``hr``, and ``ours_asleep``.

    The offset between the night's two time conventions comes from ``local_utc_offset_s`` in the
    export when present, and is otherwise recovered from the data by ``infer_utc_offset_s`` --
    the 14 nights published before that field existed still align correctly.
    """
    wins = night.get("hrv_windows") or []
    if not wins:
        return []
    if utc_offset_s is None:
        utc_offset_s = night.get("local_utc_offset_s")
    if utc_offset_s is None:
        utc_offset_s = infer_utc_offset_s(night)
    if utc_offset_s is None:
        # Refusing is right. Aligning at an unknown offset produces a full night of confident,
        # wrong numbers -- the failure mode this whole function exists to close.
        return []
    off_min = int(round(float(utc_offset_s) / 60.0))
    by_min: Dict[int, Dict[str, object]] = {}
    for smp in night.get("raw_samples") or []:
        # `idle` samples are out-of-bed telemetry; scoring them would inflate the wake class
        # with epochs nobody was in bed for.
        if str(smp.get("controller_state")) in ("idle", "None"):
            continue
        key = _ts_minute_utc(smp.get("ts"))
        if key is None:
            continue
        key -= off_min          # naive-local minute -> true UTC minute
        slot = by_min.setdefault(key, {"mv": 0.0, "hr": None, "ours": None})
        if smp.get("movement") is not None:
            slot["mv"] = max(float(slot["mv"]), float(smp["movement"]) * 1000.0)
        if smp.get("heart_rate") is not None:
            slot["hr"] = float(smp["heart_rate"])
        stage = str(smp.get("stage"))
        if stage == "awake":
            slot["ours"] = True
        elif stage in ("light", "deep", "rem") and slot["ours"] is None:
            slot["ours"] = False

    epochs: List[Dict[str, object]] = []
    for w in wins:
        slot = by_min.get(int(float(w.get("t", 0.0)) // 60)) or {}
        row: Dict[str, object] = {"t": w.get("t"),
                                  "counts": float(slot.get("mv") or 0.0),
                                  "hr": slot.get("hr"),
                                  "ours_asleep": (None if slot.get("ours") is None
                                                  else not bool(slot["ours"]))}
        for name in AUTONOMIC_FEATURE_NAMES:
            row[name] = w.get(name)
        epochs.append(row)
    return epochs


def drop_out_of_bed(epochs: Sequence[Dict[str, object]],
                    ceiling: float = OUT_OF_BED_HR) -> List[Dict[str, object]]:
    """Remove epochs whose heart rate says the sleeper was up and about. See ``OUT_OF_BED_HR``."""
    out = []
    for e in epochs:
        hrs = [v for v in (e.get("hr"), e.get("hr_from_ibi")) if v is not None]
        if hrs and max(float(v) for v in hrs) >= ceiling:
            continue
        out.append(e)
    return out


def reference_labels(epochs: Sequence[Dict[str, object]]) -> List[bool]:
    """Cole-Kripke + Webster over the night's counts. True = asleep."""
    from sleepctl.eval.reference_stagers import calibrate_scale, cole_kripke, webster_rescore
    counts = [float(e.get("counts") or 0.0) for e in epochs]
    return webster_rescore(cole_kripke(counts, scale=calibrate_scale(counts)))


def auc(values: Sequence[Optional[float]], is_wake: Sequence[Optional[bool]]) -> Optional[float]:
    """Rank-based AUC for "higher value means wake", with proper handling of ties.

    Ties matter here: pNN50 is zero for long stretches of quiet sleep, and counting a tie as a
    win would manufacture separation out of a flat feature.
    """
    pairs = [(v, bool(w)) for v, w in zip(values, is_wake)
             if v is not None and w is not None]
    pos = [v for v, w in pairs if w]
    neg = [v for v, w in pairs if not w]
    if not pos or not neg:
        return None
    ordered = sorted(v for v, _ in pairs)
    ranks: Dict[float, float] = {}
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[i]:
            j += 1
        ranks[ordered[i]] = (i + j) / 2.0 + 1.0   # average rank, 1-based
        i = j + 1
    rank_sum = sum(ranks[v] for v in pos)
    n1, n2 = len(pos), len(neg)
    return round((rank_sum - n1 * (n1 + 1) / 2.0) / (n1 * n2), 4)


def _assumed_direction(name: str) -> int:
    """+1 if the literature says the feature RISES on waking, -1 if it falls."""
    if name in _WAKE_UP_FEATURES:
        return +1
    if name in _WAKE_DOWN_FEATURES:
        return -1
    return 0


def _assumed_weight(name: str) -> float:
    return float(_WAKE_UP_FEATURES.get(name) or _WAKE_DOWN_FEATURES.get(name) or 0.0)


def measure_features(nights: Sequence[Tuple[str, Dict[str, object]]],
                     min_epochs: int = MIN_EPOCHS_PER_NIGHT) -> Dict[str, object]:
    """Per-feature AUC against the actigraphy reference, one AUC per night, then pooled.

    ``nights`` is ``[(label, night_json), ...]``. AUC is stated in the feature's ASSUMED
    direction, so >0.5 means "the literature direction holds on this night's data" and <0.5
    means it is backwards here.
    """
    per_feature: Dict[str, List[float]] = {n: [] for n in AUTONOMIC_FEATURE_NAMES}
    used: List[Dict[str, object]] = []
    skipped: List[Dict[str, object]] = []
    for label, night in nights:
        epochs = drop_out_of_bed(align_night(night))
        if len(epochs) < min_epochs:
            skipped.append({"night": label, "n_epochs": len(epochs), "why": "too few HRV windows"})
            continue
        ref_asleep = reference_labels(epochs)
        is_wake = [not a for a in ref_asleep]
        wake_n = sum(1 for w in is_wake if w)
        if wake_n == 0 or wake_n == len(is_wake):
            skipped.append({"night": label, "n_epochs": len(epochs),
                            "why": "reference found only one class"})
            continue
        night_aucs: Dict[str, Optional[float]] = {}
        for name in AUTONOMIC_FEATURE_NAMES:
            raw = auc([e.get(name) for e in epochs], is_wake)
            if raw is None:
                night_aucs[name] = None
                continue
            # Restate in the assumed direction so every number is comparable.
            oriented = raw if _assumed_direction(name) > 0 else 1.0 - raw
            night_aucs[name] = round(oriented, 4)
            per_feature[name].append(oriented)
        used.append({"night": label, "n_epochs": len(epochs),
                     "reference_wake_epochs": wake_n, "auc": night_aucs})

    features: Dict[str, Dict[str, object]] = {}
    for name, vals in per_feature.items():
        if not vals:
            features[name] = {"n_nights": 0, "mean_auc": None, "verdict": "no data"}
            continue
        mean = statistics.mean(vals)
        consistent = sum(1 for v in vals if (v - 0.5) > 0) / len(vals)
        margin = abs(mean - 0.5)
        if margin < MIN_AUC_MARGIN:
            verdict = "no separation"
        elif mean < 0.5:
            # The measured direction contradicts the literature. On four nights of a proxy
            # reference that is NOT enough to flip a direction the physiology supports -- but it
            # is more than enough to stop weighting it.
            verdict = "contradicts assumed direction"
        elif consistent < MIN_DIRECTION_CONSISTENCY:
            verdict = "inconsistent across nights"
        else:
            verdict = "keep"
        features[name] = {
            "n_nights": len(vals),
            "mean_auc": round(mean, 4),
            "min_auc": round(min(vals), 4),
            "max_auc": round(max(vals), 4),
            "direction_consistency": round(consistent, 3),
            "assumed_direction": "up on wake" if _assumed_direction(name) > 0 else "down on wake",
            "assumed_weight": _assumed_weight(name),
            "verdict": verdict,
        }
    return {"features": features, "nights_used": used, "nights_skipped": skipped}


def proposed_weights(measured: Dict[str, object]) -> Dict[str, float]:
    """Weights derived from the measurement: proportional to separation, zero where absent.

    Scaled so the strongest surviving feature keeps weight 1.0, which leaves the fusion's
    threshold on the same footing as before rather than silently shifting it.
    """
    feats = measured.get("features") or {}
    raw: Dict[str, float] = {}
    for name, info in feats.items():
        if info.get("verdict") != "keep":
            raw[name] = 0.0
            continue
        raw[name] = round(2.0 * (float(info["mean_auc"]) - 0.5), 4)
    top = max(raw.values()) if raw else 0.0
    if top <= 0:
        return {k: 0.0 for k in raw}
    return {k: round(v / top, 3) for k, v in raw.items()}


def format_report(measured: Dict[str, object]) -> str:
    lines = ["AUTONOMIC CHANNEL CALIBRATION  (reference: Cole-Kripke + Webster on armband counts)"]
    used = measured.get("nights_used") or []
    lines.append(f"  nights measured: {len(used)}")
    for n in used:
        lines.append(f"    {n['night']}  {n['n_epochs']} epochs, "
                     f"{n['reference_wake_epochs']} reference-wake")
    for n in (measured.get("nights_skipped") or []):
        lines.append(f"    {n['night']}  SKIPPED ({n['why']}, {n['n_epochs']} epochs)")
    lines.append("")
    lines.append(f"  {'feature':14} {'AUC':>6} {'range':>15} {'consist':>8}  verdict")
    feats = measured.get("features") or {}
    for name in sorted(feats, key=lambda k: -(feats[k].get("mean_auc") or 0.0)):
        info = feats[name]
        if info.get("mean_auc") is None:
            lines.append(f"  {name:14} {'--':>6}                            {info['verdict']}")
            continue
        rng = f"{info['min_auc']:.3f}-{info['max_auc']:.3f}"
        lines.append(f"  {name:14} {info['mean_auc']:6.3f} {rng:>15} "
                     f"{info['direction_consistency']:8.2f}  {info['verdict']}")
    lines.append("")
    lines.append("  AUC is stated in the feature's ASSUMED direction: >0.5 means the literature")
    lines.append("  direction holds here, <0.5 means it is backwards on this sleeper's data.")
    w = proposed_weights(measured)
    lines.append("")
    lines.append("  proposed weights (0 = carries nothing measurable, drop from the fusion)")
    for name in sorted(w, key=lambda k: -w[k]):
        lines.append(f"    {name:14} {w[name]:.3f}   (was {_assumed_weight(name):.2f})")
    return "\n".join(lines)


def composite_evaluation(nights: Sequence[Tuple[str, Dict[str, object]]],
                         min_epochs: int = MIN_EPOCHS_PER_NIGHT) -> Dict[str, object]:
    """The autonomic channel as ONE score: how well it separates, and where its threshold belongs.

    WHY NOT JUST COMPARE THE FUSED DETECTOR TO THE REFERENCE. Because that comparison is rigged,
    and it is worth being explicit about how. ``SleepWakeDetector`` weights motion 1.0 and
    cardiac 0.6 with a threshold of 0.5, so when motion says wake the score is at least
    1.0/1.6 = 0.625 and when it says sleep the score is at most 0.6/1.6 = 0.375 -- the fusion
    reduces EXACTLY to its motion channel, and its motion channel is Cole-Kripke with Webster
    rescoring, which is the reference. Scored that way it returns accuracy 1.000 and kappa 1.000,
    a perfect agreement that measures nothing but an algorithm agreeing with itself.

    So this reports the two things that ARE answerable. AUC is threshold-free, so it says how
    much the composite score separates reference-wake from reference-sleep regardless of where
    the cut sits. The threshold sweep then says where that cut SHOULD sit -- and the gap between
    the two is the difference between "these features carry nothing" and "these features carry
    something and the fusion is miscalibrated", which the accuracy at a hand-set 0.5 cannot tell
    apart.
    """
    from sleepctl.ml.sleep_wake import SleepWakeDetector
    from sleepctl.eval.performance import epoch_by_epoch

    scores: List[float] = []
    is_wake: List[bool] = []
    per_night: List[Dict[str, object]] = []
    detector = SleepWakeDetector(use_autonomic=True)
    for label, night in nights:
        epochs = drop_out_of_bed(align_night(night))
        if len(epochs) < min_epochs:
            continue
        feats = [{k: e[k] for k in AUTONOMIC_FEATURE_NAMES if e.get(k) is not None}
                 for e in epochs]
        auto = detector._autonomic_scores(feats)
        wake = [not a for a in reference_labels(epochs)]
        pairs = [(s, w) for s, w in zip(auto, wake) if s is not None]
        if not pairs:
            continue
        night_auc = auc([s for s, _ in pairs], [w for _, w in pairs])
        if night_auc is None:
            # One class only -- the reference called the whole night sleep (or the whole night
            # wake). There is nothing to separate, so listing it as a night measured would
            # overstate how much evidence this rests on.
            continue
        per_night.append({"night": label, "auc": night_auc})
        scores.extend(s for s, _ in pairs)
        is_wake.extend(w for _, w in pairs)

    if not scores:
        return {"n_epochs": 0}
    best = None
    for i in range(5, 96):
        thr = i / 100.0
        # SLEEP is the positive class throughout `performance`, so both sides are inverted.
        res = epoch_by_epoch([s < thr for s in scores], [not w for w in is_wake])
        k = res.get("kappa")
        if k is not None and (best is None or k > best["kappa"]):
            best = {"threshold": thr, "kappa": k, "accuracy": res.get("accuracy"),
                    "specificity_wake": res.get("specificity")}
    return {"n_epochs": len(scores),
            "reference_wake_fraction": round(sum(1 for w in is_wake if w) / len(is_wake), 3),
            "composite_auc": auc(scores, is_wake),
            "per_night_auc": per_night,
            "best_threshold": best}
