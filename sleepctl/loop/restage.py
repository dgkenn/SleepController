"""Re-derive a night's sleep stages from the RAW SENSOR DATA with the current estimator.

``raw_samples.stage`` records whatever the estimator emitted at the time, so a night recorded by
a stale or defective build keeps those labels forever — and every rollup, report and learner
downstream inherits them. 2026-08-04 is the worked example: the deployed build persisted
light 92.3% / deep 2.0% / **REM 0.0%** across the sleep period, while the same code we ship today,
replayed over the *identical* dense Verity series, yields light 51.8 / deep 14.3 / REM 28.4 /
awake 5.5. The physiology was recorded correctly; only the labels were wrong.

This module replays the stored sensor stream through :func:`estimate_sleep_stage` exactly the way
the live daemon feeds it — the dense ~1-sample-per-2s Verity series from ``sensor_samples``, the
same 45-minute trailing window, the same quality exclusions (``hr_frozen`` / ``not_worn``), the
band's own actigraphy counts and the trailing 10 minutes of beat intervals when those were
recorded, and the real bed-entry and onset clocks — and returns the corrected labels.

Two replays:

  * :func:`restage_night` — CAUSAL, what the live estimator would say today at each tick.
  * :func:`restage_night_offline` — the morning hypnogram. The stager's per-tick emissions are
    smoothed with the epochs after each tick as well (:mod:`sleepctl.ml.sleep_staging.offline`)
    instead of the live forward filter's past-only window, then the live post-processing runs
    on top of that.

It deliberately does NOT rewrite ``raw_samples``: those rows stay as the honest record of what the
controller actually believed at the time (an audit trail for anything that acted on them). The
corrected labels are handed to the rollup instead, so the *summary* the learners consume reflects
the physiology rather than a build artifact.

Timestamp care: ``raw_samples.ts`` is naive LOCAL while ``sensor_samples.ts`` (and the
``actigraphy`` / ``rr_intervals`` tables) are aware UTC (see the convention in
``sleepctl.storage.schema``). Getting that wrong silently yields empty history windows and a
plausible-looking but meaningless hypnogram.
"""

from __future__ import annotations

import bisect
import json
import statistics
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from sleepctl.config import AppConfig
from sleepctl.controller.state_estimator import estimate_sleep_stage
from sleepctl.ml.sleep_staging.infer import EPOCH_S
from sleepctl.models import SensorFrame, SleepStage

#: Trailing dense-history window handed to the stager, matching ``live_daemon._read_frame``'s
#: ``read_history(minutes=45.0)``. The model's features look back at most 35 min, so this covers
#: them with headroom.
HISTORY_MIN = 45.0

#: Trailing beat-interval window, matching ``BcgSensorSource.read_history``'s
#: ``recent_rr_intervals(minutes=10.0)``.
RR_HISTORY_MIN = 10.0

#: Trailing frames used for the settled-sleep HR baseline, matching the controller's own
#: ``_sleep_baseline`` pooling.
_BASELINE_POOL = 15

#: Plausible night lengths for the stager's clock normalisation (as in the controller).
_NIGHT_MIN_RANGE = (120.0, 840.0)

_STAGES = {"light": SleepStage.LIGHT, "deep": SleepStage.DEEP,
           "rem": SleepStage.REM, "awake": SleepStage.AWAKE}


def _parse(ts) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts))
    except Exception:
        return None


def _epoch(ts) -> Optional[float]:
    t = _parse(ts)
    return t.timestamp() if t is not None else None


def _day_bounds(lo: float, hi: float) -> tuple:
    """ISO date strings a day either side of an epoch span: a coarse SQL prefilter that is
    correct for any ISO spelling of an aware-UTC ``ts`` (the exact window is applied after)."""
    return ((datetime.fromtimestamp(lo, timezone.utc) - timedelta(days=1)).date().isoformat(),
            (datetime.fromtimestamp(hi, timezone.utc) + timedelta(days=2)).date().isoformat())


