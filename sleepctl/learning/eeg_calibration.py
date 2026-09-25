"""Personal stager calibration against EEG-headband nights.

The stager's forests and its smoothing HMM were fitted on other people. A few nights scored by
an EEG headband worn beside the arm band (``sleepctl.eval.hypnogram_import``) say how its
output maps onto THIS sleeper, and three light-touch adjustments are fitted from them:

  * **emission bias / temperature** -- a recalibration of the per-epoch class probabilities
    before smoothing: a log-weight per class (light is the reference) and a temperature on the
    whole emission. Both land exactly on two fields the forward filter already reads:
    ``lik_k = (e_k / emission_prior_k) ** temper``, so a class weight ``w_k`` is
    ``emission_prior_k * exp(-w_k)`` and a temperature ``T`` is ``temper / T``;
  * **HMM transitions** -- counted on the headband's own epochs and blended with the bundled
    matrix exactly as ``learning.hypnogram_priors`` blends the stager's self-labels (same
    pseudo-count, same floor on entering deep / REM). Unlike those self-labels, these are
    ground truth, so there is no feedback loop to guard against;
  * **a wake threshold** on the smoothed wake probability (the stager fixes it at 0.5).

The emissions are REPLAYED from the stored sensor stream the way the live daemon feeds the
stager (dense ``sensor_samples`` HR with the same quality exclusions and 45-minute window,
the armband's actigraphy counts when present, the real bed-entry and onset clocks), with a
fresh, un-personalised stager, and scored unsmoothed; the live smoothing is then reproduced
exactly -- a stateless forward filter over the trailing ``smoothing_epochs`` emissions from the
HMM's start distribution -- so every candidate is judged on what it would actually have output.

Validation is leave-one-night-out: each night is scored by a calibration fitted on the others,
against the unadjusted stager on the same epochs. The calibration is stored ``enabled`` only
when its pooled held-out kappa beats the unadjusted model by :data:`MIN_KAPPA_GAIN`, it wins
on at least half the held-out nights, and the gain survives a block bootstrap of the held-out
epochs (:data:`BOOT_CONFIDENCE`); the wake threshold is gated the same way on top of it.
"""

from __future__ import annotations

import bisect
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Sequence, Tuple

from sleepctl.eval.eeg_agreement import cohen_kappa
from sleepctl.eval.hypnogram_import import CLASSES, imported_nights, load_epochs

MIN_NIGHTS = 3                 # leave-one-out needs at least two nights to fit each fold on
MAX_NIGHTS = 14                # most recent imported nights used
MIN_KAPPA_GAIN = 0.02          # pooled held-out kappa gain required to enable
MIN_THRESHOLD_GAIN = 0.01      # further gain the wake threshold must add on top
#: A few nights of kappa move by a few hundredths on noise alone, so a gain must also hold up
#: under a block bootstrap of the held-out epochs: 20-minute blocks (neighbouring epochs are
#: far from independent), resampled with a fixed seed, calibrated ahead in this share of draws.
BOOT_BLOCK_EPOCHS = 40
BOOT_REPS = 400
BOOT_CONFIDENCE = 0.9
REG = 0.02                     # L2 pull of every log-parameter toward "unadjusted"
W_BOUND = 1.5                  # |class log-weight| never exceeds this (a factor of ~4.5)
T_BOUND = 1.0                  # |log temperature| never exceeds this (temper scaled at most ~2.7x)
FIT_STRIDE = 2                 # score every 2nd epoch while fitting (windows still use them all)
MAX_EVALS = 90
THRESHOLDS = [round(0.25 + 0.05 * i, 2) for i in range(11)]   # 0.25 .. 0.75
HISTORY_MIN = 45.0             # the live daemon's dense-history window
MIN_HR_SAMPLES = 5
#: An epoch whose newest HR sample is older than this is unscored: the stager would otherwise
#: score the stale tail of the window as if it were the present.
STALE_S = 120.0
PROFILE_VERSION = 1

Emission = Optional[List[float]]


