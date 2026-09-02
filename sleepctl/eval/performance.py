"""Standardized performance evaluation of a sleep tracker, per Menghini et al.

Menghini L, Cellini N, Goldstone A, Baker FC, de Zambotti M. "A standardized framework for
testing the performance of sleep-tracking technology: step-by-step guidelines and open-source
code." SLEEP 2021;44(2):zsaa170. doi:10.1093/sleep/zsaa170

This replaces an ad-hoc scheme this project had grown ("strong" vs "weak" disagreement counts),
which was an informal re-derivation of sensitivity and specificity, missing the summary-measure
and agreement analyses entirely. There is a standard; it has open-source reference code; and
using it makes our numbers comparable to every published validation instead of only to
themselves.

The framework prescribes three analyses, all implemented here:

  1. **Discrepancy analysis** -- device minus reference on the summary measures clinicians
     actually use (TST, SE, SOL, WASO), reported as bias with dispersion.
  2. **Bland-Altman agreement** -- bias, 95% limits of agreement (bias +- 1.96 SD), and a test
     for PROPORTIONAL bias (does the error grow with the magnitude being measured?). A mean bias
     near zero with wide limits is a common and dangerous pattern: it looks unbiased on average
     while being unreliable on any individual night.
  3. **Epoch-by-epoch analysis** -- accuracy, sensitivity, specificity, PPV, NPV and Cohen's
     kappa, with SLEEP as the positive class by the framework's convention.

Why sensitivity and specificity rather than a single accuracy: on a night that is 90% sleep, a
detector that never reports wake at all scores 90% accuracy. Actigraphy's documented failure is
exactly that shape -- high sensitivity, poor specificity -- so accuracy alone systematically
flatters the thing we most need to measure. Specificity IS wake detection, which is what this
controller acts on.

The reference need not be PSG. Menghini explicitly permits "other reference methods", and Weaver
et al. (Sleep Health 2023;9:417-429, doi:10.1016/j.sleh.2023.04.005) found a published algorithm
over raw accelerometry reaches 84-86% accuracy against PSG across three consumer devices -- which
is what makes ``eval/reference_stagers`` a legitimate reference here.

Pure standard library.
"""

from __future__ import annotations

import math
import statistics
from typing import Dict, List, Optional, Sequence, Tuple

#: Epoch length assumed when converting counts of epochs to minutes.
DEFAULT_EPOCH_MIN = 1.0


def epoch_by_epoch(device_sleep: Sequence[Optional[bool]],
                   reference_sleep: Sequence[Optional[bool]]) -> Dict[str, Optional[float]]:
    """Epoch-by-epoch agreement, SLEEP as the positive class (Menghini section 3.3).

    Epochs where either side has no label are excluded rather than counted as agreement --
    scoring a sensor dropout as a correct call is how an outage becomes a perfect night.
    """
    pairs = [(d, r) for d, r in zip(device_sleep, reference_sleep)
             if d is not None and r is not None]
    n = len(pairs)
    if not n:
        return {"n_epochs": 0}
    tp = sum(1 for d, r in pairs if d and r)          # both sleep
    tn = sum(1 for d, r in pairs if not d and not r)  # both wake
    fp = sum(1 for d, r in pairs if d and not r)      # device says sleep, reference says wake
    fn = sum(1 for d, r in pairs if not d and r)      # device says wake, reference says sleep

    def _ratio(num, den):
        return round(num / den, 4) if den else None

    acc = (tp + tn) / n
    # Cohen's kappa: chance-corrected, which raw accuracy is not. On a 90%-sleep night an
    # all-sleep detector scores 0.90 accuracy and 0.0 kappa, and the second number is the honest
    # one.
    po = acc
    p_yes = ((tp + fp) / n) * ((tp + fn) / n)
    p_no = ((tn + fn) / n) * ((tn + fp) / n)
    pe = p_yes + p_no
    kappa = (po - pe) / (1 - pe) if (1 - pe) > 1e-12 else None
    return {
        "n_epochs": n,
        "accuracy": round(acc, 4),
        # sensitivity = ability to detect SLEEP; specificity = ability to detect WAKE, which is
        # what this controller acts on and where actigraphy is documented to be weak.
        "sensitivity": _ratio(tp, tp + fn),
        "specificity": _ratio(tn, tn + fp),
        "ppv": _ratio(tp, tp + fp),
        "npv": _ratio(tn, tn + fn),
        "kappa": round(kappa, 4) if kappa is not None else None,
        "tp_both_sleep": tp, "tn_both_wake": tn,
        "fp_device_sleep_ref_wake": fp, "fn_device_wake_ref_sleep": fn,
    }