def _dense_hr(conn, span=None) -> list:
    """(epoch_seconds, bpm) for every usable wearable HR sample, ascending.

    Applies the same exclusions as ``bridge.sensor_history_series``: a frozen or not-worn HR has
    near-zero variability, which reads to the stager as a strong SLEEP signal.
    """
    sql = ("SELECT ts, hr FROM sensor_samples"
           " WHERE hr IS NOT NULL AND hr_frozen = 0 AND not_worn = 0")
    args: tuple = ()
    if span is not None:
        sql += " AND ts >= ? AND ts < ?"
        args = _day_bounds(*span)
    rows = conn.execute(sql + " ORDER BY ts ASC", args).fetchall()
    out = []
    for r in rows:
        t = _epoch(r["ts"])
        if t is not None:
            out.append((t, float(r["hr"])))
    out.sort(key=lambda s: s[0])
    return out


def _series(conn, sql: str, span) -> list:
    """Rows of an optional ingest table inside the night's span ([] when it does not exist)."""
    try:
        return conn.execute(sql, _day_bounds(*span)).fetchall()
    except Exception:
        return []


def _motion(conn, span) -> tuple:
    """``(series, units)`` as ``bridge.sensor_history_series`` picks them: the band's own
    actigraphy counts when any exist, else the phone's movement index."""
    counts = []
    for r in _series(conn, "SELECT ts, pim FROM actigraphy WHERE pim IS NOT NULL"
                           " AND ts >= ? AND ts < ? ORDER BY ts ASC", span):
        t = _epoch(r["ts"])
        if t is not None:
            counts.append((t, float(r["pim"])))
    if counts:
        return sorted(counts), "counts"
    phone = []
    for r in _series(conn, "SELECT ts, movement FROM sensor_samples WHERE movement IS NOT NULL"
                           " AND ts >= ? AND ts < ? ORDER BY ts ASC", span):
        t = _epoch(r["ts"])
        if t is not None:
            phone.append((t, float(r["movement"])))
    return sorted(phone), ("phone_index" if phone else None)


def _rr_batches(conn, span) -> list:
    """``[(batch_epoch, source, [(beat_epoch, rr_ms), ...]), ...]`` ascending, each beat walked
    back from the batch's post time by its own length, as ``bridge.recent_rr_intervals`` does."""
    out = []
    for r in _series(conn, "SELECT ts, rr_ms, source FROM rr_intervals"
                           " WHERE ts >= ? AND ts < ? ORDER BY ts ASC", span):
        t = _epoch(r["ts"])
        try:
            vals = [float(v) for v in json.loads(r["rr_ms"])]
        except Exception:
            continue
        if t is None:
            continue
        back, beats = sum(vals), []
        for v in vals:
            back -= v
            beats.append((t - back / 1000.0, v))
        out.append((t, r["source"], beats))
    out.sort(key=lambda b: b[0])
    return out


def _rr_window(batches: list, batch_ts: list, now: float) -> list:
    """The trailing beat intervals of ONE source: the one that posted the newest batch."""
    lo = bisect.bisect_left(batch_ts, now - RR_HISTORY_MIN * 60.0)
    hi = bisect.bisect_right(batch_ts, now)
    if hi <= lo:
        return []
    src = batches[hi - 1][1]
    return [b for _t, s, beats in batches[lo:hi] if s == src for b in beats]


def _window(series: list, ts: list, now: float, minutes: float) -> list:
    lo = bisect.bisect_left(ts, now - minutes * 60.0)
    hi = bisect.bisect_right(ts, now)
    return series[lo:hi]