@dataclass
class NightData:
    night_date: str
    truth: List[Optional[str]]       # EEG class per epoch (None = unscored)
    emissions: List[Emission]        # unsmoothed stager emission per epoch (None = no HR)
    times: List[float]
    epoch_s: float = 30.0


# --------------------------------------------------------------------------- population model
def population_hmm() -> dict:
    """The bundled smoothing model, read fresh from disk (never the live, personalised one)."""
    from sleepctl.ml.sleep_staging.infer import WEIGHTS_DIR
    with open(os.path.join(WEIGHTS_DIR, "hmm.json")) as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- emission replay
def _parse(ts) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts))
    except Exception:
        return None


def _utc_bounds(lo_unix: float, hi_unix: float) -> Tuple[str, str]:
    """Loose string bounds for an aware-UTC ``ts`` column: date prefixes sort correctly
    whatever the stored suffix (``+00:00`` / ``Z`` / fractional seconds)."""
    lo = datetime.fromtimestamp(lo_unix, timezone.utc) - timedelta(days=1)
    hi = datetime.fromtimestamp(hi_unix, timezone.utc) + timedelta(days=2)
    return lo.date().isoformat(), hi.date().isoformat()


def _dense_series(conn, lo_unix: float, hi_unix: float) -> Tuple[list, list]:
    """(hr, actigraphy) as ascending ``(unix, value)`` lists within the span.

    HR excludes ``hr_frozen`` / ``not_worn`` samples exactly as ``bridge.sensor_history_series``
    does for the live stager; actigraphy is the armband's own PIM counts."""
    a, b = _utc_bounds(lo_unix, hi_unix)
    hr: list = []
    try:
        rows = conn.execute(
            "SELECT ts, hr, hr_frozen, not_worn FROM sensor_samples WHERE ts >= ? AND ts < ? "
            "AND hr IS NOT NULL ORDER BY ts ASC", (a, b)).fetchall()
        rows = [(r[0], r[1]) for r in rows if not (r[2] or r[3])]
    except Exception:
        try:
            rows = [(r[0], r[1]) for r in conn.execute(
                "SELECT ts, hr FROM sensor_samples WHERE ts >= ? AND ts < ? AND hr IS NOT NULL "
                "ORDER BY ts ASC", (a, b)).fetchall()]
        except Exception:
            rows = []
    for ts, v in rows:
        t = _parse(ts)
        if t is not None and lo_unix <= t.timestamp() <= hi_unix:
            hr.append((t.timestamp(), float(v)))
    act: list = []
    try:
        for r in conn.execute("SELECT ts, pim FROM actigraphy WHERE ts >= ? AND ts < ? AND "
                              "pim IS NOT NULL ORDER BY ts ASC", (a, b)).fetchall():
            t = _parse(r[0])
            if t is not None and lo_unix <= t.timestamp() <= hi_unix:
                act.append((t.timestamp(), float(r[1])))
    except Exception:
        act = []
    hr.sort()
    act.sort()
    return hr, act


def _night_clocks(conn, night_date: str) -> Tuple[Optional[float], Optional[float]]:
    """(bed entry, sleep onset) as real instants from the controller's own states, the same
    anchors ``loop.restage`` replays with. Never taken from the EEG: that would leak the
    answer into the features being calibrated."""
    try:
        rows = conn.execute("SELECT ts, controller_state FROM raw_samples WHERE night_date = ? "
                            "ORDER BY id ASC", (night_date,)).fetchall()
    except Exception:
        return None, None
    bed = onset = None
    for r in rows:
        st = r[1] or "idle"
        t = _parse(r[0])
        if t is None:
            continue
        if bed is None and st != "idle":
            bed = t.timestamp()          # naive LOCAL row -> real instant
        if onset is None and st in ("maintenance", "wake_recovery", "wake_window"):
            onset = t.timestamp()
            break
    return bed, onset


