"""Which epochs of a published night were actually spent in bed.

WHY THIS IS ITS OWN MODULE
--------------------------
Every offline analysis in this project reads ``raw_samples`` filtered only by
``controller_state != 'idle'`` -- and until bed exit could be detected at all (see
``sleepctl/controller/bed_exit.py``) a session ran on for hours after the sleeper got up. So
"the night" as published routinely contains a walking-around morning, and every comparison run
over it is scoring someone making breakfast as though it were a sleep record.

That is not a small correction. On 2026-08-27, 179 of 684 epochs are out-of-bed, and including
them takes the sleep/wake comparison against Cole-Kripke+Webster from

    accuracy 0.887, wake-specificity 0.574, kappa 0.513     (in bed)
    accuracy 0.711, wake-specificity 0.469, kappa 0.154     (as published)

It halves kappa. It is also the reason two runs of the same script over the same night could not
be reconciled: the answer depends entirely on whether the daytime hours were in the window.

This filter is NOT a way of making numbers look better, and the other nights are the evidence:
2026-08-29 barely moves (kappa 0.449 -> 0.450) and 2026-08-30 gets slightly WORSE
(0.408 -> 0.384). It removes epochs that answer a different question, in whichever direction
that happens to fall.

THE CRITERION
-------------
Heart rate, for the same reason ``bed_exit`` leans on it: on this hardware the movement index
read a flat 0.022 through an entire morning of walking around, LOWER than during sleep, so
motion cannot separate in-bed from out-of-bed here. A sustained rate at or above the lying
ceiling is not someone asleep, and it is not someone lying awake in bed either.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: Heart rate at or above which an epoch is treated as out of bed. Shared with
#: ``sleepctl.controller.bed_exit.BASELINE_MAX_HR`` and the autonomic calibration, so all three
#: draw the same line.
OUT_OF_BED_HR = 95.0


def minute_heart_rates(samples: Iterable[dict]) -> Dict[str, float]:
    """Peak heart rate per minute key (``ts[:16]``), over the samples given."""
    out: Dict[str, float] = {}
    for s in samples:
        hr = s.get("heart_rate")
        if hr is None:
            continue
        key = str(s.get("ts"))[:16]
        try:
            out[key] = max(out.get(key, 0.0), float(hr))
        except (TypeError, ValueError):
            continue
    return out


def in_bed_minutes(keys: Sequence[str], heart_rates: Dict[str, float],
                   ceiling: float = OUT_OF_BED_HR) -> List[str]:
    """The subset of ``keys`` whose heart rate does not say the sleeper was up.

    A minute with NO heart rate is KEPT. Absence of evidence is not evidence of being up, and
    dropping unmeasured epochs would quietly shrink every comparison toward the stretches where
    the sensor happened to be working.
    """
    return [k for k in keys if heart_rates.get(k, 0.0) < ceiling]


def split_in_bed(samples: Sequence[dict], keys: Sequence[str],
                 ceiling: float = OUT_OF_BED_HR) -> Tuple[List[str], List[str]]:
    """``(kept, dropped)`` minute keys, so a caller can REPORT what it excluded.

    Reporting the count is the point. A filter that silently removes a quarter of a night is
    indistinguishable from a bug in the analysis that uses it.
    """
    hrs = minute_heart_rates(samples)
    kept = in_bed_minutes(keys, hrs, ceiling)
    kept_set = set(kept)
    return kept, [k for k in keys if k not in kept_set]


def provenance(night: dict, kept: int, dropped: int) -> str:
    """A one-line description of exactly what was scored, for printing beside any metric.

    Two runs of the same comparison over the same night produced 456 and 684 epochs and could
    not be reconciled afterwards, because neither run recorded what it had scored. Any number
    quoted from this pipeline should carry its own denominator.
    """
    cap = night.get("sensor_capture") or {}
    return (f"night {night.get('night_date')}  "
            f"raw_samples={cap.get('n_samples')}  "
            f"hr_present={cap.get('heart_rate_present')}  "
            f"movement_present={cap.get('movement_present')}  "
            f"epochs scored={kept}  out-of-bed dropped={dropped}")


def out_of_bed_rows(rows: Sequence[dict], hr_key: str = "hr",
                    alt_key: Optional[str] = "hr_from_ibi",
                    ceiling: float = OUT_OF_BED_HR) -> List[dict]:
    """Row-shaped variant for analyses that carry heart rate on the row itself."""
    out = []
    for r in rows:
        vals = [r.get(hr_key)]
        if alt_key:
            vals.append(r.get(alt_key))
        vals = [v for v in vals if v is not None]
        if vals and max(float(v) for v in vals) >= ceiling:
            continue
        out.append(r)
    return out
