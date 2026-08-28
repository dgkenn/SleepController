"""Published, externally-validated actigraphy sleep/wake algorithms, for comparison against ours.

WHY THESE AND NOT A BETTER MODEL
--------------------------------
The question is not "what is the most accurate stager" -- it is "where might ours be blind?".
Answering that needs a reference that was validated against POLYSOMNOGRAPHY by someone else, on
someone else's subjects, and that fails DIFFERENTLY from ours. Two algorithms fit:

  * **Cole-Kripke** (Cole et al., Sleep 1992, PMID 1455130) -- a weighted sum over a window of
    activity counts, validated against PSG at ~88% epoch agreement. The reference implementation
    every actigraphy package ships.
  * **Sadeh** (Sadeh et al., Sleep 1994, PMID 7973touch) -- a logistic discriminant over the mean,
    standard deviation, count of moderate-activity epochs and log of the current epoch. Developed
    on a different population and using different features, so it disagrees with Cole-Kripke in
    informative places.

Both consume ONE-MINUTE ACTIVITY COUNTS, which is exactly what the Polar PMD accelerometer gives
us (PIM per epoch) -- the same modality and units they were built for. That is what makes this a
real external check rather than a model grading its own homework: our stager is HR-led, these are
motion-only, so agreement is meaningful and disagreement localises the blind spot.

WHAT THEY CAN AND CANNOT SAY
----------------------------
They score SLEEP vs WAKE only -- no stages. Actigraphy's well-documented weakness is specificity:
it calls quiet wakefulness "sleep", so a disagreement where they say sleep and we say wake is
weak evidence against us, while the reverse (they see movement, we say asleep) is strong. The
comparison is reported with that asymmetry rather than as a single accuracy number, because a
symmetric score would flatter us in exactly the direction actigraphy is known to be wrong.

Pure functions over count sequences; no I/O, no model files.
"""

from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence

#: Cole-Kripke weights for the "optimal" 1-minute formulation: four minutes before, the current
#: minute, and two after, scaled by P. Sleep when the score is < 1.0.
_CK_P = 0.00001
_CK_W = (106.0, 54.0, 58.0, 76.0, 230.0, 74.0, 67.0)   # A-4 A-3 A-2 A-1 A0 A+1 A+2
_CK_LEAD = 4
_CK_LAG = 2

#: Sadeh window: 5 minutes before through 5 after (11 epochs centred on the current one).
_SADEH_HALF = 5
_SADEH_NAT_LO, _SADEH_NAT_HI = 50.0, 100.0   # "moderate activity" band for the NAT term


def _clip_counts(counts: Iterable[float]) -> List[float]:
    return [max(0.0, float(c or 0.0)) for c in counts]


def cole_kripke(counts: Sequence[float], scale: float = 1.0) -> List[bool]:
    """Sleep/wake per epoch by Cole-Kripke. True = ASLEEP.

    ``scale`` maps our counts onto the count units the weights were fitted for. It is exposed
    rather than hard-coded because it is the one genuinely uncertain step: PIM from a Polar
    armband is not the same unit as a 1990s Actillume, and pretending otherwise would silently
    bias every comparison. ``calibrate_scale`` estimates it from data instead of guessing.
    """
    a = _clip_counts(counts)
    n = len(a)
    out: List[bool] = []
    for i in range(n):
        acc = 0.0
        for k, w in enumerate(_CK_W):
            j = i + (k - _CK_LEAD)
            if 0 <= j < n:
                acc += w * (a[j] * scale)
        out.append((_CK_P * acc) < 1.0)
    return out


def sadeh(counts: Sequence[float], scale: float = 1.0) -> List[bool]:
    """Sleep/wake per epoch by the Sadeh discriminant. True = ASLEEP."""
    a = [c * scale for c in _clip_counts(counts)]
    n = len(a)
    out: List[bool] = []
    for i in range(n):
        lo, hi = max(0, i - _SADEH_HALF), min(n, i + _SADEH_HALF + 1)
        win = a[lo:hi]
        mean = sum(win) / len(win)
        if len(win) > 1:
            mu = mean
            sd = math.sqrt(sum((x - mu) ** 2 for x in win) / (len(win) - 1))
        else:
            sd = 0.0
        nat = sum(1 for x in win if _SADEH_NAT_LO <= x < _SADEH_NAT_HI)
        logact = math.log(1.0 + a[i])
        psa = 7.601 - 0.065 * mean - 1.08 * nat - 0.056 * sd - 0.703 * logact
        out.append(psa > 0.0)
    return out