def summary_measures(sleep: Sequence[Optional[bool]],
                     epoch_min: float = DEFAULT_EPOCH_MIN) -> Dict[str, Optional[float]]:
    """The four summary measures the framework compares: TST, SE, SOL and WASO (minutes/%)."""
    labelled = [s for s in sleep if s is not None]
    if not labelled:
        return {"tst_min": None, "se_pct": None, "sol_min": None, "waso_min": None}
    tib = len(labelled) * epoch_min
    tst = sum(1 for s in labelled if s) * epoch_min
    # SOL: epochs before the first sleep epoch.
    sol = 0.0
    for s in labelled:
        if s:
            break
        sol += epoch_min
    # WASO: wake epochs AFTER the first sleep epoch (and before the last one).
    try:
        first = labelled.index(True)
        last = len(labelled) - 1 - labelled[::-1].index(True)
        waso = sum(1 for s in labelled[first:last + 1] if not s) * epoch_min
    except ValueError:
        waso = 0.0
    return {
        "tst_min": round(tst, 1),
        "se_pct": round(100.0 * tst / tib, 1) if tib else None,
        "sol_min": round(sol, 1),
        "waso_min": round(waso, 1),
    }


def discrepancy(device: Dict[str, Optional[float]],
                reference: Dict[str, Optional[float]]) -> Dict[str, Optional[float]]:
    """Device minus reference for each summary measure (Menghini section 3.1)."""
    out: Dict[str, Optional[float]] = {}
    for k in ("tst_min", "se_pct", "sol_min", "waso_min"):
        d, r = device.get(k), reference.get(k)
        out[k] = round(d - r, 1) if (d is not None and r is not None) else None
    return out


def bland_altman(differences: Sequence[float],
                 means: Optional[Sequence[float]] = None) -> Dict[str, Optional[float]]:
    """Bias, 95% limits of agreement, and proportional bias (Menghini section 3.2).

    The limits matter more than the bias. A mean bias near zero with wide limits is the
    dangerous, common pattern: unbiased on average, unreliable on any individual night -- and a
    single night is exactly the unit this controller acts on.

    ``means`` (the per-pair average of device and reference) enables the proportional-bias test:
    a non-zero slope of difference-on-mean says the error grows with what is being measured, so
    a single bias figure does not describe the device.
    """
    diffs = [float(d) for d in differences if d is not None]
    n = len(diffs)
    if n < 2:
        return {"n": n, "bias": round(diffs[0], 2) if n else None,
                "loa_lower": None, "loa_upper": None, "proportional_bias_slope": None}
    bias = statistics.fmean(diffs)
    sd = statistics.stdev(diffs)
    out = {
        "n": n,
        "bias": round(bias, 2),
        "sd": round(sd, 2),
        "loa_lower": round(bias - 1.96 * sd, 2),
        "loa_upper": round(bias + 1.96 * sd, 2),
        "proportional_bias_slope": None,
    }
    if means is not None:
        ms = [float(m) for m in means if m is not None]
        if len(ms) == n and n >= 3:
            mbar = statistics.fmean(ms)
            dbar = bias
            sxx = sum((m - mbar) ** 2 for m in ms)
            if sxx > 1e-12:
                sxy = sum((m - mbar) * (d - dbar) for m, d in zip(ms, diffs))
                out["proportional_bias_slope"] = round(sxy / sxx, 4)
    return out


