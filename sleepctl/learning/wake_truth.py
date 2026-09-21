"""Calibrate wake detection on declared truth: the marker gestures and the morning note.

Every arm-shake or double tap records the stage the stager held at that declared-awake
instant, and every "awake 00:15-00:25" in the morning note contributes the stage held at the
middle of that interval (sleepctl.learning.declared_awakenings). Their agreement rate is the
only wake-detection score that rests on a fact. When enough exist, the stager's wake
probability is scaled toward the rate that would have caught them -- bounded, so thin data
cannot flood the night with wake."""
from __future__ import annotations

import json

MIN_MARKERS = 10
TARGET_AGREEMENT = 0.8
BIAS_MIN, BIAS_MAX = 0.7, 1.6


def wake_truth_profile(repo, nights: int = 30, min_markers: int = MIN_MARKERS) -> dict:
    try:
        rows = repo.conn.execute(
            "SELECT data FROM events WHERE code = 'marker_vs_stage' ORDER BY id DESC LIMIT ?",
            (int(nights) * 40,)).fetchall()
    except Exception:
        rows = []
    n = 0
    awake = 0
    misses = 0          # declared AWAKE, scored asleep -- the detector missed it
    false_alarms = 0    # declared ASLEEP, scored awake -- the detector cried wolf
    n_denied = 0
    for r in rows:
        try:
            d = json.loads(r[0]) if r[0] else {}
        except Exception:
            continue
        st = d.get("stage_at_marker")
        if st is None:
            continue
        # A wake review can declare either direction. A marker gesture only ever means
        # "awake", so an absent flag is awake.
        declared_awake = bool(d.get("declared_awake", True))
        if not declared_awake:
            n_denied += 1
            if st == "awake":
                false_alarms += 1
            continue
        n += 1
        awake += 1 if st == "awake" else 0
        if st != "awake":
            misses += 1
    n_markers = n
    try:
        from sleepctl.learning.declared_awakenings import declared_instants
        declared = declared_instants(repo, nights=nights)
    except Exception:
        declared = []
    for _ts, st in declared:
        n += 1
        awake += 1 if st == "awake" else 0
    n_notes = n - n_markers
    src = f"{n_markers} gesture(s) + {n_notes} from morning notes"
    if n_denied:
        src += f", {n_denied} denied in a wake review"
    if n < min_markers:
        return {"n": n, "n_markers": n_markers, "n_notes": n_notes, "n_denied": n_denied,
                "agreement": (round(awake / n, 2) if n else None), "bias": 1.0,
                "personalized": False,
                "rationale": f"learning -- {n}/{min_markers} declared awakenings ({src}) before "
                             f"the wake threshold is tuned to them"}
    r = awake / n
    # SYMMETRIC. Missing an awakening and inventing one are both errors, and until the wake
    # review existed only the first could ever be measured -- a marker gesture, by
    # construction, is never evidence that the detector cried wolf. A denial is exactly that
    # evidence, and it pulls the bias the other way.
    miss_rate = misses / n
    false_alarm_rate = (false_alarms / n_denied) if n_denied else 0.0
    bias = max(BIAS_MIN, min(BIAS_MAX,
                             1.0 + 1.5 * (miss_rate - (1.0 - TARGET_AGREEMENT) - false_alarm_rate)))
    rationale = (f"{awake}/{n} declared awakenings ({src}) were scored awake; wake "
                 f"probability scaled x{bias:.2f}")
    if n_denied:
        rationale += (f" -- {false_alarms}/{n_denied} denied awakening(s) had been scored "
                      f"awake, which pulls it back down")
    return {"n": n, "n_markers": n_markers, "n_notes": n_notes, "n_denied": n_denied,
            "agreement": round(r, 2), "miss_rate": round(miss_rate, 3),
            "false_alarm_rate": round(false_alarm_rate, 3),
            "bias": round(bias, 3), "personalized": True, "rationale": rationale}
