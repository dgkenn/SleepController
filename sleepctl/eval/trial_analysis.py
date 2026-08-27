"""Analysis for the randomized controller-policy trial.

Estimates E[Y | policy] differences from a blinded n-of-1 trial, where Y is a morning outcome
rather than any property of the hypnogram. Deliberately conservative about what it claims:

* **Within-stratum only.** Arms were block-randomized inside ``night_type``, so comparisons are
  made inside each stratum and pooled with stratum weights. Pooling raw across strata would let
  a shift imbalance masquerade as a policy effect.
* **Primary endpoint is a raw component, not the z-composite.** z-scoring needs a reference
  distribution, and early in the trial that distribution is a handful of nights, so a z-composite
  is at its least stable exactly when it will be looked at most. Raw paired differences on one
  pre-specified component are stable from night one; the composite is reported as secondary.
* **No p-values.** Sequential looks at an accumulating n-of-1 trial with no alpha spending make
  a nominal p meaningless, and the honest quantity here is an effect size with an interval and a
  visible n. Reporting significance would invite exactly the over-reading the design is trying
  to avoid.
* **Carryover is surfaced, not silently assumed away.** Blocks limit it; the first night of each
  block is flagged so a carryover-sensitive re-analysis is possible.

Nothing here is a treatment recommendation. It reports what was measured, with the n attached.
"""

from __future__ import annotations

import math
import statistics
from typing import Optional

#: Pre-specified PRIMARY endpoint. Grogginess is the component most directly downstream of sleep
#: continuity and the one a bad night moves most reliably; fixing it in advance is what stops the
#: primary being chosen after the fact from whichever component happened to move.
PRIMARY_COMPONENT = "grogginess"

#: Higher is better for these; grogginess is inverted when forming the composite.
_POSITIVE = ("subjective_quality", "daytime_performance")
_NEGATIVE = ("grogginess",)