def replay_emissions(repo, night_date: str, epochs: Sequence[Tuple[float, float, str]],
                     stager=None, cfg=None) -> List[Emission]:
    """One unsmoothed stager emission (wake/light/deep/rem) per EEG epoch, scored at the
    epoch's END from the trailing dense history -- or None where there is too little HR."""
    if not epochs:
        return []
    if stager is None:
        from sleepctl.ml.sleep_staging.infer import SleepStager
        stager = SleepStager.load()          # fresh: no wake bias, bundled HMM
    if not getattr(stager, "available", False):
        return [None] * len(epochs)
    use_motion = True
    min_hr = MIN_HR_SAMPLES
    try:
        if cfg is None:
            from sleepctl.config import AppConfig
            cfg = AppConfig.default()
        use_motion = bool(getattr(cfg.tunables, "stager_use_motion", True))
        min_hr = int(getattr(cfg.tunables, "stager_min_hr_samples", MIN_HR_SAMPLES))
    except Exception:
        pass
    first, last = epochs[0][0], epochs[-1][0] + epochs[-1][1]
    hr, act = _dense_series(repo.conn, first - HISTORY_MIN * 60.0, last)
    hr_ts = [t for t, _ in hr]
    act_ts = [t for t, _ in act]
    bed, onset = _night_clocks(repo.conn, night_date)
    if bed is None:
        bed = first
    out: List[Emission] = []
    win = HISTORY_MIN * 60.0
    for start, dur, _ in epochs:
        end = start + dur
        lo, hi = bisect.bisect_left(hr_ts, end - win), bisect.bisect_right(hr_ts, end)
        if hi - lo < min_hr or hr_ts[hi - 1] < end - STALE_S:
            out.append(None)
            continue
        a = None
        if use_motion and act:
            alo, ahi = bisect.bisect_left(act_ts, end - win), bisect.bisect_right(act_ts, end)
            a = act[alo:ahi] or None
        try:
            est = stager.predict(hr[lo:hi], activity_samples=a,
                                 minutes_since_start=(end - bed) / 60.0,
                                 minutes_since_onset=((end - onset) / 60.0
                                                      if onset is not None else None),
                                 smooth=False)
        except Exception:
            est = None
        if est is None:
            out.append(None)
            continue
        probs = est.probs
        out.append([float(probs.get("wake", 0.0)), float(probs.get("light", 0.0)),
                    float(probs.get("deep", 0.0)), float(probs.get("rem", 0.0))])
    return out


def load_night(repo, night_date: str, emission_fn: Optional[Callable] = None,
               stager=None) -> Optional[NightData]:
    epochs = load_epochs(repo.conn, night_date)
    if not epochs:
        return None
    if emission_fn is not None:
        em = emission_fn(repo, night_date, epochs)
    else:
        em = replay_emissions(repo, night_date, epochs, stager=stager)
    truth = [e[2] if e[2] in CLASSES else None for e in epochs]
    return NightData(night_date, truth, list(em), [e[0] for e in epochs], epochs[0][1])


