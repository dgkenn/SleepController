"""Three hypnograms, one answer: our live stager vs Apple's sleep stages vs Bevel's.

Every morning Health Auto Export brings in the night's sleep stages from every app that writes
them into Apple Health (``health_sleep``: Apple's own, and Bevel's if Bevel writes them). Our
live stager's labels are already on disk (``raw_samples.stage``). This module lines the three up
on 30-second epochs, decides what the night most likely was, measures how each algorithm
behaves, and hands the agreed parts to the existing personal calibration
(``learning.eeg_calibration``) as its reference.

How disagreements are handled
-----------------------------
None of the three is ground truth -- only an EEG night is, and one imported through the EEG path
always wins. So instead of picking a favourite, each source is modelled as a noisy rater with its
own **confusion matrix** (Dawid & Skene 1979): P(source says l | night really was k). The
matrices and the night's true stages are estimated together by EM over every night on file:

* a source is trusted on a stage exactly as far as it has agreed with the others there -- if
  Bevel calls REM what both others call light, its REM column learns that, and a lone Bevel REM
  stops carrying weight;
* each matrix starts from a prior that says "mostly right" (Apple strongest: its stages are
  validated against lab PSG; ours weakest), and the data take over within a few nights;
* the fitted matrices ARE the reverse-engineered fingerprint of each algorithm's habits, kept in
  the report as the start of the "learn their algorithms" work.

The label our stager is calibrated against is the posterior from **Apple and Bevel only**:
letting our own vote pull the label toward our own output would teach the stager nothing. Ours
still helps estimate the others' matrices. An epoch becomes a training label only at posterior
>= :data:`CONF_MIN`; below that it abstains. "Asleep (unspecified)" and "in bed" are not stages
and abstain too. Apple and Bevel both read the same watch, so their errors are not independent
and agreement between them is somewhat over-counted; the confidence threshold and the
calibration's own held-out gate (it switches on only when it beats the unadjusted stager on
nights it was not fitted on) are the guards against that.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from sleepctl.eval.eeg_agreement import _to_unix, align, cohen_kappa, recorded_labels
from sleepctl.eval.hypnogram_import import CLASSES, imported_nights, local_naive

EPOCH_S = 30.0
CONF_MIN = 0.7                 # reference posterior needed for an epoch to become a label
MAX_NIGHTS = 21                # recent nights considered
MIN_REF_EPOCHS = 120           # an hour of referenced epochs before a night counts
EM_ITERS = 25
SOURCES = ("apple", "bevel", "ours", "ours_replay")
REFERENCES = ("apple", "bevel")
#: Prior pseudo-counts on each confusion row: this many "agreed" epochs on the diagonal, one per
#: off-diagonal cell. Apple's stages are PSG-validated; Bevel's method is unpublished; ours is
#: what is being corrected.
PRIOR_DIAG = {"apple": 40.0, "bevel": 20.0, "ours": 10.0, "ours_replay": 6.0}
#: A night whose live labels cover less of the grid than this gets "ours_replay" instead: our
#: stager run afterwards on Apple Health's overnight heart rate (a reading every few minutes, far
#: sparser than the live feed). Kept as its own source -- a different, weaker input -- and only
#: for the comparison: the calibration replays the live feed, which such a night does not have.
LIVE_COVERAGE_MIN = 0.2
REPLAY_STALE_S = 900.0         # newest Health HR reading at most 15 min old
REPLAY_MIN_HR = 3              # readings in the trailing 45 min
_K = len(CLASSES)
_IDX = {c: i for i, c in enumerate(CLASSES)}
_HEALTH_STAGE = {"core": "light", "light": "light", "deep": "deep", "rem": "rem",
                 "awake": "awake", "wake": "awake"}

CONSENSUS_DDL = """
CREATE TABLE IF NOT EXISTS consensus_hypnogram (
    night_date TEXT NOT NULL,
    epoch_unix REAL NOT NULL,      -- epoch START, UTC seconds
    epoch_ts TEXT NOT NULL,        -- naive LOCAL, like raw_samples.ts
    epoch_s REAL NOT NULL DEFAULT 30,
    stage TEXT NOT NULL,           -- awake|light|deep|rem, or unknown (abstained)
    confidence REAL,               -- reference posterior of that stage
    apple TEXT, bevel TEXT, ours TEXT,
    PRIMARY KEY (night_date, epoch_unix)
);
CREATE TABLE IF NOT EXISTS staging_consensus (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts TEXT,
    report TEXT
);
"""


def ensure_schema(conn) -> None:
    conn.executescript(CONSENSUS_DDL)


def source_kind(src) -> Optional[str]:
    """Which algorithm wrote a Health sleep sample, from the writing app/device name."""
    s = str(src or "").lower()
    if "bevel" in s:
        return "bevel"
    if not s or any(k in s for k in ("watch", "iphone", "apple", "health")):
        return "apple"
    return None   # another app (AutoSleep, Pillow, ...) -- not one of the three


def _iso_unix(s) -> Optional[float]:
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        return None
    return dt.timestamp()


# --------------------------------------------------------------------------- assembling a night
def recent_nights(conn, limit: int = MAX_NIGHTS) -> List[Tuple[str, float, float]]:
    """(night_date, first, last) of our recorded nights, newest first, as unix seconds."""
    try:
        rows = conn.execute(
            "SELECT night_date, MIN(COALESCE(sample_ts, ts)), MAX(COALESCE(sample_ts, ts)) "
            "FROM raw_samples WHERE night_date IS NOT NULL GROUP BY night_date "
            "ORDER BY night_date DESC LIMIT ?", (limit,)).fetchall()
    except Exception:
        return []
    out = []
    for nd, lo, hi in rows:
        a, b = _to_unix(lo), _to_unix(hi)
        if a is not None and b is not None and b > a:
            out.append((str(nd), a, b))
    return out


def _segments(conn, lo: float, hi: float) -> Dict[str, List[Tuple[float, float, str]]]:
    """Health sleep segments overlapping [lo, hi), per source kind, mapped to our classes."""
    out: Dict[str, List[Tuple[float, float, str]]] = {k: [] for k in REFERENCES}
    lo_s = datetime.fromtimestamp(lo - 86400, timezone.utc).isoformat()
    hi_s = datetime.fromtimestamp(hi + 86400, timezone.utc).isoformat()
    try:
        rows = conn.execute("SELECT start, end, stage, source FROM health_sleep "
                            "WHERE end > ? AND start < ?", (lo_s, hi_s)).fetchall()
    except Exception:
        return out
    for st, en, stage, src in rows:
        kind, cls = source_kind(src), _HEALTH_STAGE.get(str(stage or "").lower())
        a, b = _iso_unix(st), _iso_unix(en)
        if kind in out and cls and a is not None and b is not None and b > lo and a < hi:
            out[kind].append((a, b, cls))
    for v in out.values():
        v.sort()
    return out


def _label_at(segs: Sequence[Tuple[float, float, str]], t: float) -> Optional[str]:
    for a, b, c in segs:
        if a <= t < b:
            return c
        if a > t:
            break
    return None


def assemble_night(conn, night_date: str, lo: float, hi: float,
                   replay=None) -> Optional[dict]:
    """The night's epoch grid with each source's label (None where it says nothing)."""
    segs = _segments(conn, lo, hi)
    starts = [a for v in segs.values() for a, _, _ in v]
    ends = [b for v in segs.values() for _, b, _ in v]
    if not starts:
        return None
    g0 = max(lo, min(starts))
    g1 = min(hi, max(ends))
    if g1 - g0 < EPOCH_S:
        return None
    g0 = math.floor(g0 / EPOCH_S) * EPOCH_S
    times = [g0 + i * EPOCH_S for i in range(int((g1 - g0) // EPOCH_S))]
    labels = {k: [_label_at(segs[k], t + EPOCH_S / 2) for t in times] for k in REFERENCES}
    ours = recorded_labels(conn, night_date)
    labels["ours"] = align([(t, EPOCH_S, "") for t in times], ours) if ours else [None] * len(times)
    labels["ours_replay"] = [None] * len(times)
    live = sum(1 for v in labels["ours"] if v) / len(times)
    if live < LIVE_COVERAGE_MIN and replay is not None:
        try:
            got = replay(conn, times)
        except Exception:
            got = None
        if got:
            labels["ours_replay"] = got
    return {"night_date": night_date, "times": times, "labels": labels}


def health_hr(conn, lo: float, hi: float) -> List[Tuple[float, float]]:
    """Apple Health heart-rate readings (``apple_health``) in [lo, hi], as (unix, bpm)."""
    a = datetime.fromtimestamp(lo, timezone.utc).isoformat()
    b = datetime.fromtimestamp(hi, timezone.utc).isoformat()
    try:
        rows = conn.execute("SELECT ts, value FROM apple_health WHERE metric = 'heart_rate' "
                            "AND ts >= ? AND ts <= ? ORDER BY ts", (a, b)).fetchall()
    except Exception:
        return []
    out = []
    for ts, v in rows:
        t = _iso_unix(ts)
        if t is not None and v is not None:
            out.append((t, float(v)))
    return out


def make_health_replay(stager=None):
    """A ``replay(conn, times)`` that stages a night from Apple Health's heart rate with the
    bundled, un-personalised stager and the live smoothing, for nights with no live feed."""
    import bisect

    from sleepctl.learning.eeg_calibration import (HISTORY_MIN, _hmm_with, decide,
                                                   population_hmm, windowed_posteriors)
    state = {"stager": stager, "hmm": None}

    def replay(conn, times):
        hr = health_hr(conn, times[0] - HISTORY_MIN * 60.0, times[-1] + EPOCH_S)
        if len(hr) < 10:
            return None
        if state["stager"] is None:
            from sleepctl.ml.sleep_staging.infer import SleepStager
            state["stager"] = SleepStager.load()
        st = state["stager"]
        if not getattr(st, "available", False):
            return None
        if state["hmm"] is None:
            state["hmm"] = _hmm_with(population_hmm(), {})
        ts = [t for t, _ in hr]
        em = []
        for t in times:
            end = t + EPOCH_S
            lo, hi = bisect.bisect_left(ts, end - HISTORY_MIN * 60.0), bisect.bisect_right(ts, end)
            if hi - lo < REPLAY_MIN_HR or ts[hi - 1] < end - REPLAY_STALE_S:
                em.append(None)
                continue
            try:
                est = st.predict(hr[lo:hi], minutes_since_start=(end - times[0]) / 60.0,
                                 minutes_since_onset=None, smooth=False)
            except Exception:
                est = None
            if est is None:
                em.append(None)
                continue
            pr = est.probs
            em.append([float(pr.get("wake", 0.0)), float(pr.get("light", 0.0)),
                       float(pr.get("deep", 0.0)), float(pr.get("rem", 0.0))])
        if not any(em):
            return None
        h = state["hmm"]
        post = windowed_posteriors(em, h["trans"], h["start"], h["emission_prior"],
                                   h["temper"], h["window"])
        return [decide(p) for p in post]

    return replay


# --------------------------------------------------------------------------- Dawid-Skene
def _prior_matrix(src: str) -> List[List[float]]:
    d = PRIOR_DIAG.get(src, 10.0)
    return [[d + 1.0 if k == l else 1.0 for l in range(_K)] for k in range(_K)]


def _normalize(row: List[float]) -> List[float]:
    s = sum(row)
    return [v / s for v in row] if s > 0 else [1.0 / len(row)] * len(row)


def dawid_skene(obs: Sequence[Dict[str, int]], iters: int = EM_ITERS):
    """EM over epochs of {source: class index}. Returns (confusion per source, class prior,
    full posteriors). Epochs with no observation are skipped by the caller."""
    srcs = sorted({s for o in obs for s in o})
    # start from a weighted vote so EM begins near "the sources mostly agree"
    post = []
    for o in obs:
        v = [1e-3] * _K
        for s, l in o.items():
            v[l] += PRIOR_DIAG.get(s, 10.0)
        post.append(_normalize(v))
    conf: Dict[str, List[List[float]]] = {}
    prior = [1.0 / _K] * _K
    for _ in range(iters):
        counts = {s: _prior_matrix(s) for s in srcs}
        pc = [1.0] * _K
        for o, p in zip(obs, post):
            for k in range(_K):
                pc[k] += p[k]
                for s, l in o.items():
                    counts[s][k][l] += p[k]
        conf = {s: [_normalize(r) for r in counts[s]] for s in srcs}
        prior = _normalize(pc)
        post = [_posterior(o, conf, prior) for o in obs]
    return conf, prior, post


def _posterior(o: Dict[str, int], conf, prior, only: Optional[Sequence[str]] = None):
    logp = [math.log(prior[k]) for k in range(_K)]
    for s, l in o.items():
        if only is not None and s not in only:
            continue
        for k in range(_K):
            logp[k] += math.log(max(conf[s][k][l], 1e-9))
    m = max(logp)
    return _normalize([math.exp(v - m) for v in logp])


# --------------------------------------------------------------------------- the whole pass
def build(conn, nights: Optional[Sequence[Tuple[str, float, float]]] = None,
          replay="health") -> dict:
    """Assemble, fit, store the consensus epochs and return the agreement report.

    ``replay``: "health" stages nights without a live feed from Apple Health's heart rate (see
    :func:`make_health_replay`); a callable is used as is; None disables it."""
    ensure_schema(conn)
    if replay == "health":
        replay = make_health_replay()
    nights = list(nights if nights is not None else recent_nights(conn))
    assembled = [a for nd, lo, hi in nights if (a := assemble_night(conn, nd, lo, hi, replay))]
    obs_all: List[Dict[str, int]] = []
    where: List[Tuple[int, int]] = []
    for ni, a in enumerate(assembled):
        for ei in range(len(a["times"])):
            o = {s: _IDX[a["labels"][s][ei]] for s in SOURCES if a["labels"][s][ei] in _IDX}
            if any(s in o for s in REFERENCES):
                obs_all.append(o)
                where.append((ni, ei))
    report: dict = {"fitted_ts": datetime.now().replace(microsecond=0).isoformat(),
                    "classes": list(CLASSES), "conf_min": CONF_MIN, "nights": []}
    if not obs_all:
        report["rationale"] = "no Apple/Bevel sleep stages overlapping a recorded night yet"
        _save_report(conn, report)
        return report
    conf, prior, _ = dawid_skene(obs_all)
    ref_post = [_posterior(o, conf, prior, only=REFERENCES) for o in obs_all]

    per_night: Dict[int, dict] = {}
    rows = []
    for (ni, ei), o, p in zip(where, obs_all, ref_post):
        a = assembled[ni]
        k = max(range(_K), key=lambda i: p[i])
        stage = CLASSES[k] if p[k] >= CONF_MIN else "unknown"
        t = a["times"][ei]
        ours_l = a["labels"]["ours"][ei] or (f"replay:{a['labels']['ours_replay'][ei]}"
                                             if a["labels"]["ours_replay"][ei] else None)
        rows.append((a["night_date"], t, local_naive(t), EPOCH_S, stage, round(p[k], 4),
                     a["labels"]["apple"][ei], a["labels"]["bevel"][ei], ours_l))
        per_night.setdefault(ni, {"n": 0, "labelled": 0})
        per_night[ni]["n"] += 1
        per_night[ni]["labelled"] += stage != "unknown"
    for ni in {ni for ni, _ in where}:
        conn.execute("DELETE FROM consensus_hypnogram WHERE night_date = ?",
                     (assembled[ni]["night_date"],))
    conn.executemany("INSERT OR REPLACE INTO consensus_hypnogram (night_date, epoch_unix, "
                     "epoch_ts, epoch_s, stage, confidence, apple, bevel, ours) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()

    pooled: Dict[str, Tuple[list, list]] = {}
    for ni, a in enumerate(assembled):
        cons = {t: r[4] for r in rows if r[0] == a["night_date"] for t in [r[1]]}
        entry = {"night_date": a["night_date"],
                 "epochs": per_night.get(ni, {}).get("n", 0),
                 "labelled": per_night.get(ni, {}).get("labelled", 0),
                 "sources": [s for s in SOURCES if any(a["labels"][s])],
                 "kappa": {}}
        streams = dict(a["labels"])
        streams["consensus"] = [cons.get(t) if cons.get(t) in _IDX else None for t in a["times"]]
        for x, y in (("apple", "bevel"), ("apple", "ours"), ("bevel", "ours"),
                     ("ours", "consensus"), ("ours_replay", "consensus"),
                     ("apple", "ours_replay"), ("bevel", "ours_replay")):
            pairs = [(u, v) for u, v in zip(streams[x], streams[y]) if u in _IDX and v in _IDX]
            if len(pairs) >= MIN_REF_EPOCHS:
                kap = cohen_kappa([u for u, _ in pairs], [v for _, v in pairs])
                entry["kappa"][f"{x}~{y}"] = None if kap is None else round(kap, 3)
                pt = pooled.setdefault(f"{x}~{y}", ([], []))
                pt[0].extend(u for u, _ in pairs)
                pt[1].extend(v for _, v in pairs)
        report["nights"].append(entry)
    report["pooled_kappa"] = {k: (None if (kv := cohen_kappa(*v)) is None else round(kv, 3))
                              for k, v in pooled.items()}
    report["confusion"] = {s: [[round(v, 3) for v in r] for r in m] for s, m in conf.items()}
    report["class_prior"] = [round(v, 3) for v in prior]
    report["n_nights"] = sum(1 for e in report["nights"] if e["labelled"] >= MIN_REF_EPOCHS)
    _save_report(conn, report)
    return report


def consensus_epochs(conn, night_date: str) -> List[Tuple[float, float, str]]:
    """``(epoch_unix, epoch_s, stage)`` -- the calibration's reference loader (``epochs_fn``)."""
    try:
        rows = conn.execute("SELECT epoch_unix, epoch_s, stage FROM consensus_hypnogram "
                            "WHERE night_date = ? ORDER BY epoch_unix", (night_date,)).fetchall()
    except Exception:
        return []
    return [(float(r[0]), float(r[1] or EPOCH_S), str(r[2])) for r in rows]