def _replay(repo, night_date: str, cfg: AppConfig, *, planned: bool = False):
    """Yield ``(frame, observed, estimate)`` for every in-bed tick of ``night_date``.

    ``frame.timestamp`` is the row's ``ts`` (the key the rollup reads labels by); ``observed``
    (epoch seconds) is when the tick actually ran, ``sample_ts``: two rows share each Pod frame
    ``ts`` while the daemon ticks twice as often, and it is at the tick that the live stager read
    its trailing histories and clocks. ``None`` when there is nothing to replay. ``planned``
    passes the night's real in-bed length as the stager's clock span.
    """
    try:
        rows = repo.conn.execute(
            "SELECT ts, COALESCE(sample_ts, ts) AS obs_ts, heart_rate, hrv, movement,"
            " controller_state FROM raw_samples WHERE night_date = ? ORDER BY id ASC",
            (night_date,)).fetchall()
    except Exception:            # an older database without the sample_ts column
        rows = repo.conn.execute(
            "SELECT ts, ts AS obs_ts, heart_rate, hrv, movement, controller_state"
            " FROM raw_samples WHERE night_date = ? ORDER BY id ASC", (night_date,)).fetchall()
    samples = [(t, _parse(r["obs_ts"]) or t, r)
               for r in rows if (t := _parse(r["ts"])) is not None]
    if not samples:
        return None
    in_bed = [(t, o) for t, o, r in samples if (r["controller_state"] or "idle") != "idle"]
    if not in_bed:
        return None
    (bedtime, bed_obs), (last_in_bed, last_obs) = in_bed[0], in_bed[-1]
    asleep = [o for _t, o, r in samples
              if (r["controller_state"] or "") in ("maintenance", "wake_recovery", "wake_window")]
    onset = asleep[0] if asleep else None

    span = (min(bedtime, bed_obs).astimezone().timestamp() - HISTORY_MIN * 60.0,
            max(last_in_bed, last_obs).astimezone().timestamp())
    dense = _dense_hr(repo.conn, span)
    if not dense:
        return None
    dense_ts = [d[0] for d in dense]
    motion, units = _motion(repo.conn, span)
    motion_ts = [m[0] for m in motion]
    rr = _rr_batches(repo.conn, span)
    rr_ts = [b[0] for b in rr]
    night_min = None
    if planned:
        night_min = (last_obs - bed_obs).total_seconds() / 60.0
        if not (_NIGHT_MIN_RANGE[0] <= night_min <= _NIGHT_MIN_RANGE[1]):
            night_min = None

    ticks = [(SensorFrame(timestamp=t, stage=SleepStage.UNKNOWN, heart_rate=r["heart_rate"],
                          hrv=r["hrv"], movement=r["movement"], presence=None), o)
             for t, o, r in samples if bedtime <= t <= last_in_bed]
    frames = [f for f, _o in ticks]

    def _gen():
        for i, (f, obs) in enumerate(ticks):
            if f.heart_rate is None:
                continue
            epoch = obs.astimezone().timestamp()        # naive-local row -> real instant
            f.hr_history = _window(dense, dense_ts, epoch, HISTORY_MIN)
            if motion:
                f.activity_history = _window(motion, motion_ts, epoch, HISTORY_MIN) or None
                f.activity_units = units if f.activity_history else None
            if rr:
                f.rr_history = _rr_window(rr, rr_ts, epoch) or None

            recent = frames[max(0, i - 30):i]
            pool = [x.heart_rate for x in recent if x.heart_rate is not None][-_BASELINE_POOL:]
            base = statistics.fmean(pool) if pool else None

            est = estimate_sleep_stage(
                f, base, recent, cfg,
                minutes_since_start=(obs - bed_obs).total_seconds() / 60.0,
                minutes_since_onset=((obs - onset).total_seconds() / 60.0
                                     if onset is not None else None),
                planned_night_min=night_min)
            yield f, epoch, est
    return {"ticks": _gen(), "onset": onset}


def restage_night(repo, night_date: str, cfg: Optional[AppConfig] = None) -> dict:
    """Return ``{iso_ts: stage_label}`` for ``night_date``, re-derived with the current estimator.

    Only ticks from bed entry onward are restaged; the out-of-bed guard in the controller is
    reproduced here by simply not scoring anything outside the in-bed span, so a band left still
    on a charger after wake cannot contribute fake DEEP (which is what put 281 of one night's 296
    deep samples in the following MORNING).
    """
    cfg = cfg or AppConfig.default()
    rep = _replay(repo, night_date, cfg)
    if rep is None:
        return {}
    out: dict = {}
    for f, _obs, est in rep["ticks"]:
        if est is not None:
            out[f.timestamp.isoformat()] = est[0].value
    return out


