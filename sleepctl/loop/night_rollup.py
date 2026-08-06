"""Reconstruct a :class:`NightSummary` from our OWN persisted frames.

The Eight Sleep adapters' ``fetch_night_summary`` is a stub that returns an empty
``NightSummary(date=date)`` — its docstring defers the real work to "the storage/nightly layer,
which already reconstructs summaries from persisted frames". That layer did not exist, so every
nightly close-out handed :meth:`NightlyUpdater.run` an all-``None`` summary, threw, and was
swallowed by the daemon's ``_skip``. Net effect: ``nightly_summaries`` stayed EMPTY forever and
the entire learning stack downstream of it (baselines, tiered policy, response estimator, ML
reward attribution + recommendation, all three efficacy trials, the nightly report) never
received a single observation. It also meant the once-a-night housekeeping that shares that
try-block — event/sample pruning and the rotating DB backup — never ran either.

This module closes that gap using data we already own and do not pay for: the per-tick
``raw_samples`` written by the live daemon (stage + confidence, HR, HRV, movement, respiratory
rate, commanded level, controller state, wake flags). That makes the nightly rollup independent
of the Eight Sleep sleep-tracking membership, which is exactly the position the rest of the
controller has already moved to (see ``state_estimator`` steering off the Verity alone).

Deliberately conservative: every field is optional and stays ``None`` when the underlying
evidence is missing, so a short or sensor-less night degrades to a sparse summary rather than a
confidently wrong one. Pure read-side and stdlib-only.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Optional

from sleepctl.models import NightSummary

#: Controller states that mean "in bed, asleep or trying to be" — used to bound the sleep period.
_ASLEEP_STATES = ("maintenance", "wake_recovery", "wake_window")

#: Cap on how much time a single sample may represent. Each sample is credited the real gap to
#: the NEXT sample rather than a uniform epoch: the daemon emits both a control tick and a
#: telemetry tick, so samples arrive in close pairs and the MEDIAN gap is about double the true
#: mean spacing — assuming a uniform epoch double-counted a 385-minute night as 732 minutes of
#: sleep. Integrating real gaps is exact under irregular sampling; the cap stops a restart or a
#: sensor dropout from crediting an hour of silence to whatever stage preceded it.
_MAX_SAMPLE_MIN = 5.0

#: An awake run must last at least this long to count as a discrete awakening (shorter blips are
#: micro-arousals, not WASO awakenings).
_MIN_AWAKENING_MIN = 1.0

#: Minimum fraction of the in-bed span that must carry a real sleep/wake label before the sleep
#: metrics mean anything. Below this the night is reported as UNMEASURED (bedtime, wake_time and
#: the thermal profile survive; every sleep metric stays None) rather than as a terrible night.
#:
#: A sensor blackout and a catastrophic night look identical in the totals, and the difference
#: matters enormously to the learners now consuming this. On 2026-08-06 the wearable dropped at
#: 00:01 and only 55 ticks of physiology existed across a ~10 h in-bed span: reconstructing it
#: yielded "38.8 min of sleep, 6.5% efficiency" for a night the user actually slept ~9 hours.
#: Persisting that would have driven the baselines, the reward and every policy off a night that
#: was never measured at all.
_MIN_COVERAGE_FRAC = 0.5


def _parse(ts) -> Optional[datetime]:
    if isinstance(ts, datetime):
        return ts
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts))
    except Exception:
        return None


def _durations(times: list) -> list:
    """Minutes each sample represents: the real gap to the next sample, capped.

    The final sample is credited the median of the others so a night never ends on an
    unbounded tail.
    """
    if not times:
        return []
    gaps = [min(_MAX_SAMPLE_MIN, max(0.0, (b - a).total_seconds() / 60.0))
            for a, b in zip(times, times[1:])]
    tail = statistics.median(gaps) if gaps else 0.0
    return gaps + [tail]


def _mean(vals) -> Optional[float]:
    vals = [float(v) for v in vals if v is not None]
    return round(statistics.fmean(vals), 2) if vals else None


def reconstruct_night_summary(repo, night_date: str, stage_by_ts=None) -> NightSummary:
    """Build a ``NightSummary`` for ``night_date`` from persisted ``raw_samples``.

    Returns a summary whose fields are ``None`` wherever the night lacks the evidence to support
    them (no rows at all yields a bare ``NightSummary(date=night_date)``, matching the old stub's
    contract so callers cannot regress).

    ``stage_by_ts`` optionally supplies corrected ``{iso_ts: stage_label}`` labels (see
    :func:`sleepctl.loop.restage.restage_night`) to use in place of the recorded
    ``raw_samples.stage``. That lets a night recorded by a stale or defective build be scored
    from its raw physiology instead of the labels that build happened to emit, without rewriting
    the audit trail in ``raw_samples``.
    """
    ns = NightSummary(date=night_date)
    try:
        rows = repo.conn.execute(
            "SELECT ts, stage, heart_rate, hrv, respiratory_rate, movement, "
            "       commanded_level, controller_state, wake_event "
            "FROM raw_samples WHERE night_date = ? ORDER BY id ASC",
            (night_date,),
        ).fetchall()
    except Exception:
        return ns
    samples = [(t, r) for r in rows if (t := _parse(r["ts"])) is not None]
    if not samples:
        return ns

    times = [t for t, _ in samples]

    # --- bed / sleep / wake boundaries from the controller's own state track -----------------
    # bedtime  = first tick the controller was no longer IDLE (it left IDLE on bed entry)
    # onset    = first tick it reached an asleep state (INDUCTION -> MAINTENANCE is onset)
    # wake_time= last tick before it returned to IDLE for good
    in_bed = [(t, r) for t, r in samples if (r["controller_state"] or "idle") != "idle"]
    asleep = [(t, r) for t, r in samples if (r["controller_state"] or "") in _ASLEEP_STATES]
    ns.bedtime = in_bed[0][0] if in_bed else times[0]
    onset = asleep[0][0] if asleep else None
    ns.wake_time = in_bed[-1][0] if in_bed else times[-1]
    if onset is not None and ns.bedtime is not None:
        ns.sleep_onset_latency_min = round(
            max(0.0, (onset - ns.bedtime).total_seconds() / 60.0), 1)

    # --- stage architecture over the sleep period --------------------------------------------
    # Scored between onset and final wake only: pre-onset settling and post-wake lying-in are
    # not sleep, and counting them is what made an earlier ad-hoc measurement read ~9% "awake"
    # on a night the user reported ~8 awakenings.
    period = [(t, r) for t, r in samples
              if (onset is None or t >= onset) and (ns.wake_time is None or t <= ns.wake_time)]
    ov = stage_by_ts or {}

    def _stage(t, r) -> str:
        return ov.get(t.isoformat()) or r["stage"] or "unknown"

    if period:
        durs = _durations([t for t, _ in period])
        mins: dict = {}
        for (t, r), d in zip(period, durs):
            key = _stage(t, r)
            mins[key] = mins.get(key, 0.0) + d
        deep = mins.get("deep", 0.0)
        rem = mins.get("rem", 0.0)
        light = mins.get("light", 0.0)
        awake = mins.get("awake", 0.0)
        staged = deep + rem + light + awake
        in_bed_span = ((ns.wake_time - ns.bedtime).total_seconds() / 60.0
                       if (ns.wake_time and ns.bedtime) else 0.0)
        coverage = (staged / in_bed_span) if in_bed_span > 0 else 0.0
        ns.temp_profile_summary = dict(ns.temp_profile_summary or {})
        ns.temp_profile_summary["coverage"] = round(coverage, 3)
        if in_bed_span > 0 and coverage < _MIN_COVERAGE_FRAC:
            # UNMEASURED, not bad. Leave every sleep metric None so nothing downstream can
            # mistake a sensor outage for a night worth learning from.
            ns.temp_profile_summary["unmeasured"] = True
            ns.temp_profile_summary["reason"] = (
                f"only {coverage * 100:.0f}% of the {in_bed_span:.0f} min in bed carried a "
                f"sleep/wake label — sensor coverage too low to score this night")
            return ns
        if staged > 0:
            ns.deep_min = round(deep, 1)
            ns.rem_min = round(rem, 1)
            ns.light_min = round(light, 1)
            ns.waso_min = round(awake, 1)
            ns.total_sleep_min = round(deep + rem + light, 1)
            in_bed_min = ((ns.wake_time - ns.bedtime).total_seconds() / 60.0
                          if (ns.wake_time and ns.bedtime) else None)
            if in_bed_min and in_bed_min > 0:
                ns.sleep_efficiency = round(
                    min(1.0, (ns.total_sleep_min or 0.0) / in_bed_min), 3)

        # --- awakenings: sustained AWAKE runs inside the sleep period ------------------------
        # Not the ``raw_samples.wake_event`` column: that flag is written 0 on every row (it was
        # never wired up) even on nights the arousal detector scored 10/10 against the user's own
        # report, so counting its rising edges always yields 0. Runs of AWAKE staging are the
        # standard WASO-awakening definition and use a field we actually populate.
        runs, run = 0, 0.0
        for (t, r), d in zip(period, durs):
            if _stage(t, r) == "awake":
                run += d
            else:
                if run >= _MIN_AWAKENING_MIN:
                    runs += 1
                run = 0.0
        if run >= _MIN_AWAKENING_MIN:
            runs += 1
        ns.wake_events = runs

        # --- vitals -------------------------------------------------------------------------
        ns.avg_hr = _mean(r["heart_rate"] for _, r in period)
        ns.avg_hrv = _mean(r["hrv"] for _, r in period)
        ns.avg_respiratory_rate = _mean(r["respiratory_rate"] for _, r in period)

        levels = [r["commanded_level"] for _, r in period if r["commanded_level"] is not None]
        if levels:
            # update, don't replace -- `coverage` was already recorded above and a plain
            # assignment here silently dropped it
            ns.temp_profile_summary.update({
                "mean_level": round(statistics.fmean(levels), 1),
                "min_level": min(levels),
                "max_level": max(levels),
                "n": len(levels),
            })

    ns.temp_profile_summary = dict(ns.temp_profile_summary or {})
    ns.temp_profile_summary.setdefault("source", "reconstructed")
    ns.temp_profile_summary.setdefault("samples", len(samples))
    return ns


def merge_night_summary(base: NightSummary, override) -> NightSummary:
    """Overlay any non-``None`` scalar field of ``override`` onto ``base``.

    Lets the locally reconstructed summary be the floor while a richer upstream source (a live
    Eight Sleep sleep-tracking membership, if one is ever active) still wins per-field. With
    today's stub adapter every override field is ``None``, so ``base`` passes through unchanged.
    """
    if override is None:
        return base
    for f in ("bedtime", "wake_time", "total_sleep_min", "sleep_onset_latency_min",
              "deep_min", "rem_min", "light_min", "wake_events", "waso_min",
              "sleep_efficiency", "avg_hr", "avg_hrv", "avg_respiratory_rate"):
        v = getattr(override, f, None)
        if v is not None:
            setattr(base, f, v)
    return base
