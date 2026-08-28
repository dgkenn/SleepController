"""Complete audit of one night from its PUBLISHED record -- no database, no box access.

`night_audit.py` needs sleepctl.db on the machine that wrote it, which makes it useless from a
phone or from a session that only has the repo. This reads a `night-<date>.json` off the
`night-data` branch instead, so the whole audit runs anywhere the repo is checked out:

    git show origin/night-data:night-2026-08-27.json > n.json
    python scripts/night_report.py n.json

Reports, in the order the questions actually get asked: did the sensors work, did I fall
asleep, did the controller notice, did it protect me, and what temperature was it holding.
Read-only.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _f(level):
    """Device level -> water °F. The Eight Sleep scale is NOT linear and level 0 is 81 °F, so
    raw levels read as alarmingly cold when they are ordinary: -74 is 65 °F. Every temperature
    in this report is converted, because reading levels as degrees produced a whole night of
    wrong conclusions on 2026-08-27."""
    if level is None:
        return None
    try:
        from sleepctl.controller.calibration import level_to_fahrenheit
        return level_to_fahrenheit(level)
    except Exception:
        return None


def _hhmmss(ts):
    return str(ts)[11:19] if ts else "-"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    d = json.loads(Path(sys.argv[1]).read_text())
    rs = d.get("raw_samples") or []
    night = d.get("night_date", "?")
    print("=" * 88)
    print(f"NIGHT REPORT  {night}   ({len(rs)} samples)")
    print("=" * 88)

    # ---------------------------------------------------------------- 1. sensors
    print("\n[1] SENSORS")
    cap = d.get("sensor_capture") or {}
    n = max(1, len(rs))
    for label, key in (("heart rate", "heart_rate"), ("HRV (PPI)", "hrv"),
                       ("movement (ACC)", "movement"), ("respiration", "respiratory_rate"),
                       ("bed temperature", "bed_temp_f"), ("Pod presence", "presence")):
        have = sum(1 for s in rs if s.get(key) is not None)
        flag = ""
        if key == "bed_temp_f" and have == 0:
            flag = "   <-- thermal loop OPEN all night"
        if key == "movement" and have == 0:
            flag = "   <-- no actigraphy: the best wake signal (6/6 vs 2/6) was absent"
        print(f"  {label:18} {have:5d}/{len(rs)} samples ({100*have/n:5.1f}%){flag}")
    if cap:
        print(f"  window: {_hhmmss(cap.get('first_ts'))} -> {_hhmmss(cap.get('last_ts'))}")

    # ---------------------------------------------------------------- 2. falling asleep
    print("\n[2] FALLING ASLEEP")
    ev = d.get("onset_events") or []
    if not ev:
        print("  onset NEVER confirmed -- the controller cannot leave INDUCTION, and awakening")
        print("  prevention and steering both live in MAINTENANCE, so neither could arm.")
    for e in ev:
        print(f"  CONFIRMED {_hhmmss(e.get('ts'))}  confidence={e.get('confidence')} "
              f"latency={e.get('latency_min')}")
        print(f"    signals: {', '.join(e.get('signals') or []) or '-'}")
        print(f"    accelerometer contributed: {e.get('accelerometer_contributed')}")

    # ---------------------------------------------------------------- 3. states / awakenings
    print("\n[3] STATES AND AWAKENINGS")
    states = Counter(str(s.get("controller_state")) for s in rs)
    print(f"  {dict(states)}")
    if "maintenance" not in states:
        print("  NEVER REACHED MAINTENANCE -- no wake protection ran at all this night.")
    w = [s for s in rs if s.get("wake_event")]
    print(f"  awakening ticks: {len(w)}")
    if w:
        print(f"    first {_hhmmss(w[0]['ts'])}   last {_hhmmss(w[-1]['ts'])}")
        temps = [t for t in (_f(s.get("commanded_level")) for s in w) if t is not None]
        if temps:
            print(f"    water at those moments: {min(temps):.1f}-{max(temps):.1f} F")

    # ---------------------------------------------------------------- 4. prevention
    print("\n[4] AWAKENING PRE-EMPTION")
    ps = d.get("preemption_summary") or {}
    pe = d.get("preemption_events") or []
    if not ps:
        print("  no pre-emption record in this night (predates the telemetry)")
    else:
        nm = ps.get("n_in_maintenance") or 0
        npre = ps.get("n_preempting") or 0
        pct = f"{100*npre/nm:.1f}%" if nm else "n/a"
        print(f"  engaged on {npre}/{nm} maintenance ticks ({pct})")
        print(f"  actions: {ps.get('by_action')}")
        print(f"  precursors: {ps.get('precursor_reasons')}")
        print(f"  risk reasons: {ps.get('risk_reasons')}")
        if pe:
            moved = [e for e in pe if e.get("action") in ("warmer", "cooler")]
            print(f"  of {len(pe)} pre-empting ticks, {len(moved)} MOVED the bed and "
                  f"{len(pe)-len(moved)} held that setting")
            t = [x for x in (_f(e.get("target_level")) for e in pe) if x is not None]
            if t:
                print(f"  water across those ticks: {min(t):.1f}-{max(t):.1f} F")

    # ---------------------------------------------------------------- 5. temperature
    print("\n[5] TEMPERATURE")
    temps = [t for t in (_f(s.get("commanded_level")) for s in rs
                         if str(s.get("controller_state")) in ("maintenance", "wake_recovery"))
             if t is not None]
    if not temps:
        print("  no in-maintenance commands to report")
    else:
        c = Counter(round(t) for t in temps)
        print(f"  in-maintenance water: {min(temps):.1f}-{max(temps):.1f} F "
              f"(swing {max(temps)-min(temps):.1f} F)")
        for k in sorted(c):
            bar = "#" * max(1, int(40 * c[k] / max(c.values())))
            print(f"    {k:3d} F  {c[k]:5d}  {bar}")
        prof = d.get("comfort_profile") or {}
        lo, hi = prof.get("cool_edge_f"), prof.get("warm_edge_f")
        if lo is not None and hi is not None:
            band_lo, band_hi = float(lo) - 0.5, float(hi) + 0.5
            pinned = sum(1 for t in temps if t <= band_lo + 0.5)
            print(f"  comfort band {band_lo:.1f}-{band_hi:.1f} F "
                  f"(cool_edge {lo} / warm_edge {hi}, +-0.5 F margin)")
            if pinned / len(temps) >= 0.5:
                print(f"    PINNED at the cold floor on {pinned}/{len(temps)} ticks "
                      f"({100*pinned/len(temps):.0f}%) -- the band, not the controller, is "
                      f"deciding this night")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
