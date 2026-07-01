"""Standing "does the controller help?" trial (the closed-loop's own n-of-1 efficacy audit).

The backtest (``sleepctl.eval.backtest``) proves the controller beats no-control in SIMULATION.
This module runs the equivalent comparison on the user's REAL nights: every night is assigned one
of two arms —

  * ``controlled`` — the normal closed loop: learned setpoint, in-night thermal steering,
    predictive pre-emption, all active.
  * ``held``       — a strictly do-no-harm fixed-neutral baseline: experimental steering and
    preemption are switched OFF and the setpoint is held at the tunables' neutral value, but the
    safety clamps and smart-wake alarm are UNCHANGED (the user is never put at risk or made to
    oversleep to "win" the comparison).

Over enough nights, ``analyze_efficacy`` compares wake_events / deep% / efficiency across arms
using the SAME stats machinery as the n-of-1 experiment engine (``sleepctl.experiments._stats``) —
a plain paired-groups comparison here (no crossover cycles: this is a long-running background
audit, not a scheduled experiment), reported with an unpaired Welch-style 95% CI on the mean
difference so "not enough data" is honest instead of implied.

The trial is OFF by default (opt-in via ``/efficacy/config``): this is instrumentation to convince
a skeptical user the loop is actually earning its keep, not a knob a first-time install should be
running blind.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import List, Optional

from sleepctl.experiments import _stats  # reuse the SAME mean/sd/n helper as the n-of-1 engine

_ARMS = ("controlled", "held")
# Washout: once an arm is assigned, hold it for a minimum block of nights before flipping, so
# short-run noise / carryover doesn't dominate the comparison (same rationale as the experiment
# engine's washout_nights, just simpler: no crossover cycles, just a randomized block schedule).
_DEFAULT_BLOCK_NIGHTS = 3

_METRICS = ("wake_events", "deep_pct", "efficiency")
_LOWER_BETTER = {"wake_events"}


def _today_str(d: Optional[date] = None) -> str:
    return (d or datetime.now().date()).isoformat()


# --------------------------------------------------------------------------- config (engine table)


def _get_config(repo) -> dict:
    row = repo.conn.execute(
        "SELECT enabled, block_nights FROM efficacy_config WHERE id=1").fetchone()
    if row is None:
        return {"enabled": False, "block_nights": _DEFAULT_BLOCK_NIGHTS}  # opt-in: defaults OFF
    return {"enabled": bool(row["enabled"]), "block_nights": int(row["block_nights"])}


def get_efficacy_config(repo) -> dict:
    return _get_config(repo)


def set_efficacy_config(repo, values: dict) -> dict:
    cur = _get_config(repo)
    if "enabled" in values and values["enabled"] is not None:
        cur["enabled"] = bool(values["enabled"])
    if "block_nights" in values and values["block_nights"] is not None:
        cur["block_nights"] = max(1, int(values["block_nights"]))
    repo.conn.execute(
        "INSERT INTO efficacy_config (id, enabled, block_nights) VALUES (1,?,?) "
        "ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled, "
        "block_nights=excluded.block_nights",
        (int(cur["enabled"]), cur["block_nights"]),
    )
    repo.conn.commit()
    return cur


# --------------------------------------------------------------------------- arm assignment


def _last_assignment(repo, before_date: str) -> Optional[dict]:
    """The most recent efficacy_nights row strictly before ``before_date`` (by date), or None."""
    row = repo.conn.execute(
        "SELECT night_date, arm FROM efficacy_nights WHERE night_date < ? "
        "ORDER BY night_date DESC LIMIT 1",
        (before_date,),
    ).fetchone()
    return dict(row) if row else None


def _block_run_length(repo, arm: str, before_date: str) -> int:
    """How many consecutive nights immediately before ``before_date`` already carry ``arm``
    (walks back day-by-day so gaps in the ledger don't understate the run)."""
    n = 0
    rows = repo.conn.execute(
        "SELECT night_date, arm FROM efficacy_nights WHERE night_date < ? "
        "ORDER BY night_date DESC LIMIT 60",
        (before_date,),
    ).fetchall()
    cursor = datetime.fromisoformat(before_date).date()
    for r in rows:
        cursor = cursor - timedelta(days=1)
        if r["night_date"] != cursor.isoformat() or r["arm"] != arm:
            break
        n += 1
    return n


def assign_tonight_arm(repo, cfg=None, night_date: Optional[str] = None) -> str:
    """Assign (and persist) tonight's efficacy arm: 'controlled' | 'held'.

    Balanced + randomized WITH a washout/min-hold rule: the arm is assigned in blocks of
    ``block_nights`` consecutive nights (default 3) rather than flipping every night, so a lone
    bad/good night can't be laundered into "the arm did it." Once a block completes, the next
    block's arm is chosen by simple alternation seeded off a hash of the date (so it's
    deterministic/reproducible, not dependent on call order or external RNG state), which keeps
    long-run assignment balanced 50/50 between arms.

    Idempotent per date: re-calling for the same night returns the already-persisted arm instead
    of re-randomizing it (a night's arm must not change once assigned).
    """
    econf = _get_config(repo)
    date_str = night_date or _today_str()

    existing = repo.conn.execute(
        "SELECT arm FROM efficacy_nights WHERE night_date=?", (date_str,)
    ).fetchone()
    if existing:
        return existing["arm"]

    if not econf["enabled"]:
        return None  # standing trial is opt-in; callers should treat None as "trial inactive"

    block = max(1, int(econf["block_nights"]))
    prev = _last_assignment(repo, date_str)
    if prev is None:
        # First night ever assigned: seed off the date so different installs don't all start
        # identically, but a given DB is still reproducible.
        import hashlib
        h = int(hashlib.sha256(date_str.encode()).hexdigest(), 16)
        arm = _ARMS[h % 2]
    else:
        run = _block_run_length(repo, prev["arm"], date_str)
        if run + 1 <= block:
            arm = prev["arm"]  # still inside the min-hold block: keep the same arm
        else:
            arm = _ARMS[1 - _ARMS.index(prev["arm"])]  # block complete: flip

    repo.assign_efficacy_night(date_str, arm)
    return arm


# --------------------------------------------------------------------------- HELD-arm application


def neutral_setpoint(base_profile, cfg):
    """A strictly do-no-harm fixed-neutral SetpointProfile for a HELD night: the same neutral_f
    tunable everyone starts from, all thermal STEERING biases zeroed out (no deep-bias cooling, no
    REM-warm offset, no wake-ramp beyond the plain neutral) — but still a valid, clamped profile
    the existing thermal controller renders exactly like any other night. Keeps
    ``composite_bed_weight`` (a comfort blend, not an experimental steering behavior)."""
    from dataclasses import replace

    t = cfg.tunables
    return replace(
        base_profile,
        neutral_f=t.neutral_temp_f,
        deep_bias_f=0.0,
        rem_warm_offset_f=0.0,
        wake_ramp_f=t.neutral_temp_f,
    )


_PREEMPT_DISABLED_THRESHOLD = 1.01  # above the max possible score (1.0): never fires


def apply_efficacy_arm(repo, cfg, controller, date_str: str, base_profile):
    """Apply tonight's assigned arm on top of the learned setpoint. Returns
    ``(profile_for_tonight, arm_info)`` where ``arm_info`` is None when the trial is inactive.

    On a HELD night this disables the EXPERIMENTAL levers only (steering + predictive
    preemption), reusing the controller's EXISTING setters/attributes — never the safety clamps
    and never smart-wake, which is why this is do-no-harm: worst case the user gets a night
    identical to "system not installed", never a night worse than that.

      * in-night thermal steering (deepen / REM-warm nudges): ``set_steer_policy(actuate=False)``
        — an existing setter (also used by the deepening-response learner's own control nights).
      * predictive pre-emption: both preempt gates (``wake_risk_assessor.preempt_threshold`` and
        ``precursor_detector.preempt_threshold``) are plain instance attributes the controller
        already exposes; raising them above the maximum possible score (1.0) makes
        ``score >= threshold`` always False, without touching controller.py's decision logic.

    Both preempt gates are EXPLICITLY reset to their configured default on a CONTROLLED night too
    (not just left alone), because ``PrecursorDetector``/``WakeRiskAssessor`` are constructed once
    at controller start-up and would otherwise silently carry a previous HELD night's disabled
    threshold forward forever.
    """
    t = cfg.tunables
    arm = assign_tonight_arm(repo, cfg, night_date=date_str)
    if arm is None:
        return base_profile, None
    wra = getattr(controller, "wake_risk_assessor", None)
    pd = getattr(controller, "precursor_detector", None)
    if arm == "held":
        profile = neutral_setpoint(base_profile, cfg)
        controller.set_steer_policy(actuate=False)  # no experimental deepen/REM-warm nudges
        if wra is not None:
            wra.preempt_threshold = _PREEMPT_DISABLED_THRESHOLD
        if pd is not None:
            pd.preempt_threshold = _PREEMPT_DISABLED_THRESHOLD
        return profile, {"arm": arm, "applied": True}
    # controlled: restore the real thresholds (undo any prior HELD night's override)
    if wra is not None:
        wra.preempt_threshold = getattr(t, "wake_risk_preempt_threshold", 0.5)
    if pd is not None:
        pd.preempt_threshold = getattr(t, "precursor_preempt_threshold", 0.40)
    return base_profile, {"arm": arm, "applied": False}


# --------------------------------------------------------------------------- outcome recording


def record_efficacy_outcome(repo, night_date: str, wake_events=None, deep_pct=None,
                            efficiency=None, outcome_score=None) -> None:
    """Persist tonight's measured outcome against its already-assigned arm. No-op (does not
    create a row) if the night was never assigned an arm — the trial being off/newly-enabled
    must not silently start recording untracked nights."""
    if repo.efficacy_night(night_date) is None:
        return
    repo.record_efficacy_outcome(night_date, wake_events=wake_events, deep_pct=deep_pct,
                                 efficiency=efficiency, outcome_score=outcome_score)


def _night_metrics(ns) -> dict:
    total = ns.total_sleep_min
    deep_pct = (ns.deep_min / total) if (ns.deep_min is not None and total) else None
    return {
        "wake_events": ns.wake_events,
        "deep_pct": deep_pct,
        "efficiency": ns.sleep_efficiency,
        "outcome_score": ns.outcome_score,
    }


def backfill_from_nightly_summaries(repo) -> int:
    """Resolve unresolved efficacy_nights rows by joining ``nightly_summaries`` on date. This is
    the fallback path when wiring outcome-recording into the daemon's close-out is awkward (or for
    nights that predate that wiring): every unresolved row gets its metrics filled in from the
    matching night's summary, if one exists yet. Returns the count resolved."""
    pending = [r for r in repo.efficacy_rows(resolved_only=False) if not r.get("resolved")]
    resolved = 0
    for r in pending:
        night_date = r["night_date"]
        ns_row = repo.conn.execute(
            "SELECT * FROM nightly_summaries WHERE date=?", (night_date,)
        ).fetchone()
        if ns_row is None:
            continue
        ns = repo._row_to_night(ns_row)
        m = _night_metrics(ns)
        repo.record_efficacy_outcome(night_date, wake_events=m["wake_events"],
                                     deep_pct=m["deep_pct"], efficiency=m["efficiency"],
                                     outcome_score=m["outcome_score"])
        resolved += 1
    return resolved


def efficacy_rows(repo, resolved_only: bool = True) -> List[dict]:
    return repo.efficacy_rows(resolved_only=resolved_only)


# --------------------------------------------------------------------------- analysis


def _welch_ci(a: List[float], b: List[float]):
    """95% CI + a rough p-value for the mean(b) - mean(a) difference (Welch's t, unequal
    variance) — the standard unpaired two-sample comparison. Returns (diff, ci, p) with None
    entries when there isn't enough data to compute them."""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return (None, None, None)
    ma, mb = sum(a) / na, sum(b) / nb
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    se2 = va / na + vb / nb
    if se2 <= 0:
        return (round(mb - ma, 3), None, None)
    se = se2 ** 0.5
    diff = mb - ma
    # Welch-Satterthwaite df, then a normal-approx p-value (no scipy dependency here, matching
    # the existing experiments.py style of a hand-rolled, dependency-free CI).
    df = se2 ** 2 / ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
    t = diff / se
    p = _approx_two_sided_p(abs(t), df)
    ci = [round(diff - 1.96 * se, 3), round(diff + 1.96 * se, 3)]
    return (round(diff, 3), ci, round(p, 4))


def _approx_two_sided_p(t: float, df: float) -> float:
    """A dependency-free two-sided p-value approximation for a t-statistic: for df >= ~30 the
    t-distribution is close enough to normal that the standard normal survival function is a
    reasonable stand-in, which keeps this module scipy-free like ``experiments.py``."""
    import math
    # Normal-approximation p-value via the complementary error function (exact for the normal,
    # a slight over-estimate of significance at very low df, which the "enough_data" gate below
    # already guards against by requiring a minimum n per arm).
    z = t
    p = math.erfc(z / math.sqrt(2))
    return max(0.0, min(1.0, p))


def analyze_efficacy(repo, min_n_per_arm: int = 5) -> dict:
    """Compare CONTROLLED vs HELD nights on wake_events / deep% / efficiency, with the same
    n/mean/sd stats helper the n-of-1 experiment engine uses, plus a Welch CI + p-value per
    metric. Returns a verdict string summarizing the headline (wake_events) result."""
    rows = efficacy_rows(repo, resolved_only=True)
    by_arm = {"controlled": {m: [] for m in _METRICS}, "held": {m: [] for m in _METRICS}}
    for r in rows:
        arm = r.get("arm")
        if arm not in by_arm:
            continue
        for m in _METRICS:
            v = r.get(m)
            if v is not None:
                by_arm[arm][m].append(v)

    metrics_out = {}
    for m in _METRICS:
        a_vals, b_vals = by_arm["controlled"][m], by_arm["held"][m]
        diff, ci, p = _welch_ci(a_vals, b_vals)  # diff = held - controlled
        metrics_out[m] = {
            "controlled": _stats(a_vals),
            "held": _stats(b_vals),
            "diff_held_minus_controlled": diff,
            "ci": ci,
            "p_value": p,
            "lower_better": m in _LOWER_BETTER,
        }

    n_controlled = len(by_arm["controlled"]["wake_events"]) or len(by_arm["controlled"]["deep_pct"]) \
        or len(by_arm["controlled"]["efficiency"])
    n_held = len(by_arm["held"]["wake_events"]) or len(by_arm["held"]["deep_pct"]) \
        or len(by_arm["held"]["efficiency"])
    enough = n_controlled >= min_n_per_arm and n_held >= min_n_per_arm

    verdict = _verdict(metrics_out, n_controlled, n_held, min_n_per_arm, enough)

    return {
        "enough_data": enough,
        "min_n_per_arm": min_n_per_arm,
        "n_controlled": n_controlled,
        "n_held": n_held,
        "metrics": metrics_out,
        "verdict": verdict,
    }


def _verdict(metrics_out: dict, n_controlled: int, n_held: int, min_n: int, enough: bool) -> str:
    if not enough:
        need_c = max(0, min_n - n_controlled)
        need_h = max(0, min_n - n_held)
        return (f"Not enough data yet (controlled n={n_controlled}, held n={n_held}; need "
                f">={min_n}/arm — {need_c} more controlled, {need_h} more held nights).")
    wake = metrics_out["wake_events"]
    diff, ci, p = wake["diff_held_minus_controlled"], wake["ci"], wake["p_value"]
    if diff is None or ci is None:
        return "Not enough variance in wake_events yet to compare arms."
    significant = p is not None and p < 0.05
    # diff = held - controlled; a POSITIVE diff means held had MORE wake_events, i.e. the
    # controller (lower wake_events) is better.
    if significant and diff > 0:
        pct = abs(diff) / wake["held"]["mean"] * 100.0 if wake["held"]["mean"] else None
        pct_str = f" ({pct:.0f}% fewer)" if pct is not None else ""
        return (f"The controller reduces awakenings by {abs(diff):.2f}/night{pct_str} vs the "
                f"held baseline (n_controlled={n_controlled}, n_held={n_held}, p={p:.3f}).")
    if significant and diff < 0:
        return (f"Surprising: the held baseline shows FEWER awakenings than the controlled loop "
                f"(Δ={diff:.2f}/night, n_controlled={n_controlled}, n_held={n_held}, p={p:.3f}). "
                "Worth investigating before trusting the controller's steering.")
    p_str = f"{p:.3f}" if p is not None else "n/a"
    return (f"No significant difference in awakenings yet (Δ={diff:.2f}/night, p={p_str}, "
            f"n_controlled={n_controlled}, n_held={n_held}). Keep collecting nights.")