def _mean(xs):
    xs = [float(x) for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def _stdev(xs):
    xs = [float(x) for x in xs if x is not None]
    return statistics.stdev(xs) if len(xs) > 1 else None


def collect_nights(conn) -> list:
    """Join trial assignments to their morning outcomes. Only LOCKED nights are eligible --
    an unlocked night has either not been rated yet or was rated after the arm was revealed."""
    rows = conn.execute(
        "SELECT night_date, policy, block_id, block_index, night_type, controller_version,"
        " outcome_locked FROM trial_assignments ORDER BY night_date").fetchall()
    out = []
    for r in rows:
        r = dict(r)
        if not r.get("outcome_locked"):
            continue
        ctx = conn.execute(
            "SELECT subjective_quality, grogginess, daytime_performance FROM context "
            "WHERE date = ?", (r["night_date"],)).fetchone()
        if ctx is None:
            continue
        c = dict(ctx)
        if all(c.get(k) is None for k in
               ("subjective_quality", "grogginess", "daytime_performance")):
            continue
        r.update(c)
        r["is_block_start"] = (int(r.get("block_index") or 0) == 0)
        out.append(r)
    return out


def composite_y(night: dict, refs: dict) -> Optional[float]:
    """z(quality) - z(grogginess) + z(performance), against a supplied reference mean/sd.

    Returns None unless every component is present AND has a usable reference spread -- a
    composite silently built from one or two of three components is not the pre-registered
    endpoint and must not be passed off as it.
    """
    total = 0.0
    for key in _POSITIVE + _NEGATIVE:
        v = night.get(key)
        ref = refs.get(key) or {}
        mu, sd = ref.get("mean"), ref.get("sd")
        if v is None or mu is None or not sd:
            return None
        z = (float(v) - mu) / sd
        total += -z if key in _NEGATIVE else z
    return total


def _refs(nights) -> dict:
    out = {}
    for key in _POSITIVE + _NEGATIVE:
        vals = [n.get(key) for n in nights if n.get(key) is not None]
        out[key] = {"mean": _mean(vals), "sd": _stdev(vals), "n": len(vals)}
    return out


def analyze(conn, component: str = PRIMARY_COMPONENT) -> dict:
    """Per-arm outcome distributions and stratum-weighted contrasts against arm A."""
    from sleepctl.eval.trial import POLICY_A_STATIC

    nights = collect_nights(conn)
    refs = _refs(nights)
    out: dict = {
        "n_nights": len(nights),
        "component": component,
        "per_policy": {},
        "per_stratum": {},
        "contrasts": {},
        "refs": refs,
        "warnings": [],
    }
    if not nights:
        out["warnings"].append("no locked nights with outcomes yet -- nothing to analyze")
        return out

    for n in nights:
        n["_y"] = composite_y(n, refs)

    def _bucket(rows):
        vals = [r.get(component) for r in rows if r.get(component) is not None]
        ys = [r["_y"] for r in rows if r.get("_y") is not None]
        return {
            "n": len(rows),
            "n_with_component": len(vals),
            f"{component}_mean": _mean(vals),
            f"{component}_sd": _stdev(vals),
            "composite_mean": _mean(ys),
            "block_starts": sum(1 for r in rows if r.get("is_block_start")),
        }

    policies = sorted({n["policy"] for n in nights})
    for p in policies:
        out["per_policy"][p] = _bucket([n for n in nights if n["policy"] == p])

    strata = sorted({(n.get("night_type") or "other") for n in nights})
    for s in strata:
        rows = [n for n in nights if (n.get("night_type") or "other") == s]
        out["per_stratum"][s] = {p: _bucket([r for r in rows if r["policy"] == p])
                                 for p in sorted({r["policy"] for r in rows})}

    # Stratum-weighted contrast vs arm A. Weighted by stratum size so a stratum where one arm
    # happens to have more nights cannot dominate the pooled estimate.
    for p in policies:
        if p == POLICY_A_STATIC:
            continue
        num, den, pairs = 0.0, 0.0, 0
        for s in strata:
            a = [n.get(component) for n in nights
                 if n["policy"] == POLICY_A_STATIC
                 and (n.get("night_type") or "other") == s and n.get(component) is not None]
            b = [n.get(component) for n in nights
                 if n["policy"] == p
                 and (n.get("night_type") or "other") == s and n.get(component) is not None]
            if not a or not b:
                continue
            w = min(len(a), len(b))
            num += (_mean(b) - _mean(a)) * w
            den += w
            pairs += w
        if den > 0:
            out["contrasts"][f"{p}_vs_{POLICY_A_STATIC}"] = {
                "component": component,
                "diff": round(num / den, 3),
                "effective_pairs": pairs,
                "note": ("stratum-weighted difference in means; lower is better for grogginess. "
                         "No p-value on purpose -- see module docstring."),
            }

    # --- honesty checks -----------------------------------------------------------------------
    thin = [p for p, b in out["per_policy"].items() if b["n"] < 6]
    if thin:
        out["warnings"].append(
            f"arms with fewer than 6 nights: {thin} -- estimates are not yet interpretable")
    if len(strata) > 1:
        for s in strata:
            present = set(out["per_stratum"][s])
            if present != set(policies):
                out["warnings"].append(
                    f"stratum '{s}' is missing arms {sorted(set(policies) - present)}; "
                    "pooled contrasts exclude it")
    versions = {n.get("controller_version") for n in nights if n.get("controller_version")}
    if len(versions) > 1:
        out["warnings"].append(
            f"nights span {len(versions)} controller versions -- a code change inside the trial "
            "confounds the arms; consider analyzing per version")
    return out


def format_report(res: dict) -> str:
    lines = [f"TRIAL ANALYSIS  ({res['n_nights']} locked nights, primary = {res['component']})"]
    if not res["per_policy"]:
        lines += [f"  {w}" for w in res["warnings"]]
        return "\n".join(lines)
    lines.append(f"  {'arm':<16}{'n':>4}{'mean':>9}{'sd':>8}{'composite':>11}")
    comp = res["component"]
    for p, b in sorted(res["per_policy"].items()):
        m = b.get(f"{comp}_mean")
        sd = b.get(f"{comp}_sd")
        cm = b.get("composite_mean")
        lines.append(f"  {p:<16}{b['n']:>4}"
                     f"{('-' if m is None else f'{m:.2f}'):>9}"
                     f"{('-' if sd is None else f'{sd:.2f}'):>8}"
                     f"{('-' if cm is None else f'{cm:+.2f}'):>11}")
    if res["contrasts"]:
        lines.append("  contrasts:")
        for k, c in res["contrasts"].items():
            lines.append(f"    {k}: {c['diff']:+.3f} ({comp}), {c['effective_pairs']} pairs")
    for w in res["warnings"]:
        lines.append(f"  ! {w}")
    return "\n".join(lines)
