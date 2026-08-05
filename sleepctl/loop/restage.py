"""Re-derive a night's sleep stages from the RAW SENSOR DATA with the current estimator.

``raw_samples.stage`` records whatever the estimator emitted at the time, so a night recorded by
a stale or defective build keeps those labels forever — and every rollup, report and learner
downstream inherits them. 2026-08-04 is the worked example: the deployed build persisted
light 92.3% / deep 2.0% / **REM 0.0%** across the sleep period, while the same code we ship today,
replayed over the *identical* dense Verity series, yields light 51.8 / deep 14.3 / REM 28.4 /
awake 5.5. The physiology was recorded correctly; only the labels were wrong.

This module replays the stored sensor stream through :func:`estimate_sleep_stage` exactly the way
the live daemon feeds it — the dense ~1-sample-per-2s Verity series from ``sensor_samples``, the
same 45-minute trailing window, the same quality exclusions (``hr_frozen`` / ``not_worn``), and
the real bed-entry and onset clocks — and returns the corrected labels.

It deliberately does NOT rewrite ``raw_samples``: those rows stay as the honest record of what the
controller actually believed at the time (an audit trail for anything that acted on them). The
corrected labels are handed to the rollup instead, so the *summary* the learners consume reflects
the physiology rather than a build artifact.

Timestamp care: ``raw_samples.ts`` is naive LOCAL while ``sensor_samples.ts`` is aware UTC (see
the convention in ``sleepctl.storage.schema``). Getting that wrong silently yields empty history
windows and a plausible-looking but meaningless hypnogram.
"""

from __future__ import annotations

import bisect
import statistics
from datetime import datetime
from typing import Optional

from sleepctl.config import AppConfig
from sleepctl.controller.state_estimator import estimate_sleep_stage
from sleepctl.models import SensorFrame, SleepStage

#: Trailing dense-history window handed to the stager, matching ``live_daemon._read_frame``'s
#: ``read_history(minutes=45.0)``. The model's features look back at most 35 min, so this covers
#: them with headroom.
HISTORY_MIN = 45.0

#: Trailing frames used for the settled-sleep HR baseline, matching the controller's own
#: ``_sleep_baseline`` pooling.
_BASELINE_POOL = 15

_STAGES = {"light": SleepStage.LIGHT, "deep": SleepStage.DEEP,
           "rem": SleepStage.REM, "awake": SleepStage.AWAKE}


def _parse(ts) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts))
    except Exception:
        return None


def _dense_hr(conn) -> list:
    """(epoch_seconds, bpm) for every usable wearable HR sample, ascending.

    Applies the same exclusions as ``bridge.sensor_history_series``: a frozen or not-worn HR has
    near-zero variability, which reads to the stager as a strong SLEEP signal.
    """
    rows = conn.execute(
        "SELECT ts, hr FROM sensor_samples"
        " WHERE hr IS NOT NULL AND hr_frozen = 0 AND not_worn = 0"
        " ORDER BY ts ASC").fetchall()
    out = []
    for r in rows:
        t = _parse(r["ts"])
        if t is not None:
            out.append((t.timestamp(), float(r["hr"])))
    return out


def restage_night(repo, night_date: str, cfg: Optional[AppConfig] = None) -> dict:
    """Return ``{iso_ts: stage_label}`` for ``night_date``, re-derived with the current estimator.

    Only ticks from bed entry onward are restaged; the out-of-bed guard in the controller is
    reproduced here by simply not scoring anything outside the in-bed span, so a band left still
    on a charger after wake cannot contribute fake DEEP (which is what put 281 of one night's 296
    deep samples in the following MORNING).
    """
    cfg = cfg or AppConfig.default()
    dense = _dense_hr(repo.conn)
    dense_ts = [d[0] for d in dense]

    rows = repo.conn.execute(
        "SELECT ts, heart_rate, hrv, movement, controller_state FROM raw_samples"
        " WHERE night_date = ? ORDER BY id ASC", (night_date,)).fetchall()
    samples = [(t, r) for r in rows if (t := _parse(r["ts"])) is not None]
    if not samples or not dense:
        return {}

    in_bed = [(t, r) for t, r in samples if (r["controller_state"] or "idle") != "idle"]
    if not in_bed:
        return {}
    bedtime, last_in_bed = in_bed[0][0], in_bed[-1][0]
    asleep = [t for t, r in samples
              if (r["controller_state"] or "") in ("maintenance", "wake_recovery", "wake_window")]
    onset = asleep[0] if asleep else None

    frames = [SensorFrame(timestamp=t, stage=SleepStage.UNKNOWN, heart_rate=r["heart_rate"],
                          hrv=r["hrv"], movement=r["movement"], presence=None)
              for t, r in samples if bedtime <= t <= last_in_bed]

    out: dict = {}
    for i, f in enumerate(frames):
        if f.heart_rate is None:
            continue
        epoch = f.timestamp.astimezone().timestamp()   # naive-local row -> real instant
        lo = bisect.bisect_left(dense_ts, epoch - HISTORY_MIN * 60.0)
        hi = bisect.bisect_right(dense_ts, epoch)
        f.hr_history = dense[lo:hi]

        recent = frames[max(0, i - 30):i]
        pool = [x.heart_rate for x in recent if x.heart_rate is not None][-_BASELINE_POOL:]
        base = statistics.fmean(pool) if pool else None

        est = estimate_sleep_stage(
            f, base, recent, cfg,
            minutes_since_start=(f.timestamp - bedtime).total_seconds() / 60.0,
            minutes_since_onset=((f.timestamp - onset).total_seconds() / 60.0
                                 if onset is not None else None))
        if est is not None:
            out[f.timestamp.isoformat()] = est[0].value
    return out