# --------------------------------------------------------------------------- live smoothing
def windowed_posteriors(emissions: Sequence[Emission], trans, start, emission_prior,
                        temper: float, window: int, stride: int = 1
                        ) -> List[Optional[Tuple[float, float, float, float]]]:
    """The live stager's smoothed posterior at every epoch (``stride`` > 1 scores a subset).

    The same recursion (and floors) as ``infer.forward_filter``, run as the live stager runs it: a
    fresh pass from ``start`` over the trailing ``window`` emissions for every epoch. Unrolled
    for four classes because the fit calls it tens of times per night."""
    a = float(temper)
    ep = [max(float(p), 1e-9) for p in emission_prior]
    lik: List[Optional[Tuple[float, float, float, float]]] = []
    for e in emissions:
        if e is None:
            lik.append(None)
        else:
            lik.append(((max(e[0], 1e-9) / ep[0]) ** a, (max(e[1], 1e-9) / ep[1]) ** a,
                        (max(e[2], 1e-9) / ep[2]) ** a, (max(e[3], 1e-9) / ep[3]) ** a))
    (t00, t01, t02, t03), (t10, t11, t12, t13), (t20, t21, t22, t23), (t30, t31, t32, t33) = \
        [[float(x) for x in r] for r in trans]
    s0, s1, s2, s3 = (float(x) for x in start)
    out: List[Optional[Tuple[float, float, float, float]]] = [None] * len(lik)
    w = max(1, int(window))
    for i in range(0, len(lik), max(1, int(stride))):
        if lik[i] is None:
            continue
        idx = [j for j in range(max(0, i - w + 1), i + 1) if lik[j] is not None]
        l0, l1, l2, l3 = lik[idx[0]]
        a0, a1, a2, a3 = max(s0, 1e-12) * l0, max(s1, 1e-12) * l1, \
            max(s2, 1e-12) * l2, max(s3, 1e-12) * l3
        tot = a0 + a1 + a2 + a3
        a0, a1, a2, a3 = (a0 / tot, a1 / tot, a2 / tot, a3 / tot) if tot > 0 else (.25,) * 4
        for j in idx[1:]:
            l0, l1, l2, l3 = lik[j]
            b0 = max(a0 * t00 + a1 * t10 + a2 * t20 + a3 * t30, 1e-12) * l0
            b1 = max(a0 * t01 + a1 * t11 + a2 * t21 + a3 * t31, 1e-12) * l1
            b2 = max(a0 * t02 + a1 * t12 + a2 * t22 + a3 * t32, 1e-12) * l2
            b3 = max(a0 * t03 + a1 * t13 + a2 * t23 + a3 * t33, 1e-12) * l3
            tot = b0 + b1 + b2 + b3
            a0, a1, a2, a3 = (b0 / tot, b1 / tot, b2 / tot, b3 / tot) if tot > 0 else (.25,) * 4
        out[i] = (a0, a1, a2, a3)
    return out


def decide(post, wake_threshold: Optional[float] = None) -> Optional[str]:
    """The label the stager emits for a smoothed posterior.

    ``None`` is the shipped rule (argmax, overridden to wake at p_wake >= 0.5). A threshold
    makes wake exactly ``p_wake >= threshold``, else the best sleep class."""
    if post is None:
        return None
    if wake_threshold is None:
        k = max(range(4), key=lambda i: post[i])
        return CLASSES[0] if post[0] >= 0.5 else CLASSES[k]
    if post[0] >= wake_threshold:
        return CLASSES[0]
    return CLASSES[max((1, 2, 3), key=lambda i: post[i])]


# --------------------------------------------------------------------------- the model
def _hmm_with(pop: dict, params: dict) -> dict:
    """The effective smoothing parameters for a calibration ``params`` dict."""
    ep0 = pop.get("emission_prior") or pop.get("prior") or [0.25] * 4
    w = params.get("log_weights") or [0.0] * 4
    ep = [float(ep0[k]) * math.exp(-float(w[k])) for k in range(4)]
    s = sum(ep)
    return {
        "trans": params.get("trans") or pop["trans"],
        "start": pop.get("start") or pop.get("prior"),
        "emission_prior": [v / s for v in ep],
        "temper": float(pop.get("temper", 1.0)) / math.exp(float(params.get("log_temp", 0.0))),
        "window": int(pop.get("smoothing_epochs", 20)),
    }


def _posteriors(night: NightData, hmm: dict, stride: int = 1):
    return windowed_posteriors(night.emissions, hmm["trans"], hmm["start"],
                               hmm["emission_prior"], hmm["temper"], hmm["window"], stride)


def _labels(night: NightData, hmm: dict, threshold=None) -> List[Optional[str]]:
    return [decide(p, threshold) for p in _posteriors(night, hmm)]


