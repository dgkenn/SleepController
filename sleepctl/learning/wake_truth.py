"""Calibrate wake detection on declared truth: the marker gestures.

Every arm-shake records the stage the stager held at that declared-awake instant. Their
agreement rate is the only wake-detection score that rests on a fact. When enough exist, the
stager's wake probability is scaled toward the rate that would have caught them -- bounded, so
thin data cannot flood the night with wake."""
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
    for r in rows:
        try:
            d = json.loads(r[0]) if r[0] else {}
        except Exception:
            continue
        st = d.get("stage_at_marker")
        if st is None:
            continue
        n += 1
        awake += 1 if st == "awake" else 0
    if n < min_markers:
        return {"n": n, "agreement": (round(awake / n, 2) if n else None), "bias": 1.0,
                "personalized": False,
                "rationale": f"learning -- {n}/{min_markers} marker gestures before the wake "
                             f"threshold is tuned to your declared awakenings"}
    r = awake / n
    bias = max(BIAS_MIN, min(BIAS_MAX, 1.0 + 1.5 * (TARGET_AGREEMENT - r)))
    return {"n": n, "agreement": round(r, 2), "bias": round(bias, 3), "personalized": True,
            "rationale": (f"{awake}/{n} declared awakenings were scored awake; wake probability "
                          f"scaled x{bias:.2f}")}
