"""How well did the stager agree with an EEG headband on the same night?

Aligns an imported hypnogram (``eeg_hypnogram``, see ``sleepctl.eval.hypnogram_import``) with
two label streams for the same night and scores each against it:

  * ``recorded`` -- ``raw_samples.stage``, what the controller believed and acted on at the time;
  * ``restaged`` -- the same night replayed through today's estimator
    (``sleepctl.loop.restage.restage_night``), i.e. what the current build would say.

Per stream: Cohen's kappa and accuracy over the four classes, a confusion matrix (rows = EEG,
columns = stager), per-stage minutes on the aligned epochs, and wake detection: for every EEG
awakening after sleep onset, how long until the stager said awake (or that it never did), plus
the minutes it called awake while the headband scored sleep.

Alignment: each 30 s EEG epoch takes the LAST stager label issued inside it -- the belief the
controller held as the epoch closed -- falling back to the latest label from the epoch before
it, so a tick landing a second early is not lost. Controller ticks are keyed on ``sample_ts``
(the tick's own clock; ``ts`` is the pod frame time and lags up to a minute, see
``sleepctl.storage.schema``) and both streams are compared as real instants: the naive-local
rows are read as local time, never as UTC.
"""

from __future__ import annotations

import bisect
import statistics
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from sleepctl.eval.hypnogram_import import CLASSES, load_epochs

#: An EEG awakening shorter than this is an arousal, not something the controller could or
#: should react to; only runs of at least this many epochs count toward wake detection.
MIN_WAKE_BOUT_EPOCHS = 2
#: A stager "awake" this many epochs after the EEG bout ended still counts as catching it
#: late (the stager's trailing windows lag), rather than as a miss plus a false alarm.
WAKE_GRACE_EPOCHS = 4

_IDX = {c: i for i, c in enumerate(CLASSES)}
_ALIASES = {"wake": "awake"}


def _norm(stage) -> Optional[str]:
    s = _ALIASES.get(str(stage or "").lower(), str(stage or "").lower())
    return s if s in _IDX else None


def cohen_kappa(truth: Sequence[str], pred: Sequence[str]) -> Optional[float]:
    """Unweighted kappa over the four classes; None when there is nothing to score."""
    n = len(truth)
    if n == 0:
        return None
    k = len(CLASSES)
    cm = confusion(truth, pred)
    po = sum(cm[i][i] for i in range(k)) / n
    rows = [sum(cm[i]) for i in range(k)]
    cols = [sum(cm[i][j] for i in range(k)) for j in range(k)]
    pe = sum(rows[i] * cols[i] for i in range(k)) / float(n * n)
    if pe >= 1.0:
        return 1.0 if po >= 1.0 else 0.0
    return (po - pe) / (1.0 - pe)


def confusion(truth: Sequence[str], pred: Sequence[str]) -> List[List[int]]:
    cm = [[0] * len(CLASSES) for _ in CLASSES]
    for t, p in zip(truth, pred):
        if t in _IDX and p in _IDX:
            cm[_IDX[t]][_IDX[p]] += 1
    return cm


def _to_unix(ts) -> Optional[float]:
    """A stored timestamp -> Unix seconds. Naive = LOCAL (the engine-table convention)."""
    try:
        return datetime.fromisoformat(str(ts)).timestamp()
    except Exception:
        return None


def recorded_labels(conn, night_date: str) -> List[Tuple[float, str]]:
    """``(unix, stage)`` for every controller tick of the night that carried a stage."""
    try:
        rows = conn.execute(
            "SELECT COALESCE(sample_ts, ts) AS t, stage FROM raw_samples "
            "WHERE night_date = ? AND stage IS NOT NULL ORDER BY id ASC", (night_date,)).fetchall()
    except Exception:
        rows = conn.execute(
            "SELECT ts, stage FROM raw_samples WHERE night_date = ? AND stage IS NOT NULL "
            "ORDER BY id ASC", (night_date,)).fetchall()
    out = []
    for r in rows:
        t = _to_unix(r[0])
        if t is not None:
            out.append((t, str(r[1])))
    out.sort(key=lambda x: x[0])
    return out


def labels_from_map(stage_by_ts: Dict[str, str]) -> List[Tuple[float, str]]:
    """``restage_night``'s ``{naive_local_iso: stage}`` -> sorted ``(unix, stage)``."""
    out = [(t, s) for k, s in (stage_by_ts or {}).items() if (t := _to_unix(k)) is not None]
    out.sort(key=lambda x: x[0])
    return out


def align(epochs: Sequence[Tuple[float, float, str]],
          labels: Sequence[Tuple[float, str]]) -> List[Optional[str]]:
    """One stager label (or None) per EEG epoch; see the module docstring for the rule."""
    ts = [t for t, _ in labels]
    out: List[Optional[str]] = []
    for start, dur, _ in epochs:
        j = bisect.bisect_left(ts, start + dur) - 1      # last label strictly inside the epoch end
        if j >= 0 and ts[j] >= start - dur:
            out.append(_norm(labels[j][1]))
        else:
            out.append(None)
    return out