def eeg_transitions(nights: Sequence[NightData], pop: dict) -> List[List[float]]:
    """Transitions counted on the headband's epochs, blended with the population matrix the
    way ``hypnogram_priors.learn_transitions`` blends (pseudo-count per row, deep / REM entry
    never below half the population's)."""
    from sleepctl.learning.hypnogram_priors import PSEUDO_COUNT
    pop_t = pop["trans"]
    idx = {c: i for i, c in enumerate(CLASSES)}
    counts = [[0.0] * 4 for _ in range(4)]
    for n in nights:
        for k in range(1, len(n.truth)):
            a, b = n.truth[k - 1], n.truth[k]
            if a in idx and b in idx and n.times[k] - n.times[k - 1] <= 1.5 * n.epoch_s:
                counts[idx[a]][idx[b]] += 1
    trans = []
    for i in range(4):
        rn = sum(counts[i])
        row = [(counts[i][j] + PSEUDO_COUNT * float(pop_t[i][j])) / (rn + PSEUDO_COUNT)
               for j in range(4)]
        for j in (2, 3):
            if i != j:
                row[j] = max(row[j], 0.5 * float(pop_t[i][j]))
        s = sum(row)
        trans.append([v / s for v in row])
    return trans


def _nll(nights: Sequence[NightData], hmm: dict, stride: int) -> float:
    idx = {c: i for i, c in enumerate(CLASSES)}
    tot, n = 0.0, 0
    for night in nights:
        for t, p in zip(night.truth, _posteriors(night, hmm, stride)):
            if p is None or t is None:
                continue
            tot -= math.log(max(p[idx[t]], 1e-6))
            n += 1
    return tot / n if n else 0.0


def _fit_emission(nights: Sequence[NightData], pop: dict, trans) -> Tuple[List[float], float]:
    """Class log-weights (light fixed at 0) and log temperature, by a bounded pattern search on
    the held-in nights' mean negative log-likelihood of the EEG label under the LIVE smoothed
    posterior, with a small ridge toward "unadjusted"."""
    def params(x):
        return {"trans": trans, "log_weights": [x[0], 0.0, x[1], x[2]], "log_temp": x[3]}

    def obj(x):
        return _nll(nights, _hmm_with(pop, params(x)), FIT_STRIDE) + REG * sum(v * v for v in x)

    x = [0.0, 0.0, 0.0, 0.0]
    f = obj(x)
    step, evals = 0.5, 1
    while step >= 0.06 and evals < MAX_EVALS:
        improved = False
        for i in range(4):
            for d in (step, -step):
                y = list(x)
                bound = T_BOUND if i == 3 else W_BOUND
                y[i] = max(-bound, min(bound, y[i] + d))
                if y == x:
                    continue
                fy = obj(y)
                evals += 1
                if fy < f - 1e-6:
                    x, f, improved = y, fy, True
                    break
        if not improved:
            step /= 2.0
    return [x[0], 0.0, x[1], x[2]], x[3]


def _pooled_kappa(nights: Sequence[NightData], labels: Sequence[Sequence[Optional[str]]]):
    tt, pp = [], []
    for n, lab in zip(nights, labels):
        for t, p in zip(n.truth, lab):
            if t is not None and p is not None:
                tt.append(t)
                pp.append(p)
    return cohen_kappa(tt, pp)


def _fit_threshold(nights: Sequence[NightData], hmm: dict) -> Optional[float]:
    posts = [_posteriors(n, hmm) for n in nights]
    base = _pooled_kappa(nights, [[decide(p) for p in ps] for ps in posts])
    best, best_k = None, base if base is not None else -1.0
    for th in THRESHOLDS:
        k = _pooled_kappa(nights, [[decide(p, th) for p in ps] for ps in posts])
        if k is not None and k > best_k + MIN_THRESHOLD_GAIN:
            best, best_k = th, k
    return best


def fit(nights: Sequence[NightData], pop: dict) -> dict:
    """All three adjustments from a set of nights."""
    trans = eeg_transitions(nights, pop)
    w, lt = _fit_emission(nights, pop, trans)
    params = {"trans": trans, "log_weights": w, "log_temp": lt}
    params["wake_threshold"] = _fit_threshold(nights, _hmm_with(pop, params))
    return params


