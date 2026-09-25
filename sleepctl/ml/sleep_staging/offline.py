"""Non-causal (offline) HMM smoothing of the stager's emissions — PURE standard library.

The live stager (:meth:`infer.SleepStager.predict`) runs a CAUSAL forward filter: at 3 am it
cannot see 3:10 am, so a stage is only called once its evidence has outweighed the transition
prior for several epochs. The morning hypnogram has no such limit. Here the same per-epoch
emissions (the blended wake + 4-class posteriors, before any smoothing) and the same HMM
(transitions, start, emission prior, temper — the personal HMM when one is active) are smoothed
with the epochs AFTER each one as well:

  * :func:`windowed_posteriors` (the default): the live filter's own trailing window, restarted
    from ``start`` exactly as live, times a short lookahead of :data:`LOOKAHEAD_EPOCHS`;
  * :func:`forward_backward`: posterior marginals over the WHOLE night;
  * :func:`viterbi`: the single most likely path over the whole night.

Held-out BIDSleep subjects (fold models that never saw them; ``docs/BIDSLEEP_TRAINING.md``,
all figures after the hypnogram constraints): posterior marginals beat Viterbi on every variant
(4-class kappa 0.464 vs 0.425, HR only), but over the whole night they call far too much REM
(43% of sleep against 29% true). The class-balanced heads lean to REM, a run of near-duplicate
epochs compounds that lean, and the live filter's short window, restarted from ``start``, is
what caps it. Keeping that window and adding a lookahead keeps the cap and still uses the
future: kappa 0.456 -> 0.475, wake kappa 0.662 -> 0.681 (HR only; +0.02 on HR + motion and on
sparse HR). A lookahead of 5 epochs is what the training script's own smoothing rule picks
(best mean of 4-class and wake kappa, ties to the least deep error). It still calls more REM
(35%) and slightly more deep than the live filter, so per-night stage MINUTES do not improve
(REM error 45 -> 50 min/night): the epochs are better placed, the totals are not.

Epochs come from a replay at irregular tick times, so :func:`smooth_night` bins them onto the
30 s grid the HMM was estimated on. An epoch with no emission (a sensor dropout, a tick scored
by something other than the model) carries no evidence: its likelihood is 1 and the transition
structure alone bridges it. Several emissions in one epoch are averaged, not multiplied —
consecutive scores share almost all of their trailing window, which is the same reason the
live filter tempers its likelihoods.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

from .infer import DEFAULT_SMOOTHING_EPOCHS, EPOCH_S, STAGE4_LABELS

#: "posterior" (marginals) or "viterbi" (whole-night MAP path).
DEFAULT_METHOD = "posterior"

#: Epochs after each one that the default smoother reads (2.5 min).
LOOKAHEAD_EPOCHS = 5

#: Longest night grid handled (24 h of 30 s epochs); anything longer is not a night.
MAX_EPOCHS = 2880

Emission = Optional[Sequence[float]]


def _likelihoods(emissions: Sequence[Emission], prior: Sequence[float],
                 temper: float) -> List[Optional[List[float]]]:
    """Posterior -> tempered likelihood, exactly as :func:`infer.forward_filter` does it."""
    n = len(prior)
    a = float(temper)
    out: List[Optional[List[float]]] = []
    for e in emissions:
        if e is None:
            out.append(None)
            continue
        out.append([(max(float(e[k]), 1e-9) / max(float(prior[k]), 1e-9)) ** a
                    for k in range(n)])
    return out


def _step(alpha: Optional[List[float]], lik: Optional[List[float]], trans, start) -> List[float]:
    n = len(start)
    pred = (list(start) if alpha is None
            else [sum(alpha[i] * trans[i][k] for i in range(n)) for k in range(n)])
    post = [max(pred[k], 1e-12) * (lik[k] if lik is not None else 1.0) for k in range(n)]
    tot = sum(post)
    return [p / tot for p in post] if tot > 0 else [1.0 / n] * n


def _back(beta: List[float], lik: Optional[List[float]], trans) -> List[float]:
    n = len(beta)
    lk = lik if lik is not None else [1.0] * n
    nb = [sum(trans[i][k] * lk[k] * beta[k] for k in range(n)) for i in range(n)]
    s = sum(nb)
    return [b / s for b in nb] if s > 0 else [1.0] * n


def _combine(a: Sequence[float], b: Sequence[float]) -> List[float]:
    g = [x * y for x, y in zip(a, b)]
    tot = sum(g)
    return [x / tot for x in g] if tot > 0 else [1.0 / len(g)] * len(g)


def forward_backward(emissions: Sequence[Emission], trans: Sequence[Sequence[float]],
                     start: Sequence[float], prior: Sequence[float],
                     temper: float = 1.0) -> List[List[float]]:
    """Posterior marginals for every epoch given the WHOLE run. ``None`` epochs carry no
    evidence. Scaled recursions, so a night of any length cannot underflow."""
    T = len(emissions)
    if T == 0:
        return []
    lik = _likelihoods(emissions, prior, temper)
    alphas: List[List[float]] = []
    alpha: Optional[List[float]] = None
    for t in range(T):
        alpha = _step(alpha, lik[t], trans, start)
        alphas.append(alpha)
    out: List[List[float]] = [[]] * T
    beta = [1.0] * len(start)
    for t in range(T - 1, -1, -1):
        out[t] = _combine(alphas[t], beta)
        if t:
            beta = _back(beta, lik[t], trans)
    return out


def windowed_posteriors(emissions: Sequence[Emission], trans: Sequence[Sequence[float]],
                        start: Sequence[float], prior: Sequence[float], temper: float = 1.0,
                        lookback: int = DEFAULT_SMOOTHING_EPOCHS,
                        lookahead: int = LOOKAHEAD_EPOCHS) -> List[List[float]]:
    """Marginal of each epoch on the chain from ``lookback - 1`` epochs before it (started from
    ``start``, as the live filter is) to ``lookahead`` epochs after it. ``lookahead=0`` is the
    live forward filter; an unbounded window both ways is :func:`forward_backward`."""
    T = len(emissions)
    lik = _likelihoods(emissions, prior, temper)
    W = max(1, int(lookback))
    L = max(0, int(lookahead))
    out: List[List[float]] = []
    for t in range(T):
        alpha: Optional[List[float]] = None
        for j in range(max(0, t - W + 1), t + 1):
            alpha = _step(alpha, lik[j], trans, start)
        beta = [1.0] * len(start)
        for j in range(min(T - 1, t + L), t, -1):
            beta = _back(beta, lik[j], trans)
        out.append(_combine(alpha, beta))
    return out


def viterbi(emissions: Sequence[Emission], trans: Sequence[Sequence[float]],
            start: Sequence[float], prior: Sequence[float], temper: float = 1.0) -> List[int]:
    """Most likely state path (indices into :data:`infer.STAGE4_LABELS`)."""
    n = len(start)
    T = len(emissions)
    if T == 0:
        return []
    lik = _likelihoods(emissions, prior, temper)
    lt = [[math.log(max(float(trans[i][k]), 1e-12)) for k in range(n)] for i in range(n)]

    def _ll(t: int) -> List[float]:
        return [0.0] * n if lik[t] is None else [math.log(max(x, 1e-300)) for x in lik[t]]
    ll0 = _ll(0)
    delta = [math.log(max(float(start[k]), 1e-12)) + ll0[k] for k in range(n)]
    back: List[List[int]] = []
    for t in range(1, T):
        llt = _ll(t)
        nd, bp = [0.0] * n, [0] * n
        for k in range(n):
            best = max(range(n), key=lambda i: delta[i] + lt[i][k])
            bp[k] = best
            nd[k] = delta[best] + lt[best][k] + llt[k]
        delta = nd
        back.append(bp)
    path = [max(range(n), key=lambda k: delta[k])]
    for bp in reversed(back):
        path.append(bp[path[-1]])
    path.reverse()
    return path


def label_of(post: Sequence[float]) -> str:
    """The stage a posterior names, with the live rule that wake wins at p >= 0.5."""
    if float(post[0]) >= 0.5:
        return STAGE4_LABELS[0]
    return STAGE4_LABELS[max(range(len(post)), key=lambda k: post[k])]


def smooth_night(times: Sequence[float], emissions: Sequence[Emission], hmm: dict, *,
                 method: str = DEFAULT_METHOD, whole_night: bool = False,
                 lookahead: int = LOOKAHEAD_EPOCHS,
                 epoch_s: float = EPOCH_S) -> List[Optional[dict]]:
    """Offline smoothing of emissions observed at ``times`` (epoch seconds, any order).

    ``method="posterior"`` gives marginals: over the live trailing window plus ``lookahead``
    epochs, or over the whole night with ``whole_night=True``. ``method="viterbi"`` is the
    whole-night path. Returns one entry per input, ``{"stage": label, "probs": {label: p}}``,
    or ``None`` for an input without an emission; with Viterbi, ``probs`` are the whole-night
    marginals (for a confidence), the stage is the path's.
    """
    obs = [(float(t), e) for t, e in zip(times, emissions) if e is not None]
    if not obs or not hmm:
        return [None] * len(times)
    t0 = min(t for t, _ in obs)
    idx = [int(round((float(t) - t0) / epoch_s)) for t in times]
    n_ep = max(i for i, e in zip(idx, emissions) if e is not None) + 1
    if n_ep > MAX_EPOCHS:
        raise ValueError(f"{n_ep} epochs is not a night")
    acc: Dict[int, List[float]] = {}
    cnt: Dict[int, int] = {}
    for i, e in zip(idx, emissions):
        if e is None:
            continue
        a = acc.setdefault(i, [0.0] * len(STAGE4_LABELS))
        for k in range(len(a)):
            a[k] += float(e[k])
        cnt[i] = cnt.get(i, 0) + 1
    grid: List[Emission] = [None] * n_ep
    for i, a in acc.items():
        grid[i] = [x / cnt[i] for x in a]
    trans = hmm["trans"]
    start = hmm.get("start") or hmm["prior"]
    # the heads are class-balanced, so their effective prior is uniform (as in predict)
    prior = hmm.get("emission_prior") or hmm["prior"]
    temper = float(hmm.get("temper", 1.0))
    path = None
    if method == "viterbi" or whole_night:
        post = forward_backward(grid, trans, start, prior, temper)
        if method == "viterbi":
            path = viterbi(grid, trans, start, prior, temper)
    else:
        post = windowed_posteriors(
            grid, trans, start, prior, temper,
            lookback=int(hmm.get("smoothing_epochs", DEFAULT_SMOOTHING_EPOCHS)),
            lookahead=lookahead)
    out: List[Optional[dict]] = []
    for i, e in zip(idx, emissions):
        if e is None:
            out.append(None)
            continue
        p = post[i]
        stage = STAGE4_LABELS[path[i]] if path is not None else label_of(p)
        out.append({"stage": stage,
                    "probs": {lbl: float(p[k]) for k, lbl in enumerate(STAGE4_LABELS)}})
    return out


__all__ = ["DEFAULT_METHOD", "LOOKAHEAD_EPOCHS", "forward_backward", "windowed_posteriors",
           "viterbi", "label_of", "smooth_night"]
