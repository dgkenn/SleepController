"""Personal stage-transition priors for the stager's smoothing model.

The bundled HMM's transition matrix and stage prior are population values from the training
set. After enough of this user's own nights, the transitions between their own
high-confidence epochs are blended in -- a strong pseudo-count keeps the population shape
until the personal counts genuinely outweigh it."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

CLASSES = ["wake", "light", "deep", "rem"]
_IDX = {"awake": 0, "wake": 0, "light": 1, "deep": 2, "rem": 3}
MIN_NIGHTS = 5
PSEUDO_COUNT = 200.0     # per row: population shape worth this many observed epochs
EPOCH_S = 30.0


def _dedupe_epochs(rows) -> List[tuple]:
    """One (t, stage) per 30-second epoch (the daemon ticks twice per epoch)."""
    out, last_k = [], None
    for ts, stage, conf in rows:
        try:
            t = datetime.fromisoformat(str(ts)).timestamp()
        except Exception:
            continue
        k = int(t // EPOCH_S)
        if k == last_k:
            continue
        last_k = k
        out.append((t, stage))
    return out


def learn_transitions(repo, population: dict, nights: int = 14, min_nights: int = MIN_NIGHTS,
                      min_conf: float = 0.55) -> dict:
    """``{"trans": 4x4, "prior": 4, "n_nights", "n_epochs", "personalized", "rationale"}``.
    ``population`` is the bundled hmm dict (``trans``, ``prior``)."""
    pop_t = population.get("trans")
    pop_p = population.get("prior") or [0.25] * 4
    if not pop_t:
        return {"personalized": False, "rationale": "no population model"}
    try:
        dates = [r[0] for r in repo.conn.execute(
            "SELECT DISTINCT night_date FROM raw_samples WHERE night_date IS NOT NULL "
            "AND controller_state IN ('maintenance','wake_recovery','wake_window') "
            "ORDER BY night_date DESC LIMIT ?", (int(nights),)).fetchall()]
    except Exception:
        dates = []
    counts = [[0.0] * 4 for _ in range(4)]
    occ = [0.0] * 4
    n_epochs = 0
    used_nights = 0
    for d in dates:
        rows = repo.conn.execute(
            "SELECT ts, stage, stage_confidence FROM raw_samples WHERE night_date = ? AND "
            "controller_state IN ('maintenance','wake_recovery','wake_window') AND stage IS NOT NULL "
            "ORDER BY ts ASC", (d,)).fetchall()
        rows = [(r[0], r[1], r[2]) for r in rows if r[1] in _IDX and (r[2] is None or float(r[2]) >= min_conf)]
        ep = _dedupe_epochs(rows)
        if len(ep) < 60:
            continue
        used_nights += 1
        prev = None
        for t, st in ep:
            i = _IDX[st]
            occ[i] += 1
            n_epochs += 1
            if prev is not None and t - prev[0] <= 2.5 * EPOCH_S:
                counts[_IDX[prev[1]]][i] += 1
            prev = (t, st)
    if used_nights < min_nights:
        return {"personalized": False, "n_nights": used_nights, "n_epochs": n_epochs,
                "rationale": f"learning -- {used_nights}/{min_nights} nights before the stage "
                             "transitions are tuned to your own hypnogram"}
    trans = []
    for i in range(4):
        row_n = sum(counts[i])
        row = [(counts[i][j] + PSEUDO_COUNT * float(pop_t[i][j])) / (row_n + PSEUDO_COUNT)
               for j in range(4)]
        s = sum(row)
        trans.append([v / s for v in row] if s > 0 else list(pop_t[i]))
    tot = sum(occ)
    prior = [(occ[j] + PSEUDO_COUNT * float(pop_p[j])) / (tot + 4 * PSEUDO_COUNT) for j in range(4)]
    s = sum(prior)
    prior = [v / s for v in prior]
    return {"personalized": True, "n_nights": used_nights, "n_epochs": n_epochs,
            "trans": [[round(v, 5) for v in r] for r in trans], "prior": [round(v, 5) for v in prior],
            "rationale": f"stage transitions blended from {n_epochs} of your own epochs over "
                         f"{used_nights} nights (population shape worth {PSEUDO_COUNT:.0f} per row)"}