def _baseline_hmm(pop: dict) -> dict:
    return _hmm_with(pop, {})


# --------------------------------------------------------------------------- validation + build
def validate(nights: Sequence[NightData], pop: dict) -> dict:
    """Leave-one-night-out: pooled + per-night kappa of the unadjusted stager, the calibration
    fitted without that night, and the calibration plus its wake threshold."""
    base_hmm = _baseline_hmm(pop)
    per, lab_b, lab_c, lab_t = [], [], [], []
    for i, held in enumerate(nights):
        train = [n for j, n in enumerate(nights) if j != i]
        params = fit(train, pop)
        hmm = _hmm_with(pop, params)
        b = _labels(held, base_hmm)
        c = _labels(held, hmm)
        t = _labels(held, hmm, params["wake_threshold"]) if params["wake_threshold"] else c
        lab_b.append(b)
        lab_c.append(c)
        lab_t.append(t)
        kb, kc, kt = (_pooled_kappa([held], [b]), _pooled_kappa([held], [c]),
                      _pooled_kappa([held], [t]))
        per.append({"night_date": held.night_date,
                    "n_epochs": sum(1 for x, y in zip(held.truth, b)
                                    if x is not None and y is not None),
                    "kappa_unadjusted": _r(kb), "kappa_calibrated": _r(kc),
                    "kappa_with_threshold": _r(kt),
                    "fold_wake_threshold": params["wake_threshold"]})
    kb, kc, kt = (_pooled_kappa(nights, lab_b), _pooled_kappa(nights, lab_c),
                  _pooled_kappa(nights, lab_t))
    wins = sum(1 for p in per if p["kappa_calibrated"] is not None
               and p["kappa_unadjusted"] is not None
               and p["kappa_calibrated"] > p["kappa_unadjusted"])
    wins_t = sum(1 for p in per if p["kappa_with_threshold"] is not None
                 and p["kappa_calibrated"] is not None
                 and p["kappa_with_threshold"] > p["kappa_calibrated"])
    return {"kappa_unadjusted": _r(kb), "kappa_calibrated": _r(kc),
            "kappa_with_threshold": _r(kt), "wins": wins, "threshold_wins": wins_t,
            "p_better": _r(bootstrap_p_better(nights, lab_b, lab_c)),
            "p_threshold_better": _r(bootstrap_p_better(nights, lab_c, lab_t)),
            "n_nights": len(nights), "per_night": per}


def _kappa_cm(cm) -> Optional[float]:
    n = sum(sum(r) for r in cm)
    if not n:
        return None
    po = sum(cm[i][i] for i in range(4)) / n
    pe = sum(sum(cm[i]) * sum(cm[r][i] for r in range(4)) for i in range(4)) / float(n * n)
    return (po - pe) / (1.0 - pe) if pe < 1.0 else (1.0 if po >= 1.0 else 0.0)


def bootstrap_p_better(nights: Sequence[NightData], lab_a, lab_b,
                       reps: int = BOOT_REPS) -> Optional[float]:
    """Share of block-bootstrap draws of the held-out epochs in which ``lab_b`` scores a
    higher kappa than ``lab_a``."""
    import random
    idx = {c: i for i, c in enumerate(CLASSES)}
    blocks = []
    for n, a, b in zip(nights, lab_a, lab_b):
        trip = [(idx[t], idx[x], idx[y]) for t, x, y in zip(n.truth, a, b)
                if t is not None and x is not None and y is not None]
        for k in range(0, len(trip), BOOT_BLOCK_EPOCHS):
            ca = [[0] * 4 for _ in range(4)]
            cb = [[0] * 4 for _ in range(4)]
            for t, x, y in trip[k:k + BOOT_BLOCK_EPOCHS]:
                ca[t][x] += 1
                cb[t][y] += 1
            blocks.append((ca, cb))
    if len(blocks) < 2:
        return None
    rng = random.Random(0)
    ahead = 0
    for _ in range(int(reps)):
        sa = [[0] * 4 for _ in range(4)]
        sb = [[0] * 4 for _ in range(4)]
        for _ in range(len(blocks)):
            ca, cb = blocks[rng.randrange(len(blocks))]
            for i in range(4):
                ra, rb, qa, qb = sa[i], sb[i], ca[i], cb[i]
                for j in range(4):
                    ra[j] += qa[j]
                    rb[j] += qb[j]
        ka, kb = _kappa_cm(sa), _kappa_cm(sb)
        if ka is not None and kb is not None and kb > ka:
            ahead += 1
    return ahead / float(reps)