# ------------------------------------------------------------------ offline (non-causal)
class _Stager:
    """Stands in for the live stager while a night is replayed, delegating everything else.

    ``record``: each ``predict`` is answered UNSMOOTHED, under the exact arguments the live
    estimator built (variant selection, features, clocks, wake bias), and the emissions are
    kept on the 30 s grid the live filter itself scores: live, every tick recomputes the
    emission at each 30 s step back from its newest heart-rate sample, whatever the tick rate,
    so a step since the previous tick is back-filled from the same history cut at that step.
    ``replay``: each ``predict`` is answered from the offline posterior instead, so the live
    post-processing downstream of the stager runs, unchanged, on the offline stage. Calls are
    keyed by (tick, n-th call in that tick), so the two passes cannot drift out of step.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.mode = "record"
        self.emissions: list = []           # (epoch end, emission, variant)
        self.calls: dict = {}               # (tick, n) -> index of its own emission
        self.answers: dict = {}             # (tick, n) -> StageEstimate
        self.tick = -1
        self._n = 0
        self._last_end: Optional[float] = None

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def begin(self, tick: int) -> None:
        self.tick, self._n = tick, 0

    def predict(self, hr_samples, activity_samples=None, minutes_since_start=None,
                minutes_since_onset=None, **kw):
        key = (self.tick, self._n)
        self._n += 1
        if self.mode != "record":
            return self.answers.get(key)
        kw["smooth"] = False
        hr = sorted((float(t), float(v)) for t, v in (hr_samples or ()))
        if not hr:
            return None
        act = (sorted(activity_samples, key=lambda x: float(x[0]))
               if activity_samples else activity_samples)
        ibi = kw.get("ibi_samples")
        ibi = sorted(ibi, key=lambda x: float(x[0])) if ibi else ibi
        last_t = hr[-1][0]
        steps = 1
        if self._last_end is not None:
            steps = int(round((last_t - self._last_end) / EPOCH_S))
            steps = max(1, min(steps, int(getattr(self._inner, "smoothing_epochs", 20))))
        self._last_end = last_t
        est = None
        for k in range(steps - 1, -1, -1):
            end, back = last_t - EPOCH_S * k, 0.5 * k
            if k:
                kw_k = dict(kw)
                if ibi:
                    kw_k["ibi_samples"] = _upto(ibi, end)
                est = self._inner.predict(
                    _upto(hr, end), _upto(act, end),
                    None if minutes_since_start is None else minutes_since_start - back,
                    None if minutes_since_onset is None else minutes_since_onset - back, **kw_k)
            else:
                est = self._inner.predict(hr, act, minutes_since_start, minutes_since_onset,
                                          **kw)
            if est is not None:
                self.emissions.append((end, [float(est.probs[c]) for c in _LABELS],
                                       est.variant))
        self.calls[key] = len(self.emissions) - 1 if est is not None else None
        return est


_LABELS = ("wake", "light", "deep", "rem")


def _upto(series, end: float):
    """A time-sorted ``(t, ...)`` series cut at ``end`` (inclusive), as the live filter cuts it."""
    if not series:
        return series
    return series[:bisect.bisect_right([float(x[0]) for x in series], end)]


@contextmanager
def _stager_swapped(proxy):
    """Install ``proxy`` as the estimator's stager for the duration, whatever happens. The
    nightly close-out runs synchronously on the daemon's loop, so no live tick sees it."""
    from sleepctl.controller import state_estimator as se
    saved = (se._STAGER, se._STAGER_LOADED)
    se._STAGER, se._STAGER_LOADED = proxy, True
    try:
        yield
    finally:
        se._STAGER, se._STAGER_LOADED = saved


