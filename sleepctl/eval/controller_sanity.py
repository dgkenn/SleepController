"""Controller sanity gate: does the DEPLOYED controller behave like a validated one?

This is gate 1 of the validation stack, and it is deliberately NOT a measure of sleep-staging
accuracy. It answers a narrower engineering question that needs no ground truth at all:

    Is the thing running on the bed tonight behaving like the thing that was validated?

That question is answerable from a night's own record, because the estimator ships with a
cross-validated report of how it behaves (``sleepctl/ml/sleep_staging/cv_report.json``, 25663
held-out PSG-labelled epochs). Two facts from it anchor everything here:

    * true stage flips per night      ~7.6
    * predicted flips, HMM-smoothed   ~2.7
    * predicted flips, unsmoothed     ~10.3

Measured on this deployment, 2026-08-23/24: **183 and 242 flips**. That is ~18x the UNSMOOTHED
model and ~70x the smoothed one, and normalising for sample rate does not rescue it (~1% per-sample
flip rate in CV vs ~16% live). So the deployed estimator does not behave like either validated
configuration, and its CV accuracy (66.7% / kappa 0.44 smoothed) cannot be assumed to transfer.

A controller can also fail this gate while its LABELS are fine: 31 of 36 consecutive thermal
interventions on 2026-08-24 were direction reversals, several within the same minute, because the
inferred state flipped every few minutes and each flip re-targeted the bed. A bed that hunts all
night is an engineering failure regardless of whether "rem" was really REM.

Physiological-plausibility checks are included for the same diagnostic purpose -- gross violations
(REM at half the night, deep sleep flat instead of front-loaded) indicate a broken estimator. They
are evidence about the CONTROLLER, not an endpoint the system optimises. Efficacy is judged by
morning outcomes, not by these.

Every threshold below is deliberately loose: this gate exists to catch gross engineering failure
with near-zero false-positive rate, not to police borderline nights.
"""

from __future__ import annotations

import statistics
from typing import Optional

#: Stage flips per hour of scored sleep. CV true rate is ~7.6 flips/night (~1.1/h over a 7 h
#: night); the unsmoothed model predicts ~10.3 (~1.5/h). Gate at 6/h -- over 5x the validated
#: true rate, so only gross flapping trips it. Live nights measured 19/h.
MAX_STAGE_FLIPS_PER_HOUR = 6.0

#: Median stage bout. Real sleep holds a stage for 15-30 min; measured live at 1-2 min.
MIN_MEDIAN_BOUT_MIN = 5.0

#: Thermal target reversals per hour. Note the existing oscillation guardrail cannot catch these:
#: it ignores reversals smaller than ``guardrail_oscillation_min_delta_f`` (0.75F), and the live
#: flips were all sub-threshold -- 31 reversals in one night, of which it flagged one.
MAX_THERMAL_REVERSALS_PER_HOUR = 2.0

#: Gross sleep-architecture bounds. Wide on purpose (population norms are REM 20-25%, deep 13-23%).
REM_FRACTION_RANGE = (0.10, 0.40)
DEEP_FRACTION_RANGE = (0.04, 0.35)

_SLEEP_STAGES = ("light", "deep", "rem")


def _durations_min(times) -> list:
    """Minutes each sample represents (gap to the next), capped so a dropout isn't credited."""
    out = []
    for i, t in enumerate(times):
        if i + 1 < len(times):
            d = (times[i + 1] - t).total_seconds() / 60.0
        else:
            d = 0.5
        out.append(max(0.0, min(d, 5.0)))
    return out


def _bouts(stages) -> list:
    """Contiguous runs of the same stage as (stage, n_samples)."""
    out, cur, n = [], None, 0
    for s in stages:
        if s != cur:
            if cur is not None:
                out.append((cur, n))
            cur, n = s, 1
        else:
            n += 1
    if cur is not None:
        out.append((cur, n))
    return out


