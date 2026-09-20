"""Does each stage the stager called look like that stage in the signals it was called from?

There is no polysomnography here, so stage ACCURACY cannot be measured. What can be measured is
PLAUSIBILITY: sleep stages have physiological signatures that are established independently of
any wearable model, and the same night's heart rate, beat-interval variability, breathing and
movement either show them or do not.

    deep (N3)  heart rate at its lowest of the night, breathing at its most regular, vagal
               (high-frequency) variability at its highest, almost no movement
    REM        breathing IRREGULAR (the signature that separates REM from N2 without EEG),
               movement near zero (atonia), heart rate and its variability above deep
    wake       movement, or heart rate, above what light sleep shows

Measured on 2026-09-19 the stager called 216 minutes of REM and 1 minute of deep. This audit
says which of those calls the physiology supports. It is deliberately relative -- every metric
is ranked within the night's own distribution -- so the answer does not depend on absolute
units, sensor placement or the person's resting rate.

Pure functions over the night export dict (``dashboard/api/app/night_export.py``): usable on
the box at export time and off-box on the published JSON.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Dict, List, Optional

STAGES = ("awake", "light", "deep", "rem")
EPOCH_S = 30.0
#: A stage needs this many epochs (minutes * 2) before its signature can be judged.
MIN_EPOCHS = 20
#: Breathing irregularity is the coefficient of variation of the breathing rate over this many
#: epochs centred on the epoch (5 minutes).
RESP_CV_EPOCHS = 10
#: REM must be at least this much more irregular in breathing than light sleep.
REM_RESP_RATIO = 1.15
#: Deep must sit at or below this heart-rate percentile (median over its epochs).
DEEP_HR_PCTL_MAX = 0.45
#: Adult norms, as a share of total sleep (Ohayon 2004; wide on purpose).
DEEP_SHARE = (0.08, 0.30)
REM_SHARE = (0.12, 0.35)


def _naive_local_to_epoch(ts: str, offset_s: int) -> Optional[int]:
    """Night exports carry naive LOCAL timestamps on samples and UTC epoch seconds elsewhere."""
    try:
        t = datetime.fromisoformat(str(ts)).replace(tzinfo=timezone.utc).timestamp() - offset_s
        return int(t // EPOCH_S)
    except Exception:
        return None


def _pct_rank(sorted_vals: List[float], v: float) -> float:
    if not sorted_vals:
        return 0.5
    import bisect
    return bisect.bisect_left(sorted_vals, v) / max(1, len(sorted_vals) - 1) if len(sorted_vals) > 1 else 0.5


def _median(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def build_epochs(export: dict) -> List[dict]:
    """One row per 30 s epoch of the night's session (non-idle ticks with a stage call)."""
    offset = int(export.get("local_utc_offset_s") or 0)
    rows: Dict[int, dict] = {}
    for smp in export.get("raw_samples") or []:
        st = str(smp.get("stage") or "unknown")
        if st not in STAGES or str(smp.get("controller_state")) in ("idle", "None"):
            continue
        k = _naive_local_to_epoch(smp.get("ts"), offset)
        if k is None:
            continue
        row = rows.setdefault(k, {"k": k, "stage": st, "hr": None, "mov": None, "resp": None,
                                  "rmssd": None, "hf": None, "pim": None})
        if smp.get("heart_rate") is not None and row["hr"] is None:
            row["hr"] = float(smp["heart_rate"])
        if smp.get("movement") is not None:
            row["mov"] = max(float(smp["movement"]), row["mov"] or 0.0)
        if smp.get("respiratory_rate") is not None and row["resp"] is None:
            row["resp"] = float(smp["respiratory_rate"])
    for w in export.get("hrv_windows") or []:
        try:
            k = int(float(w["t"]) // EPOCH_S)
        except Exception:
            continue
        for kk in (k, k + 1):                       # a 60 s window covers two epochs
            if kk in rows:
                if rows[kk]["rmssd"] is None and w.get("ibi_rmssd") is not None:
                    rows[kk]["rmssd"] = float(w["ibi_rmssd"])
                if rows[kk]["hf"] is None and w.get("ibi_hf") is not None:
                    rows[kk]["hf"] = float(w["ibi_hf"])
    for e in export.get("actigraphy_epochs") or []:
        try:
            k = int(float(e["t"]) // EPOCH_S)
        except Exception:
            continue
        if k in rows and e.get("pim_mean") is not None:
            rows[k]["pim"] = float(e["pim_mean"])
    out = [rows[k] for k in sorted(rows)]
    # Each row's DURATION. The daemon logs two samples per Pod poll under the poll's own
    # timestamp (~61 s apart), so counting rows -- or 30 s buckets -- halves every stage.
    # A row lasts until the next distinct sample, capped so a data hole is not "sleep".
    for i, r in enumerate(out):
        nxt = out[i + 1]["k"] if i + 1 < len(out) else None
        gap = (nxt - r["k"]) * EPOCH_S if nxt is not None else None
        r["dur_min"] = (min(gap, 120.0) / 60.0) if gap is not None else 0.5
    if len(out) > 2:
        med = statistics.median(r["dur_min"] for r in out[:-1])
        out[-1]["dur_min"] = med
    # breathing irregularity: CV of the breathing rate over a centred window
    resp = [r["resp"] for r in out]
    half = RESP_CV_EPOCHS // 2
    for i, r in enumerate(out):
        win = [x for x in resp[max(0, i - half):i + half + 1] if x is not None]
        if len(win) >= 4 and statistics.mean(win) > 0:
            r["resp_cv"] = statistics.pstdev(win) / statistics.mean(win)
        else:
            r["resp_cv"] = None
    # night-relative percentile ranks
    for key in ("hr", "rmssd", "hf", "pim", "mov", "resp_cv"):
        vals = sorted(r[key] for r in out if r.get(key) is not None)
        for r in out:
            r[key + "_p"] = _pct_rank(vals, r[key]) if r.get(key) is not None else None
    return out


def _stage_profile(rows: List[dict], stage: str) -> dict:
    mine = [r for r in rows if r["stage"] == stage]
    prof = {"n_epochs": len(mine), "minutes": round(sum(r.get("dur_min", 0.5) for r in mine), 1)}
    for key in ("hr", "rmssd", "hf", "pim", "mov", "resp_cv"):
        prof[key + "_pctl"] = (round(m, 3) if (m := _median([r.get(key + "_p") for r in mine])) is not None else None)
    prof["resp_cv"] = (round(m, 4) if (m := _median([r.get("resp_cv") for r in mine])) is not None else None)
    moving = [r for r in mine if r.get("pim") is not None]
    if moving:
        thresh = 1.0
        prof["moving_share"] = round(sum(1 for r in moving if r["pim"] >= thresh) / len(moving), 3)
    else:
        prof["moving_share"] = None
    return prof


def stage_consistency(export: dict) -> dict:
    """Judge each called stage against its physiological signature. Never raises."""
    try:
        rows = build_epochs(export)
    except Exception as exc:
        return {"error": repr(exc), "verdicts": {}, "supported": [], "unsupported": []}
    profiles = {s: _stage_profile(rows, s) for s in STAGES}
    light, deep, rem, wake = profiles["light"], profiles["deep"], profiles["rem"], profiles["awake"]
    verdicts: Dict[str, dict] = {}

    def judge(stage: str, prof: dict, checks: List[tuple]) -> None:
        if prof["n_epochs"] < MIN_EPOCHS:
            verdicts[stage] = {"verdict": "insufficient", "minutes": prof["minutes"],
                               "reasons": [f"only {prof['minutes']} min called"]}
            return
        passed, failed, unknown = [], [], []
        for name, ok in checks:
            (passed if ok is True else failed if ok is False else unknown).append(name)
        verdict = "unsupported" if failed else ("supported" if passed else "insufficient")
        verdicts[stage] = {"verdict": verdict, "minutes": prof["minutes"], "passed": passed,
                           "failed": failed, "unmeasured": unknown}

    def cmp(a, b, op) -> Optional[bool]:
        if a is None or b is None:
            return None
        return bool(op(a, b))

    # DEEP: lowest heart rate, most regular breathing, high vagal variability, still
    judge("deep", deep, [
        (f"heart rate low in the night (median percentile {deep['hr_pctl']} <= {DEEP_HR_PCTL_MAX})",
         cmp(deep["hr_pctl"], DEEP_HR_PCTL_MAX, lambda a, b: a <= b)),
        (f"breathing no more irregular than light ({deep['resp_cv']} vs {light['resp_cv']})",
         cmp(deep["resp_cv"], light["resp_cv"], lambda a, b: a <= b * 1.25 + 0.005)),
        (f"vagal variability at or above light (RMSSD percentile {deep['rmssd_pctl']} vs {light['rmssd_pctl']})",
         cmp(deep["rmssd_pctl"], light["rmssd_pctl"], lambda a, b: a >= b - 0.05)),
        (f"no more movement than light ({deep['moving_share']} vs {light['moving_share']} of epochs moving)",
         cmp(deep["moving_share"], light["moving_share"], lambda a, b: a <= b + 0.02)),
    ])
    # REM: irregular breathing, atonia, heart rate above deep
    judge("rem", rem, [
        (f"breathing more irregular than light ({rem['resp_cv']} vs {light['resp_cv']}, needs x{REM_RESP_RATIO})",
         cmp(rem["resp_cv"], light["resp_cv"], lambda a, b: a >= b * REM_RESP_RATIO)),
        (f"movement no higher than light ({rem['moving_share']} vs {light['moving_share']} of epochs moving)",
         cmp(rem["moving_share"], light["moving_share"], lambda a, b: a <= b + 0.02)),
        (f"heart rate at or above deep (percentile {rem['hr_pctl']} vs {deep['hr_pctl']})",
         cmp(rem["hr_pctl"], deep["hr_pctl"], lambda a, b: a >= b - 0.05) if deep["n_epochs"] >= MIN_EPOCHS else None),
    ])
    # WAKE: movement or heart rate above light
    mv = cmp(wake["moving_share"], light["moving_share"], lambda a, b: a > b + 0.05)
    hr = cmp(wake["hr_pctl"], light["hr_pctl"], lambda a, b: a > b + 0.05)
    either = None if (mv is None and hr is None) else bool(mv or hr)
    judge("awake", wake, [
        (f"more movement or higher heart rate than light (moving {wake['moving_share']} vs {light['moving_share']}; "
         f"HR percentile {wake['hr_pctl']} vs {light['hr_pctl']})", either),
    ])
    asleep = sum(profiles[s]["n_epochs"] for s in ("light", "deep", "rem"))
    shares = {}
    norms = []
    asleep_min = sum(profiles[s]["minutes"] for s in ("light", "deep", "rem"))
    if asleep >= 120 and asleep_min > 0:
        shares = {s: round(profiles[s]["minutes"] / asleep_min, 3) for s in ("light", "deep", "rem")}
        if not (DEEP_SHARE[0] <= shares["deep"] <= DEEP_SHARE[1]):
            norms.append(f"deep is {shares['deep']:.0%} of sleep (adult norm {DEEP_SHARE[0]:.0%}-{DEEP_SHARE[1]:.0%})")
        if not (REM_SHARE[0] <= shares["rem"] <= REM_SHARE[1]):
            norms.append(f"REM is {shares['rem']:.0%} of sleep (adult norm {REM_SHARE[0]:.0%}-{REM_SHARE[1]:.0%})")
    supported = [s for s, v in verdicts.items() if v["verdict"] == "supported"]
    unsupported = [s for s, v in verdicts.items() if v["verdict"] == "unsupported"]
    summary = _summary(verdicts, norms)
    return {"n_epochs": len(rows), "profiles": profiles, "verdicts": verdicts, "shares": shares,
            "outside_norms": norms, "supported": supported, "unsupported": unsupported,
            "summary": summary}


def _summary(verdicts: dict, norms: List[str]) -> str:
    parts = []
    for s in ("deep", "rem", "awake"):
        v = verdicts.get(s) or {}
        if v.get("verdict") == "unsupported":
            parts.append(f"{s}: {v['minutes']} min called but {'; '.join(v['failed'])}")
        elif v.get("verdict") == "supported":
            parts.append(f"{s}: {v['minutes']} min, signature holds")
        elif v.get("verdict") == "insufficient":
            parts.append(f"{s}: {'; '.join(v.get('reasons') or ['not measurable'])}")
    if norms:
        parts.append("; ".join(norms))
    return " | ".join(parts) if parts else "no session epochs to judge"


def format_report(night: str, res: dict) -> str:
    lines = [f"{night}: {res.get('summary')}"]
    for s in ("awake", "light", "deep", "rem"):
        p = (res.get("profiles") or {}).get(s) or {}
        if not p:
            continue
        lines.append(f"  {s:5s} {p.get('minutes', 0):6.1f} min  HR pctl {p.get('hr_pctl')}  RMSSD pctl {p.get('rmssd_pctl')}  "
                     f"resp CV {p.get('resp_cv')}  moving {p.get('moving_share')}")
    return "\n".join(lines)