def _fresh_rescorer() -> None:
    """Start the autonomic rescorer's night distribution over (it is a module singleton)."""
    from sleepctl.controller import state_estimator as se
    r = getattr(se, "_RESCORER", None)
    if r is not None:
        r.reset()


def restage_night_offline(repo, night_date: str, cfg: Optional[AppConfig] = None,
                          method: Optional[str] = None, whole_night: bool = False) -> dict:
    """``{iso_ts: stage_label}`` for ``night_date``, smoothed with the night AFTER each tick too.

    1. Replay the night once, answering the estimator's stager calls unsmoothed and keeping each
       tick's emission -- the model output before the HMM, from the same variant and features.
    2. Smooth those emissions non-causally with the same HMM (transitions, prior, temper, and
       the personal HMM when one is active): :func:`sleepctl.ml.sleep_staging.offline.
       smooth_night`, by default the live trailing window plus a short lookahead.
    3. Replay again with the stager answering from that posterior, so everything downstream of
       the model -- the accelerometer and absolute wake tests, autonomic rescoring, deep
       corroboration -- runs exactly as it does live, on the offline stage. Then the hypnogram
       constraints, as in the controller. The stage hold is NOT applied: it damps a causal
       filter's tick-to-tick flapping, and on held-out subjects it only cost agreement.

    ``method`` / ``whole_night`` are passed to ``smooth_night``. Returns ``{}`` when the night
    cannot be replayed (no in-bed ticks, no dense HR, no stager). Raises on anything
    unexpected: the caller owns the fallback.
    """
    from sleepctl.controller.state_estimator import _get_stager

    cfg = cfg or AppConfig.default()
    stager = _get_stager()
    if stager is None or not getattr(stager, "hmm", None):
        return {}
    proxy = _Stager(stager)
    with _stager_swapped(proxy):
        try:
            return _two_passes(repo, night_date, cfg, proxy, stager.hmm,
                               method=method, whole_night=whole_night)
        finally:
            _fresh_rescorer()


def _ticks(rep, proxy):
    """``(k, frame, observed, estimate)``, telling the stand-in which tick is calling."""
    it, k = rep["ticks"], 0
    while True:
        proxy.begin(k)
        try:
            f, obs, est = next(it)
        except StopIteration:
            return
        yield k, f, obs, est
        k += 1


def _two_passes(repo, night_date, cfg, proxy, hmm, *, method, whole_night) -> dict:
    from sleepctl.controller.hypnogram import HypnogramConstraint, constrain
    from sleepctl.ml.sleep_staging import offline
    from sleepctl.ml.sleep_staging.infer import StageEstimate

    _fresh_rescorer()
    rep = _replay(repo, night_date, cfg, planned=True)
    if rep is None:
        return {}
    for _tick in _ticks(rep, proxy):         # pass 1: the stand-in records every emission
        pass
    em = proxy.emissions
    if not em:
        return {}
    smoothed = offline.smooth_night([t for t, _e, _v in em], [e for _t, e, _v in em], hmm,
                                    method=method or offline.DEFAULT_METHOD,
                                    whole_night=whole_night)
    for key, i in proxy.calls.items():
        if i is None or smoothed[i] is None:
            continue
        p = smoothed[i]["probs"]
        stage = smoothed[i]["stage"]
        proxy.answers[key] = StageEstimate(
            stage_label=stage, p_wake=p["wake"], confidence=p[stage], probs=dict(p),
            source="model", smoothed=True, variant=em[i][2] or "hr")

    proxy.mode = "replay"
    _fresh_rescorer()
    rep = _replay(repo, night_date, cfg, planned=True)
    if rep is None:
        return {}
    onset = rep["onset"]
    hc = HypnogramConstraint()
    out: dict = {}
    for _k, f, obs, est in _ticks(rep, proxy):
        if est is None:
            continue
        now = datetime.fromtimestamp(obs)               # the tick's own (naive local) clock
        est = constrain(est, now, cfg, hc, onset if onset is not None and now >= onset else None)
        hc.observe(est[0], now)
        out[f.timestamp.isoformat()] = est[0].value
    return out