def _wake_detection(truth: List[Optional[str]], pred: List[Optional[str]], epoch_s: float
                    ) -> dict:
    onset = next((i for i, t in enumerate(truth) if t in ("light", "deep", "rem")), None)
    bouts = []
    if onset is not None:
        i = onset
        while i < len(truth):
            if truth[i] == "awake":
                j = i
                while j < len(truth) and truth[j] == "awake":
                    j += 1
                if j - i >= MIN_WAKE_BOUT_EPOCHS:
                    bouts.append((i, j))
                i = j
            else:
                i += 1
    latencies, missed = [], 0
    for a, b in bouts:
        hit = next((k for k in range(a, min(len(pred), b + WAKE_GRACE_EPOCHS))
                    if pred[k] == "awake"), None)
        if hit is None:
            missed += 1
        else:
            latencies.append((hit - a) * epoch_s / 60.0)
    false_wake = sum(1 for t, p in zip(truth, pred)
                     if t in ("light", "deep", "rem") and p == "awake") * epoch_s / 60.0
    return {
        "eeg_awakenings": len(bouts),
        "detected": len(latencies),
        "missed": missed,
        "latency_median_min": round(statistics.median(latencies), 1) if latencies else None,
        "latency_mean_min": round(statistics.fmean(latencies), 1) if latencies else None,
        "latencies_min": [round(x, 1) for x in latencies],
        "false_wake_min": round(false_wake, 1),
        "min_bout_min": MIN_WAKE_BOUT_EPOCHS * epoch_s / 60.0,
    }


def compare(epochs: Sequence[Tuple[float, float, str]], pred: Sequence[Optional[str]]) -> dict:
    """Score one aligned label stream against the EEG epochs."""
    epoch_s = epochs[0][1] if epochs else 30.0
    truth = [_norm(e[2]) for e in epochs]
    pairs = [(t, p) for t, p in zip(truth, pred) if t is not None and p is not None]
    tt = [t for t, _ in pairs]
    pp = [p for _, p in pairs]
    scored = sum(1 for t in truth if t is not None)
    kappa = cohen_kappa(tt, pp)
    mins = lambda seq: {c: round(sum(1 for s in seq if s == c) * epoch_s / 60.0, 1)  # noqa: E731
                        for c in CLASSES}
    per_class = {}
    cm = confusion(tt, pp)
    for i, c in enumerate(CLASSES):
        tp = cm[i][i]
        row, col = sum(cm[i]), sum(cm[r][i] for r in range(len(CLASSES)))
        per_class[c] = {"recall": round(tp / row, 3) if row else None,
                        "precision": round(tp / col, 3) if col else None}
    return {
        "n_epochs": len(pairs),
        "coverage": round(len(pairs) / scored, 3) if scored else 0.0,
        "kappa": round(kappa, 3) if kappa is not None else None,
        "accuracy": round(sum(1 for t, p in pairs if t == p) / len(pairs), 3) if pairs else None,
        "classes": list(CLASSES),
        "confusion": cm,
        "per_class": per_class,
        "minutes": {"eeg": mins(tt), "stager": mins(pp)},
        "wake": _wake_detection(truth, list(pred), epoch_s),
    }


def agreement_report(repo, night_date: str, *, include_restage: bool = True,
                     restaged: Optional[Dict[str, str]] = None, cfg=None) -> dict:
    """The full agreement report for one imported night (see the module docstring)."""
    conn = repo.conn
    epochs = load_epochs(conn, night_date)
    if not epochs:
        return {"night_date": night_date, "available": False,
                "reason": "no EEG hypnogram imported for this night"}
    epoch_s = epochs[0][1]
    truth_all = [e[2] for e in epochs]
    try:
        src = conn.execute("SELECT MIN(source) FROM eeg_hypnogram WHERE night_date = ?",
                           (night_date,)).fetchone()[0]
    except Exception:
        src = None
    out = {
        "night_date": night_date, "available": True, "source": src, "epoch_s": epoch_s,
        "start": datetime.fromtimestamp(epochs[0][0]).isoformat(),
        "end": datetime.fromtimestamp(epochs[-1][0] + epoch_s).isoformat(),
        "eeg_minutes": {c: round(truth_all.count(c) * epoch_s / 60.0, 1)
                        for c in CLASSES + ("unknown",)},
        "streams": {},
    }
    rec = recorded_labels(conn, night_date)
    out["streams"]["recorded"] = (compare(epochs, align(epochs, rec)) if rec
                                  else {"n_epochs": 0, "reason": "no controller stages recorded"})
    if include_restage:
        try:
            if restaged is None:
                from sleepctl.loop.restage import restage_night
                restaged = restage_night(repo, night_date, cfg)
            lab = labels_from_map(restaged)
            out["streams"]["restaged"] = (compare(epochs, align(epochs, lab)) if lab else
                                          {"n_epochs": 0, "reason": "no raw sensor data to replay"})
        except Exception as exc:  # the replay is best-effort; the recorded comparison stands
            out["streams"]["restaged"] = {"n_epochs": 0, "reason": f"restage failed: {exc}"}
    return out


__all__ = ["agreement_report", "compare", "align", "cohen_kappa", "confusion",
           "recorded_labels", "labels_from_map"]