def _r(x, nd=3):
    return None if x is None else round(float(x), nd)


def build_calibration(repo, *, nights: Optional[Sequence[str]] = None,
                      emission_fn: Optional[Callable] = None, stager=None,
                      pop: Optional[dict] = None) -> dict:
    """Fit + validate on the imported nights and return the profile (not yet stored)."""
    pop = pop or population_hmm()
    if nights is None:
        nights = [n["night_date"] for n in imported_nights(repo.conn)][:MAX_NIGHTS]
    if stager is None and emission_fn is None:
        from sleepctl.ml.sleep_staging.infer import SleepStager
        stager = SleepStager.load()
    data, skipped = [], []
    for d in nights:
        nd = load_night(repo, d, emission_fn=emission_fn, stager=stager)
        usable = 0 if nd is None else sum(1 for t, e in zip(nd.truth, nd.emissions)
                                          if t is not None and e is not None)
        if nd is None or usable < 120:        # under an hour of paired epochs
            skipped.append({"night_date": d, "paired_epochs": usable})
            continue
        data.append(nd)
    data.sort(key=lambda n: n.night_date)
    now = datetime.now().replace(microsecond=0).isoformat()
    prof = {"version": PROFILE_VERSION, "fitted_ts": now, "enabled": False,
            "wake_threshold_enabled": False, "n_nights": len(data),
            "nights": [n.night_date for n in data], "skipped": skipped}
    if len(data) < MIN_NIGHTS:
        prof["rationale"] = (f"learning -- {len(data)}/{MIN_NIGHTS} EEG nights paired with "
                             "arm-band data before a calibration can be validated")
        return prof
    val = validate(data, pop)
    params = fit(data, pop)
    hmm = _hmm_with(pop, params)
    gain = (val["kappa_calibrated"] or 0.0) - (val["kappa_unadjusted"] or 0.0)
    enabled = (gain >= MIN_KAPPA_GAIN and val["wins"] * 2 >= len(data)
               and (val["p_better"] or 0.0) >= BOOT_CONFIDENCE)
    t_gain = (val["kappa_with_threshold"] or 0.0) - (val["kappa_calibrated"] or 0.0)
    t_enabled = (enabled and params["wake_threshold"] is not None
                 and t_gain >= MIN_THRESHOLD_GAIN and val["threshold_wins"] * 2 >= len(data)
                 and (val["p_threshold_better"] or 0.0) >= BOOT_CONFIDENCE)
    prof.update({
        "enabled": bool(enabled),
        "wake_threshold_enabled": bool(t_enabled),
        "emission_bias": {c: _r(params["log_weights"][k]) for k, c in enumerate(CLASSES)},
        "temperature": _r(math.exp(params["log_temp"])),
        "wake_threshold": params["wake_threshold"],
        "hmm": {"trans": [[round(v, 6) for v in r] for r in hmm["trans"]],
                "emission_prior": [round(v, 6) for v in hmm["emission_prior"]],
                "temper": round(hmm["temper"], 6)},
        "validation": val,
        "replay": "HR (+ armband counts when recorded), unsmoothed, fresh stager",
    })
    if enabled:
        prof["rationale"] = (f"held-out kappa {val['kappa_unadjusted']:.2f} -> "
                             f"{val['kappa_calibrated']:.2f} over {len(data)} EEG nights "
                             f"(better on {val['wins']}); enabled")
    else:
        prof["rationale"] = (f"held-out kappa {val['kappa_unadjusted']} -> "
                             f"{val['kappa_calibrated']} over {len(data)} EEG nights "
                             f"(better on {val['wins']}, ahead in {val['p_better']} of bootstrap "
                             f"draws) -- needs +{MIN_KAPPA_GAIN}, at least half the nights and "
                             f"{BOOT_CONFIDENCE} of draws; the unadjusted stager stays")
    return prof