def calibrate_scale(counts: Sequence[float], target_sleep_fraction: float = 0.88,
                    algorithm=None) -> float:
    """Estimate the count-unit scale by matching an algorithm's overall sleep fraction.

    The alternative -- assuming our PIM equals a 1990s device's counts -- is an unstated
    assumption that would bias every downstream comparison in an unknown direction. Matching a
    plausible whole-night sleep fraction fixes the free parameter using the night's own data,
    so the comparison is about WHERE the algorithms disagree rather than about a unit guess.

    **Each algorithm needs its OWN scale.** Cole-Kripke is a linear weighted sum, so scaling the
    counts scales its score; Sadeh mixes a linear mean, a standard deviation, a LOG term and a
    count of epochs falling inside an absolute 50-100 band, and those respond to a scale change
    in four different ways. Fitting one scale on Cole-Kripke and reusing it for Sadeh produced
    69% "strong disagreement" and put the two references at odds with each other on 67% of
    minutes -- an artifact of the shared scale, not a finding about either algorithm.

    Returns 1.0 when there is nothing to fit.
    """
    a = _clip_counts(counts)
    if not a or max(a) <= 0:
        return 1.0
    algo = algorithm or cole_kripke
    lo, hi = 1e-4, 1e4
    for _ in range(40):
        mid = math.sqrt(lo * hi)
        frac = sum(algo(a, scale=mid)) / len(a)
        if frac > target_sleep_fraction:
            lo = mid          # too much sleep -> counts need to weigh MORE
        else:
            hi = mid
    return math.sqrt(lo * hi)


def compare(ours_asleep: Sequence[Optional[bool]], counts: Sequence[float],
            scale: Optional[float] = None) -> dict:
    """Agreement between our sleep/wake calls and both references, reported ASYMMETRICALLY.

    ``ours_asleep`` may contain None for epochs we did not label; those are excluded rather than
    counted as agreement.
    """
    # One scale PER ALGORITHM -- see calibrate_scale. A shared scale is not a simplification,
    # it is a bug that manufactures disagreement.
    ck_scale = scale if scale is not None else calibrate_scale(counts, algorithm=cole_kripke)
    sd_scale = scale if scale is not None else calibrate_scale(counts, algorithm=sadeh)
    ck = cole_kripke(counts, scale=ck_scale)
    sd = sadeh(counts, scale=sd_scale)
    idx = [i for i, v in enumerate(ours_asleep) if v is not None and i < len(ck)]
    if not idx:
        return {"n": 0, "scale": scale}

    def _agree(ref):
        both_sleep = sum(1 for i in idx if ours_asleep[i] and ref[i])
        both_wake = sum(1 for i in idx if not ours_asleep[i] and not ref[i])
        # The asymmetry that matters: actigraphy's known failure is calling quiet wake "sleep",
        # so "reference sees MOVEMENT while we say asleep" is the strong disagreement.
        ref_wake_we_sleep = sum(1 for i in idx if ours_asleep[i] and not ref[i])
        we_wake_ref_sleep = sum(1 for i in idx if not ours_asleep[i] and ref[i])
        n = len(idx)
        return {
            "agreement": round((both_sleep + both_wake) / n, 3),
            "both_sleep": both_sleep, "both_wake": both_wake,
            # STRONG disagreement: independent motion evidence of wake that we scored as sleep.
            "missed_wake_we_called_sleep": ref_wake_we_sleep,
            # WEAK disagreement: we called wake where motion was quiet. Actigraphy cannot see
            # quiet wakefulness, so this is the direction in which WE may well be right.
            "we_called_wake_ref_quiet": we_wake_ref_sleep,
        }

    return {"n": len(idx), "scale_cole_kripke": round(ck_scale, 6),
            "scale_sadeh": round(sd_scale, 6),
            "cole_kripke": _agree(ck), "sadeh": _agree(sd),
            # Where the two references disagree with EACH OTHER, neither is authoritative and our
            # label cannot be judged. Reporting it stops a coin-flip epoch counting as evidence.
            "references_disagree": sum(1 for i in idx if ck[i] != sd[i])}