def compute_controller_sanity(rows, interventions=None) -> dict:
    """Score one night's controller behaviour. ``rows`` are raw_samples-shaped mappings
    (ts, stage, controller_state, ...); ``interventions`` are intervention rows (ts, action).

    Returns a dict with the measured statistics, a per-check verdict, and an overall
    ``passed`` flag. Never raises: a night with too little data is reported ``insufficient``
    rather than failed, because "unmeasured" and "broken" must not look alike.
    """
    from datetime import datetime

    def _parse(v):
        if isinstance(v, datetime):
            return v
        try:
            return datetime.fromisoformat(str(v))
        except Exception:
            return None

    out: dict = {"checks": {}, "stats": {}, "passed": None, "insufficient": False}

    samples = []
    for r in rows or []:
        t = _parse(r["ts"] if not hasattr(r, "get") else r.get("ts"))
        if t is not None:
            samples.append((t, r))
    samples.sort(key=lambda x: x[0])

    staged = [(t, (r["stage"] or "unknown")) for t, r in samples
              if (r["stage"] or "unknown") in _SLEEP_STAGES + ("awake",)]
    if len(staged) < 20:
        out["insufficient"] = True
        out["reason"] = (f"only {len(staged)} staged samples -- not enough to judge controller "
                         "behaviour (this is UNMEASURED, not a failure)")
        return out

    times = [t for t, _ in staged]
    span_h = max((times[-1] - times[0]).total_seconds() / 3600.0, 1e-6)
    durs = _durations_min(times)

    # --- 1. stage flip rate -------------------------------------------------------------------
    bouts = _bouts([s for _, s in staged])
    flips = max(0, len(bouts) - 1)
    flips_per_h = flips / span_h
    out["stats"]["stage_flips"] = flips
    out["stats"]["stage_flips_per_hour"] = round(flips_per_h, 2)
    out["checks"]["flip_rate"] = {
        "ok": flips_per_h <= MAX_STAGE_FLIPS_PER_HOUR,
        "detail": (f"{flips} stage flips over {span_h:.1f} h = {flips_per_h:.1f}/h "
                   f"(gate {MAX_STAGE_FLIPS_PER_HOUR}/h; validated true rate ~1.1/h)"),
    }

    # --- 2. bout length -----------------------------------------------------------------------
    idx, bout_mins = 0, []
    for _, n in bouts:
        bout_mins.append(sum(durs[idx:idx + n]))
        idx += n
    med_bout = statistics.median(bout_mins) if bout_mins else 0.0
    out["stats"]["median_bout_min"] = round(med_bout, 1)
    out["stats"]["longest_bout_min"] = round(max(bout_mins), 1) if bout_mins else 0.0
    out["checks"]["bout_length"] = {
        "ok": med_bout >= MIN_MEDIAN_BOUT_MIN,
        "detail": (f"median stage bout {med_bout:.1f} min "
                   f"(gate >= {MIN_MEDIAN_BOUT_MIN}; real sleep holds 15-30 min)"),
    }

    # --- 3. sleep architecture ----------------------------------------------------------------
    mins: dict = {}
    for (t, s), d in zip(staged, durs):
        mins[s] = mins.get(s, 0.0) + d
    tst = sum(mins.get(k, 0.0) for k in _SLEEP_STAGES)
    rem_frac = (mins.get("rem", 0.0) / tst) if tst > 0 else 0.0
    deep_frac = (mins.get("deep", 0.0) / tst) if tst > 0 else 0.0
    out["stats"]["total_sleep_min"] = round(tst, 1)
    out["stats"]["rem_fraction"] = round(rem_frac, 3)
    out["stats"]["deep_fraction"] = round(deep_frac, 3)
    out["checks"]["rem_fraction"] = {
        "ok": REM_FRACTION_RANGE[0] <= rem_frac <= REM_FRACTION_RANGE[1],
        "detail": f"REM {100*rem_frac:.0f}% of sleep (gate {REM_FRACTION_RANGE}; norm 20-25%)",
    }
    out["checks"]["deep_fraction"] = {
        "ok": DEEP_FRACTION_RANGE[0] <= deep_frac <= DEEP_FRACTION_RANGE[1],
        "detail": f"deep {100*deep_frac:.0f}% of sleep (gate {DEEP_FRACTION_RANGE}; norm 13-23%)",
    }

    # --- 4. deep front-loading ----------------------------------------------------------------
    # SWS is heavily front-loaded in real sleep. A FLAT deep profile across the night means the
    # estimator isn't tracking sleep pressure at all -- measured 5.6% / 5.6% / 8.9% by third on
    # 2026-08-24, i.e. slightly INVERTED.
    third = max(1, len(staged) // 3)
    def _deep_frac(seg_idx):
        seg = staged[seg_idx[0]:seg_idx[1]]
        segd = durs[seg_idx[0]:seg_idx[1]]
        m: dict = {}
        for (t, s), d in zip(seg, segd):
            m[s] = m.get(s, 0.0) + d
        tot = sum(m.get(k, 0.0) for k in _SLEEP_STAGES)
        return (m.get("deep", 0.0) / tot) if tot > 0 else 0.0
    d1 = _deep_frac((0, third))
    d3 = _deep_frac((2 * third, len(staged)))
    out["stats"]["deep_fraction_first_third"] = round(d1, 3)
    out["stats"]["deep_fraction_last_third"] = round(d3, 3)
    out["checks"]["deep_front_loaded"] = {
        "ok": d1 >= d3,
        "detail": (f"deep {100*d1:.0f}% in first third vs {100*d3:.0f}% in last "
                   "(real SWS is front-loaded; flat/inverted means the estimator is not "
                   "tracking sleep pressure)"),
    }

    # --- 5. thermal reversals -----------------------------------------------------------------
    ivs = []
    for r in interventions or []:
        t = _parse(r["ts"] if not hasattr(r, "get") else r.get("ts"))
        a = r["action"] if not hasattr(r, "get") else r.get("action")
        if t is not None and a in ("cooler", "warmer"):
            ivs.append((t, a))
    ivs.sort(key=lambda x: x[0])
    reversals = sum(1 for i in range(1, len(ivs)) if ivs[i][1] != ivs[i - 1][1])
    if ivs:
        iv_span_h = max((ivs[-1][0] - ivs[0][0]).total_seconds() / 3600.0, 1e-6)
        rev_per_h = reversals / iv_span_h
    else:
        iv_span_h, rev_per_h = 0.0, 0.0
    out["stats"]["thermal_interventions"] = len(ivs)
    out["stats"]["thermal_reversals"] = reversals
    out["stats"]["thermal_reversals_per_hour"] = round(rev_per_h, 2)
    out["checks"]["thermal_stability"] = {
        "ok": rev_per_h <= MAX_THERMAL_REVERSALS_PER_HOUR,
        "detail": (f"{reversals} direction reversals in {len(ivs)} interventions over "
                   f"{iv_span_h:.1f} h = {rev_per_h:.1f}/h "
                   f"(gate {MAX_THERMAL_REVERSALS_PER_HOUR}/h)"),
    }

    out["passed"] = all(c["ok"] for c in out["checks"].values())
    out["failed_checks"] = [k for k, c in out["checks"].items() if not c["ok"]]
    return out


def format_report(sanity: dict, label: str = "") -> str:
    """Human-readable one-night report."""
    lines = [f"CONTROLLER SANITY{(' - ' + label) if label else ''}"]
    if sanity.get("insufficient"):
        lines.append(f"  INSUFFICIENT DATA: {sanity.get('reason')}")
        return "\n".join(lines)
    verdict = "PASS" if sanity["passed"] else "FAIL"
    lines.append(f"  verdict: {verdict}")
    for name, c in sanity["checks"].items():
        lines.append(f"    [{'ok  ' if c['ok'] else 'FAIL'}] {name}: {c['detail']}")
    return "\n".join(lines)