# --------------------------------------------------------------------------- storage
def save_calibration(conn, profile: dict) -> None:
    conn.execute(
        "INSERT INTO staging_calibration (id, ts, enabled, n_nights, profile) VALUES (1,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, enabled=excluded.enabled, "
        "n_nights=excluded.n_nights, profile=excluded.profile",
        (profile.get("fitted_ts"), 1 if profile.get("enabled") else 0,
         int(profile.get("n_nights") or 0), json.dumps(profile)))
    conn.commit()


def load_calibration(conn) -> Optional[dict]:
    try:
        row = conn.execute("SELECT profile FROM staging_calibration WHERE id = 1").fetchone()
    except Exception:
        return None
    if row is None or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def calibrate(repo, **kw) -> dict:
    """Fit, validate and store in one step (the "calibrate now" action)."""
    prof = build_calibration(repo, **kw)
    save_calibration(repo.conn, prof)
    return prof


# --------------------------------------------------------------------------- applying it
def _valid_patch(h: dict) -> bool:
    try:
        tr = h["trans"]
        if len(tr) != 4 or any(len(r) != 4 for r in tr):
            return False
        if any(not (0.0 <= float(v) <= 1.0) for r in tr for v in r):
            return False
        if any(abs(sum(float(v) for v in r) - 1.0) > 1e-3 for r in tr):
            return False
        ep = h["emission_prior"]
        if len(ep) != 4 or any(not (float(v) > 0.0) for v in ep):
            return False
        return 0.05 <= float(h["temper"]) <= 5.0
    except Exception:
        return False


def apply_calibration(stager, profile: Optional[dict]) -> Optional[str]:
    """Install an ENABLED calibration on a loaded stager; returns what was applied, or None.

    Transitions always apply. The emission bias / temperature was fitted on the HR and
    HR+motion emissions the replay produces, so it is withheld while a beat-interval (HRV)
    variant is installed, whose emissions it was never checked against. The wake threshold
    needs ``SleepStager.set_wake_threshold`` and is skipped on a stager without it."""
    if not profile or not profile.get("enabled") or stager is None:
        return None
    hmm = getattr(stager, "hmm", None)
    patch = profile.get("hmm") or {}
    if not hmm or not _valid_patch(patch):
        return None
    new = dict(hmm)
    new["trans"] = [[float(v) for v in r] for r in patch["trans"]]
    applied = ["transitions"]
    if not getattr(stager, "hrv_available", False):
        new["emission_prior"] = [float(v) for v in patch["emission_prior"]]
        new["temper"] = float(patch["temper"])
        applied.append("emission bias/temperature")
        # Fitted on unbiased emissions: a marker-derived wake bias on top would count twice.
        if hasattr(stager, "set_wake_bias"):
            stager.set_wake_bias(1.0)
    stager.hmm = new
    th = profile.get("wake_threshold")
    if profile.get("wake_threshold_enabled") and th is not None:
        setter = getattr(stager, "set_wake_threshold", None)
        if callable(setter):
            setter(float(th))
            applied.append(f"wake threshold {float(th):.2f}")
    return " + ".join(applied)


def apply_stored_calibration(repo, stager) -> Optional[str]:
    return apply_calibration(stager, load_calibration(repo.conn))


__all__ = ["NightData", "replay_emissions", "windowed_posteriors", "decide", "eeg_transitions",
           "fit", "validate", "build_calibration", "calibrate", "save_calibration",
           "load_calibration", "apply_calibration", "apply_stored_calibration",
           "population_hmm"]
