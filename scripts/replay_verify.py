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

    # Hand the controller tonight's ideal architecture, exactly as the daemon does at session
    # start (live_daemon._apply_night_type). Without it `night_targets` is None and
    # `_evaluate_steering` returns on its very FIRST gate every tick -- so section [4] would
    # report "steering never activated" for every night ever replayed, as a property of this
    # harness rather than of the controller. Best-effort: if planning fails, say so, because
    # "steering held" and "steering was never evaluated" are completely different findings.
    steering_armed = False
    try:
        from sleepctl.controller.sleep_plan import plan_night
        first_ts = datetime.fromisoformat(rows[0]["ts"])
        plan = plan_night(first_ts, None, [])
        controller.set_night_targets(plan.targets, plan.est_sleep_min)
        steering_armed = controller.night_targets is not None
    except Exception as exc:
        print(f"  ! could not plan night targets ({exc!r}) -- steering cannot be evaluated")

    print("=" * 88)
    print(f"REPLAY VERIFY - {label}   ({len(rows)} recorded samples)")
    print("=" * 88)

    recent: list = []
    decisions: list = []
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
            decisions.append(controller.decide(frame, None, recent, ts))
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

    # ------------------------------------------------- 4. steering / awakening prevention
    # Sections 1-3 answer "did it notice?". This one answers "did it DO anything, and could
    # it tell whether that worked?" -- which is the half that was never checked here, and the
    # half where a night can look perfectly healthy while the loop is open the whole time.
    print("\n[4] STEERING / AWAKENING PREVENTION")
    if not decisions:
        print("  no decisions produced -- nothing to steer with")
    else:
        intents = Counter(getattr(d.thermal_intent, "value", str(d.thermal_intent))
                          for d in decisions)
        actions = Counter(getattr(d.action, "value", str(d.action)) for d in decisions)
        print(f"  thermal intents: {dict(intents)}")
        print(f"  actions        : {dict(actions)}")

        lv = [d.target_level for d in decisions if d.target_level is not None]
        tf = [d.target_temp_f for d in decisions if d.target_temp_f is not None]
        if lv:
            print(f"  commanded level: {min(lv)}..{max(lv)} (mean {sum(lv)/len(lv):.1f})")
        if tf:
            print(f"  target temp F  : {min(tf):.1f}..{max(tf):.1f} (mean {sum(tf)/len(tf):.1f})")

        # Is the thermal loop CLOSED? composite_temp_f is None exactly when there is no measured
        # bed temperature, which drops resolve() into open-loop feedforward -- the inversion
        # amplifies demanded cooling by 1/composite_bed_weight with nothing measuring the result.
        closed = sum(1 for d in decisions
                     if (d.log_payload or {}).get("composite_temp_f") is not None)
        pct = 100.0 * closed / len(decisions)
        print(f"  thermal feedback: closed-loop on {closed}/{len(decisions)} ticks ({pct:.0f}%)"
              + ("" if closed else "  <-- FULLY OPEN-LOOP: no measured bed temperature all night"))

        # Awakening precursors: ticks where the controller saw wake evidence at all, and what
        # it did about them. A precursor count of zero with awakenings in section 3 means the
        # prevention path never got the chance to act.
        sig_ticks = [d for d in decisions if (d.log_payload or {}).get("wake_signals")]
        sigs = Counter()
        for d in sig_ticks:
            ws = (d.log_payload or {}).get("wake_signals") or []
            for x in (ws if isinstance(ws, (list, tuple)) else [ws]):
                sigs[str(x)] += 1
        print(f"  ticks carrying wake signals: {len(sig_ticks)}")
        if sigs:
            print(f"    signal counts: {dict(sigs.most_common(10))}")
        acted = [d for d in sig_ticks
                 if getattr(d.action, "value", str(d.action)) not in ("hold", "none")]
        print(f"    of those, {len(acted)} produced a thermal correction rather than a hold")

        wake_actions = [d for d in decisions if (d.log_payload or {}).get("wake_action")]
        print(f"  explicit wake actions: {len(wake_actions)}")
        for d in wake_actions[:5]:
            print(f"    {d.timestamp}  {(d.log_payload or {}).get('wake_action')}")

        if not steering_armed:
            print("  steering NOT EVALUATED -- no night targets, so the steerer returned on its "
                  "first gate every tick. Nothing below is a statement about the steerer.")
        steered = [d for d in decisions if (d.log_payload or {}).get("steering")]
        judged = [d for d in steered
                  if ((d.log_payload or {}).get("steering") or {}).get("reason") is not None]
        print(f"  ticks with a steering summary: {len(steered)} "
              f"({len(judged)} where the steerer actually ran and gave a reason)")
        acts = Counter(((d.log_payload or {}).get("steering") or {}).get("maneuver")
                       for d in steered)
        print(f"    maneuvers: {dict(acts)}")
        on = [d for d in steered
              if ((d.log_payload or {}).get("steering") or {}).get("active")
              or ((d.log_payload or {}).get("steering") or {}).get("defending")]
        print(f"    ticks actively nudging or defending: {len(on)}")
        if steered:
            print(f"    last: {(steered[-1].log_payload or {}).get('steering')}")

        # Data-quality holds are only interesting INSIDE the session: the long idle stretch with
        # no wearable attached legitimately holds, and counting it makes a healthy night look
        # broken. Scope it to ticks where the controller was actually running a night.
        in_session = [d for d in decisions
                      if getattr(d.state, "value", str(d.state))
                      not in ("idle", "calibration")]
        holds = [d for d in in_session
                 if getattr(d.action, "value", str(d.action)) == "hold"
                 and "data quality low" in (d.reason or "")]
        if in_session:
            pct = 100.0 * len(holds) / len(in_session)
            line = (f"  data-quality holds INSIDE the session: {len(holds)}/{len(in_session)} "
                    f"ticks ({pct:.0f}%)")
            print(line + ("  <-- steering was disabled for most of the night" if pct >= 50
                          else ""))
        else:
            print("  no in-session ticks -- the controller never left idle")


if __name__ == "__main__":
    main()