def consensus_nights(conn) -> List[str]:
    try:
        rows = conn.execute(
            "SELECT night_date FROM consensus_hypnogram WHERE stage != 'unknown' "
            "GROUP BY night_date HAVING COUNT(*) >= ? ORDER BY night_date DESC",
            (MIN_REF_EPOCHS,)).fetchall()
    except Exception:
        return []
    return [r[0] for r in rows]


def _save_report(conn, report: dict) -> None:
    conn.execute("INSERT INTO staging_consensus (id, ts, report) VALUES (1,?,?) "
                 "ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, report=excluded.report",
                 (report.get("fitted_ts"), json.dumps(report)))
    conn.commit()


def load_report(conn) -> Optional[dict]:
    try:
        ensure_schema(conn)
        row = conn.execute("SELECT report FROM staging_consensus WHERE id = 1").fetchone()
        return json.loads(row[0]) if row and row[0] else None
    except Exception:
        return None


# --------------------------------------------------------------------------- calibrating ours
def calibrate(repo, *, stager=None, emission_fn=None) -> dict:
    """Refit the personal calibration against the consensus -- unless EEG nights exist, which
    are real ground truth and keep the calibration slot. Stored only through the existing
    profile (enabled only when it beats the unadjusted stager on held-out nights)."""
    from sleepctl.learning import eeg_calibration as ec
    if len(imported_nights(repo.conn)) >= ec.MIN_NIGHTS:
        return {"skipped": "EEG nights on file -- they stay the calibration's reference"}
    nights = consensus_nights(repo.conn)[:ec.MAX_NIGHTS]
    prof = ec.build_calibration(repo, nights=nights, stager=stager, emission_fn=emission_fn,
                                epochs_fn=consensus_epochs, reference="Apple/Bevel consensus")
    ec.save_calibration(repo.conn, prof)
    return prof


