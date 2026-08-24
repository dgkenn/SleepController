"""One-off night audit: pull the thermal trace + the daemon's own device-status snapshots
around a flagged incident window, straight from sleepctl.db. Paste the output back for
root-cause analysis -- this only reads, never modifies anything.

Usage:
    .venv\\Scripts\\python.exe night_audit.py --db <path to sleepctl.db> ^
        --start "2026-08-23 20:50" --end "2026-08-24 07:30"
"""
import argparse
import json
import sqlite3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--start", required=True, help='e.g. "2026-08-23 20:50"')
    ap.add_argument("--end", required=True, help='e.g. "2026-08-24 07:30"')
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    print("=" * 100)
    print("THERMAL_SAMPLES (the raw device-level trace, one row per active tick)")
    print("=" * 100)
    rows = conn.execute(
        "SELECT ts, device_level, target_level, delta_level, direction, room_temp_f, state, "
        "session_mode FROM thermal_samples WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (args.start, args.end),
    ).fetchall()
    print(f"{len(rows)} rows")
    for r in rows:
        print(f"  {r['ts']}  level={r['device_level']:>4} target={r['target_level']:>4} "
              f"delta={r['delta_level']:>4} dir={r['direction']:<8} room={r['room_temp_f']} "
              f"state={r['state']} mode={r['session_mode']}")

    print()
    print("=" * 100)
    print("STATE_HISTORY -> extra.device (the daemon's own device_status() snapshot per tick --")
    print("this is EXACTLY what the thermal_response check reads, including the Eight Sleep")
    print("app's own schedule/Autopilot state via external_schedule)")
    print("=" * 100)
    rows = conn.execute(
        "SELECT ts, state, target_level, extra FROM state_history "
        "WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (args.start, args.end),
    ).fetchall()
    print(f"{len(rows)} rows")
    schedule_flags = []
    for r in rows:
        extra = {}
        try:
            extra = json.loads(r["extra"]) if r["extra"] else {}
        except Exception:
            pass
        d = extra.get("device") or {}
        sched = d.get("external_schedule") or {}
        line = (
            f"  {r['ts']}  ctrl_state={r['state']:<12} cmd_target_level={r['target_level']} "
            f"| device_level={d.get('device_level')} device_target={d.get('device_target_level')} "
            f"heating={d.get('now_heating')} cooling={d.get('now_cooling')} "
            f"online={d.get('online')} priming={d.get('priming')} water={d.get('has_water')} "
            f"| schedule: active={sched.get('active')} activity={sched.get('activity')} "
            f"schedule_target={sched.get('target_level')}"
        )
        print(line)
        # Flag any tick where the app's own schedule is ACTIVE and asking for a level that
        # disagrees with what we commanded -- the concrete, checkable version of "does an
        # Autopilot/schedule hold a different setpoint" instead of assuming either way.
        if sched.get("active") and sched.get("target_level") is not None and r["target_level"] is not None:
            if abs(sched.get("target_level") - r["target_level"]) > 3:
                schedule_flags.append(line)

    print()
    print("=" * 100)
    if schedule_flags:
        print(f"SCHEDULE CONFLICT: {len(schedule_flags)} tick(s) where the Eight Sleep app's own "
              f"schedule was ACTIVE and disagreed with our commanded target by >3 levels:")
        for line in schedule_flags[:10]:
            print(line)
    else:
        print("No schedule/Autopilot conflict found -- external_schedule was either inactive or "
              "agreed with our command on every tick in this window.")
    print("=" * 100)

    conn.close()


if __name__ == "__main__":
    main()
