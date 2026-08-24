"""Did last night actually work? Reconstructs the real night from raw_samples (independent of
whether the morning checkin/rollup has run yet) and answers, in order: did the sensor capture
correctly, did the sleep-architecture scoring compute, did staging/wake-detection look sane, did
thermal steering actually act. Read-only -- this only reads sleepctl.db and computes; it writes
nothing.

Usage:
    .venv\\Scripts\\python.exe sleep_audit.py --db <path to sleepctl.db> --date 2026-08-23
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--date", required=True, help='night_date, e.g. "2026-08-23" (the evening it started)')
    args = ap.parse_args()

    # Import the real engine code rather than re-deriving any of this logic.
    from sleepctl.loop.night_rollup import reconstruct_night_summary
    from sleepctl.benchmarks import perfect_sleep_index, NightMode
    from sleepctl.storage.repository import Repository

    repo = Repository(args.db, check_same_thread=False)
    conn = repo.conn
    conn.row_factory = sqlite3.Row

    print("=" * 96)
    print(f"SLEEP AUDIT — {args.date}")
    print("=" * 96)

    # ---------------------------------------------------------------- 1. sensor capture
    print("\n[1] SENSOR CAPTURE")
    rows = conn.execute(
        "SELECT ts, stage, stage_confidence, heart_rate, hrv, respiratory_rate, movement, "
        "presence, bed_temp_f, controller_state, wake_event "
        "FROM raw_samples WHERE night_date = ? ORDER BY id ASC",
        (args.date,),
    ).fetchall()
    n = len(rows)
    print(f"  {n} raw_samples rows for {args.date}")
    if n == 0:
        print("  NOTHING RECORDED for this date -- check the --date value (the night_date the")
        print("  daemon uses is usually the evening's calendar date, not the morning's).")
        return
    hr_n = sum(1 for r in rows if r["heart_rate"] is not None)
    mv_n = sum(1 for r in rows if r["movement"] is not None)
    stage_n = sum(1 for r in rows if r["stage"] not in (None, "unknown"))
    print(f"  heart_rate present: {hr_n}/{n} ({100*hr_n/n:.0f}%)")
    print(f"  movement present:   {mv_n}/{n} ({100*mv_n/n:.0f}%)")
    print(f"  usable stage label: {stage_n}/{n} ({100*stage_n/n:.0f}%)")
    first_ts, last_ts = rows[0]["ts"], rows[-1]["ts"]
    print(f"  spans {first_ts} -> {last_ts}")

    # ---------------------------------------------------------------- 2. architecture
    print("\n[2] IDEAL SLEEP ARCHITECTURE")
    night = reconstruct_night_summary(repo, args.date)
    print(f"  total_sleep_min={night.total_sleep_min}  deep_min={night.deep_min}  "
          f"rem_min={night.rem_min}  light_min={night.light_min}")
    print(f"  sleep_onset_latency_min={night.sleep_onset_latency_min}  "
          f"waso_min={night.waso_min}  wake_events={night.wake_events}  "
          f"sleep_efficiency={night.sleep_efficiency}")
    print(f"  avg_hr={night.avg_hr}  avg_hrv={night.avg_hrv}  "
          f"avg_respiratory_rate={night.avg_respiratory_rate}")
    if night.total_sleep_min:
        psi = perfect_sleep_index(night, NightMode.NORMAL)
        print(f"  Perfect Sleep Index: {psi.get('score')}/100 (mode={psi.get('mode')})")
        print(f"    components: {psi.get('components')}")
        print(f"    targets met: {psi.get('targets_met')}")
        if psi.get("notes"):
            print(f"    notes: {psi.get('notes')}")
    else:
        print("  no total_sleep_min reconstructed -- too little evidence to score (see [1] above)")

    # ---------------------------------------------------------------- 2b. onset detection
    # SleepOnsetDetector's confirmed result used to be computed live and discarded -- nothing
    # logged which signals actually fired. As of the _maybe_log_onset fix, a confirmed onset
    # writes an `events` row (category="sleep", code="onset_confirmed") naming every signal that
    # voted, including "stillness" -- the accelerometer/movement-derived one. Scoped to this
    # night's own time span since `events` has no night_date column of its own.
    print("\n[2b] SLEEP ONSET DETECTION")
    onset_events = conn.execute(
        "SELECT ts, message, data FROM events WHERE category='sleep' AND code='onset_confirmed' "
        "AND ts >= ? AND ts <= ? ORDER BY ts ASC",
        (first_ts, last_ts),
    ).fetchall()
    if not onset_events:
        print("  no onset_confirmed event this night -- either onset never confirmed, or this")
        print("  box predates the _maybe_log_onset fix (upgrade and check again tomorrow).")
    for r in onset_events:
        import json as _json
        try:
            d = _json.loads(r["data"]) if r["data"] else {}
        except Exception:
            d = {}
        signals = d.get("signals") or []
        print(f"  {r['ts']}  confidence={d.get('confidence')}  latency_min={d.get('latency_min')}")
        print(f"    signals: {signals}")
        print(f"    accelerometer (stillness) contributed: {'stillness' in signals}")

    # ---------------------------------------------------------------- 3. staging + wake detection
    print("\n[3] STAGING + WAKE DETECTION")
    stage_counts = Counter(r["stage"] or "None" for r in rows)
    print(f"  stage label distribution: {dict(stage_counts)}")
    # condensed transition timeline: print only when the stage actually CHANGES
    prev = None
    transitions = []
    for r in rows:
        if r["stage"] != prev:
            transitions.append((r["ts"], r["stage"], r["stage_confidence"]))
            prev = r["stage"]
    print(f"  {len(transitions)} stage transitions:")
    for ts, stage, conf in transitions:
        conf_txt = f"{conf:.2f}" if conf is not None else "?"
        print(f"    {ts}  -> {stage:<10} (confidence {conf_txt})")

    wake_rows = [r for r in rows if r["wake_event"]]
    print(f"\n  {len(wake_rows)} wake_event flags during the night:")
    for r in wake_rows:
        print(f"    {r['ts']}  stage={r['stage']}")

    wl = conn.execute(
        "SELECT * FROM wake_log WHERE date = ?",
        (args.date,),
    ).fetchone()
    if wl is None:
        # wake_log is often keyed to the MORNING date, not the night_date -- try that too.
        wl = conn.execute(
            "SELECT * FROM wake_log ORDER BY date DESC LIMIT 1",
        ).fetchone()
        if wl:
            print(f"\n  wake_log (most recent row, date={wl['date']} -- night_date lookup missed):")
    else:
        print(f"\n  wake_log for {args.date}:")
    if wl:
        print(f"    woke_from_stage={wl['woke_from_stage']}  minutes_early={wl['minutes_early']}  "
              f"window_min={wl['window_min']}  forced={wl['forced']}  p_wake={wl['p_wake']}  "
              f"wake_thermal_f={wl['wake_thermal_f']}")
    else:
        print("  no wake_log row found")

    # ---------------------------------------------------------------- 4. steering
    print("\n[4] THERMAL STEERING")
    iv = conn.execute(
        "SELECT ts, controller_state, action, magnitude_f, reason, held, reverted "
        "FROM interventions WHERE night_date = ? ORDER BY id ASC",
        (args.date,),
    ).fetchall()
    print(f"  {len(iv)} interventions logged")
    by_action = Counter(r["action"] for r in iv)
    print(f"  by action: {dict(by_action)}")
    by_state = Counter(r["controller_state"] for r in iv)
    print(f"  by controller_state: {dict(by_state)}")
    print(f"  first 20 (of {len(iv)}):")
    for r in iv[:20]:
        print(f"    {r['ts']}  state={r['controller_state']:<14} action={r['action']:<14} "
              f"mag={r['magnitude_f']}  reason={r['reason']}")

    repo.close()


if __name__ == "__main__":
    main()