def run(repo, **kw) -> dict:
    """The morning pass: rebuild the consensus, then recalibrate against it."""
    report = build(repo.conn)
    out = {"report": report}
    # Apple-measured night outcomes + the recovery score, and the deepen/REM nudges re-graded by
    # Apple's hypnogram (learning.recovery): what experiments and the steer learners are judged by.
    try:
        from sleepctl.learning.recovery import build as build_reference
        out["reference"] = build_reference(repo.conn)
    except Exception as exc:
        out["reference"] = {"error": repr(exc)[:200]}
    if report.get("n_nights", 0) > 0:
        out["calibration"] = calibrate(repo, **kw)
    return out


def snapshot(conn) -> dict:
    """Agreement figures only -- no stage minutes or times (the health branch is public)."""
    rep = load_report(conn)
    if not rep:
        return {"status": "no comparison yet"}
    out = {"fitted": rep.get("fitted_ts"), "nights": rep.get("n_nights", 0),
           "pooled_kappa": rep.get("pooled_kappa"),
           "sources": sorted({s for n in rep.get("nights", []) for s in n.get("sources", [])})}
    try:
        from sleepctl.learning.eeg_calibration import load_calibration
        cal = load_calibration(conn) or {}
        out["calibration"] = {"reference": cal.get("reference"), "enabled": cal.get("enabled"),
                              "rationale": str(cal.get("rationale") or "")[:200]}
    except Exception:
        pass
    return out


__all__ = ["source_kind", "assemble_night", "dawid_skene", "build", "consensus_epochs",
           "consensus_nights", "calibrate", "run", "snapshot", "load_report", "CONF_MIN"]
