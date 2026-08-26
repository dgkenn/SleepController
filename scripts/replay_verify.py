"""Replay a captured night through the REAL controller and verify the three detection paths.

Answers, from actual recorded physiology rather than synthetic fixtures:
  [1] SLEEP STAGING     -- does a usable hypnogram come out, and is it stable/plausible?
  [2] FELL-ASLEEP       -- does SleepOnsetDetector confirm onset, and on WHICH signals
                           (including "stillness", the accelerometer-derived one)?
  [3] WAKE DETECTION    -- are awakenings detected, and how many?

This drives ``SleepController.decide`` tick by tick exactly as the daemon does, so it exercises
the real state machine, stager, onset detector and arousal/wake logic -- not reimplementations.
It is READ-ONLY: it opens nothing, writes nothing, and touches no device.

Usage:
    python scripts/replay_verify.py --night path/to/night-2026-08-25.json
    python scripts/replay_verify.py --db sleepctl.db --date 2026-08-25
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _rows_from_night_json(path):
    with open(path) as fh:
        return json.load(fh)["raw_samples"]


def _rows_from_db(db_path, night_date):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ts, stage, stage_confidence, heart_rate, hrv, respiratory_rate, movement, "
        "presence, bed_temp_f, commanded_level, controller_state, wake_event "
        "FROM raw_samples WHERE night_date = ? ORDER BY id ASC", (night_date,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--night", help="a night-<date>.json from the night-data branch")
    ap.add_argument("--db", help="sleepctl.db (use with --date)")
    ap.add_argument("--date", help="night_date when using --db")
    args = ap.parse_args()

    if args.night:
        rows = _rows_from_night_json(args.night)
        label = Path(args.night).stem
    elif args.db and args.date:
        rows = _rows_from_db(args.db, args.date)
        label = args.date
    else:
        ap.error("provide --night, or --db together with --date")

    from sleepctl.config import AppConfig
    from sleepctl.controller.controller import SleepController
    from sleepctl.models import SensorFrame, SleepStage, ControllerState

    cfg = AppConfig()
    controller = SleepController(cfg)

    print("=" * 88)
    print(f"REPLAY VERIFY - {label}   ({len(rows)} recorded samples)")
    print("=" * 88)

    recent: list = []
    errs, err_shown = 0, False
    stages, states, reasons = [], [], []
    onset_seen = None
    for r in rows:
        ts = datetime.fromisoformat(r["ts"])
        frame = SensorFrame(
            timestamp=ts,
            stage=SleepStage.UNKNOWN,          # replay the SENSOR feed, not the stored label,
            stage_confidence=None,             # so staging is genuinely recomputed here
            heart_rate=r["heart_rate"], hrv=r["hrv"],
            respiratory_rate=r["respiratory_rate"], movement=r["movement"],
            presence=r["presence"], bed_temp_f=r["bed_temp_f"],
            commanded_level=r["commanded_level"], data_age_seconds=5.0,
        )
        try:
            controller.decide(frame, None, recent, ts)
        except Exception as exc:              # never let one bad tick end the replay
            if not err_shown:
                print(f"  ! decide() raised at {ts}: {exc!r} (suppressing further)")
                err_shown = True
            errs += 1
        stages.append(getattr(frame.stage, "value", str(frame.stage)))
        states.append(getattr(controller.sm.state, "value", str(controller.sm.state)))
        reasons.append(controller.sm.reason)
        ev = getattr(controller, "last_onset_event", None)
        if ev is not None and onset_seen is None:
            onset_seen = ev
        recent.append(frame)
        if len(recent) > 60:
            recent.pop(0)

    # ---------------------------------------------------------------- 1. staging
    if errs:
        print(f"  ({errs} ticks raised inside decide())")
    print("\n[1] SLEEP STAGING")
    dist = Counter(stages)
    staged = sum(v for k, v in dist.items() if k not in ("unknown", "None"))
    print(f"  labels produced: {staged}/{len(stages)} ({100*staged/max(1,len(stages)):.0f}% of ticks)")
    print(f"  distribution: {dict(dist)}")
    bouts, cur, n = [], None, 0
    for s in stages:
        if s != cur:
            if cur is not None:
                bouts.append((cur, n))
            cur, n = s, 1
        else:
            n += 1
    if cur is not None:
        bouts.append((cur, n))
    real = [b for b in bouts if b[0] not in ("unknown", "None")]
    if real:
        import statistics
        lens = sorted(n for _, n in real)
        print(f"  {len(real)} stage bouts, median {statistics.median(lens)} ticks, "
              f"longest {max(lens)} ticks")
        print("  (a 1-tick median means the classifier is flipping every sample -- real sleep "
              "holds a stage for 15-30 min)")
    else:
        print("  NO usable stage labels produced -- staging did not engage")

    # ---------------------------------------------------------------- 2. fell-asleep
    print("\n[2] FELL-ASLEEP (SleepOnsetDetector)")
    if onset_seen is None:
        print("  onset NEVER confirmed across the whole night")
        print("  -> the 'fell asleep' path did not fire; check staging above and bed entry below")
    else:
        sig = list(getattr(onset_seen, "signals", []) or [])
        print(f"  CONFIRMED at {onset_seen.timestamp}")
        print(f"    confidence : {getattr(onset_seen, 'confidence', None)}")
        print(f"    latency_min: {getattr(onset_seen, 'latency_min', None)}")
        print(f"    signals    : {sig}")
        print(f"    accelerometer (stillness) contributed: {'stillness' in sig}")

    # ---------------------------------------------------------------- 3. wake detection
    print("\n[3] WAKE DETECTION / state machine")
    sdist = Counter(states)
    print(f"  controller states: {dict(sdist)}")
    if set(sdist) == {"idle"}:
        print("  NEVER LEFT IDLE -- no session ran, so nothing downstream could engage")
    transitions = [(i, states[i]) for i in range(1, len(states)) if states[i] != states[i-1]]
    print(f"  {len(transitions)} state transitions")
    for i, st in transitions[:12]:
        print(f"    {rows[i]['ts']}  -> {st:<14} ({reasons[i]})")
    if len(transitions) > 12:
        print(f"    ... and {len(transitions)-12} more")
    awake_runs, run = 0, 0
    for s in stages:
        if s == "awake":
            run += 1
        else:
            if run >= 2:
                awake_runs += 1
            run = 0
    if run >= 2:
        awake_runs += 1
    print(f"  sustained awake runs (>=2 ticks) inside the night: {awake_runs}")


if __name__ == "__main__":
    main()