def evaluate(device_sleep: Sequence[Optional[bool]],
             reference_sleep: Sequence[Optional[bool]],
             epoch_min: float = DEFAULT_EPOCH_MIN) -> Dict[str, object]:
    """One night, all three analyses. Multi-night Bland-Altman needs `bland_altman` directly."""
    dev = summary_measures(device_sleep, epoch_min)
    ref = summary_measures(reference_sleep, epoch_min)
    return {
        "epoch_by_epoch": epoch_by_epoch(device_sleep, reference_sleep),
        "device_summary": dev,
        "reference_summary": ref,
        "discrepancy": discrepancy(dev, ref),
    }


def format_report(res: Dict[str, object], label: str = "", reference: str = "reference") -> str:
    """Human-readable rendering, in the framework's own order."""
    ebe = res.get("epoch_by_epoch") or {}
    if not ebe.get("n_epochs"):
        return f"PERFORMANCE vs {reference}{f' - {label}' if label else ''}\n  no comparable epochs"
    lines = [f"PERFORMANCE vs {reference}{f' - {label}' if label else ''}",
             f"  epoch-by-epoch ({ebe['n_epochs']} epochs)",
             f"    accuracy    {ebe['accuracy']}      kappa {ebe['kappa']}",
             f"    sensitivity {ebe['sensitivity']}  (detecting SLEEP)",
             f"    specificity {ebe['specificity']}  (detecting WAKE -- what the controller acts on)",
             f"    PPV {ebe['ppv']}   NPV {ebe['npv']}"]
    dev, ref, disc = (res.get("device_summary") or {}, res.get("reference_summary") or {},
                      res.get("discrepancy") or {})
    lines.append("  summary measures (device / reference / discrepancy)")
    for k, unit in (("tst_min", "min"), ("se_pct", "%"), ("sol_min", "min"), ("waso_min", "min")):
        lines.append(f"    {k:9} {dev.get(k)} / {ref.get(k)} / "
                     f"{disc.get(k):+} {unit}" if disc.get(k) is not None
                     else f"    {k:9} {dev.get(k)} / {ref.get(k)} / n/a")
    return "\n".join(lines)


def reference_discriminability(counts: Sequence[float],
                               reference_sleep: Sequence[Optional[bool]]) -> Dict[str, object]:
    """How much MOTION evidence the reference's own wake calls actually rest on.

    WHY A COMPARISON NEEDS THIS. ``calibrate_scale`` fits the count scale so a night reaches a
    target sleep fraction, which forces roughly 12% of epochs to be scored wake whether or not
    any of them contain motion. On a night whose movement distribution is compressed -- and this
    sleeper's are: median 27, p95 103, on a scale that saturates at 1000 -- the threshold lands
    inside the noise, and the reference labels wake on epochs indistinguishable from its own
    sleep epochs.

    Measured on 2026-08-27 and 2026-08-30, roughly ONE THIRD of the reference's wake epochs sit
    at or below the night's median movement. So a third of the epochs where we "disagree" are
    epochs where the reference has no motion basis for its call either. That does not make our
    number better; it makes the comparison less informative than a bare kappa implies, and a
    kappa quoted without it overstates what was established.
    """
    pairs = [(c, r) for c, r in zip(counts, reference_sleep) if r is not None]
    if not pairs:
        return {"n": 0}
    vals = sorted(c for c, _ in pairs)
    med = vals[len(vals) // 2]
    wake = [c for c, r in pairs if not r]
    if not wake:
        return {"n": len(pairs), "reference_wake_epochs": 0}
    quiet = sum(1 for c in wake if c <= med)
    p95 = vals[min(len(vals) - 1, int(0.95 * len(vals)))]
    return {
        "n": len(pairs),
        "reference_wake_epochs": len(wake),
        # The headline: wake calls with no more motion than a typical sleeping minute.
        "wake_calls_at_or_below_median_motion": quiet,
        "wake_calls_without_motion_evidence_frac": round(quiet / len(wake), 3),
        "motion_median": round(med, 1),
        "motion_p95": round(p95, 1),
        # A compressed distribution is what makes the threshold arbitrary in the first place.
        "motion_dynamic_range": round(p95 / med, 1) if med > 0 else None,
    }
