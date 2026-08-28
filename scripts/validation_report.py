"""Run every validation layer this project has built, from the PUBLISHED night record.

    python scripts/validation_report.py night-2026-08-27.json [more.json ...]
        [--powerlog CurrentPowerlog.PLSQL]   # iOS screen-ons as objective wake anchors
        [--anchors anchors.json]             # any other external known-awake timestamps

Three modules existed for this and were reachable from nothing:

  * ``eval/controller_sanity.py`` -- internal consistency of a night (are the stage bouts
    physiological, do the interventions make sense against them),
  * ``eval/wake_anchors.py``     -- recall-free objective wake anchors, the layer that judges
    wake detection without asking the user to remember anything,
  * ``eval/trial_analysis.py``   -- reads out the randomized controller trial.

Dead validation code is worse than none: it looks like the question has been answered. The
trial one matters most right now -- an armed trial whose results nobody can read is a
write-only experiment.

Read-only. Runs anywhere the repo is checked out, which is the point: the box is not always
reachable and this project is meant to be debuggable from a phone.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleepctl.eval.controller_sanity import (compute_controller_sanity,  # noqa: E402
                                             format_report as sanity_report)
from sleepctl.eval.wake_anchors import (evaluate_wake_anchors,  # noqa: E402
                                        format_report as anchor_report)


def _rows(night: dict) -> list:
    """Night JSON samples in the shape the eval modules expect."""
    out = []
    for s in night.get("raw_samples") or []:
        if str(s.get("controller_state")) in ("idle", "None"):
            continue
        out.append(dict(s))
    return out


def _external_anchors(path: Optional[str]) -> list:
    """Objective known-awake instants from an EXTERNAL source.

    ``wake_anchors`` is a validation layer, and validation requires evidence the system did not
    produce itself: a phone unlock, a sent message, a logged bed exit. Feeding it our own
    ``wake_event`` flags -- which is what a first pass of this script did -- turns it into a
    measure of internal agreement between the wake voter and the stage label and reports the
    result as if it were ground truth. That is worse than not running it.

    Accepts a JSON file of ISO timestamps, or nothing.
    """
    if not path:
        return []
    try:
        data = json.loads(Path(path).read_text())
    except Exception:
        return []
    if isinstance(data, dict):
        data = data.get("anchors") or data.get("timestamps") or []
    return [str(x) for x in data]


def _powerlog_anchors(powerlog_path: Optional[str], night: dict) -> tuple:
    """Screen-on anchors from an iOS PowerLog, bounded to this night."""
    if not powerlog_path:
        return [], {}
    try:
        from sleepctl.eval.ios_powerlog import screen_on_events
        cap = night.get("sensor_capture") or {}
        lo = hi = None
        if cap.get("first_ts") and cap.get("last_ts"):
            from datetime import datetime as _dt, timezone as _tz
            lo = _dt.fromisoformat(str(cap["first_ts"])).astimezone(_tz.utc)
            hi = _dt.fromisoformat(str(cap["last_ts"])).astimezone(_tz.utc)
        return screen_on_events(powerlog_path, lo=lo, hi=hi)
    except Exception as exc:
        return [], {"error": repr(exc)}


def _internal_wake_flags(night: dict) -> list:
    """Our OWN wake_event timestamps -- for the internal-consistency check, never for validation."""
    return [s["ts"] for s in night.get("raw_samples") or [] if s.get("wake_event")]


def main() -> int:
    argv = sys.argv[1:]
    anchor_path = powerlog_path = None
    for flag in ("--anchors", "--powerlog"):
        if flag in argv:
            i = argv.index(flag)
            val = argv[i + 1] if i + 1 < len(argv) else None
            argv = argv[:i] + argv[i + 2:]
            if flag == "--anchors":
                anchor_path = val
            else:
                powerlog_path = val
    paths = argv
    if not paths:
        print(__doc__)
        return 2
    for p in paths:
        night = json.loads(Path(p).read_text())
        label = str(night.get("night_date"))
        rows = _rows(night)
        print("=" * 88)
        print(f"VALIDATION REPORT  {label}   ({len(rows)} in-session samples)")
        print("=" * 88)
        if not rows:
            print("  no in-session samples -- nothing to validate")
            continue

        print("\n[1] CONTROLLER SANITY (internal consistency)")
        try:
            sanity = compute_controller_sanity(rows, night.get("interventions"))
            print(sanity_report(sanity, label=label))
        except Exception as exc:
            print(f"  could not compute: {exc!r}")

        print("\n[2] RECALL-FREE WAKE ANCHORS (independent evidence)")
        # GAIT anchors ride in the night export. Rhythmic locomotion is the one motion signature
        # no wake detector reads -- every existing one measures amplitude -- so it is genuinely
        # independent, and sustained gait is near-certain evidence of being up.
        markers = list(night.get("marker_anchors") or [])
        gaits = list(night.get("gait_anchors") or [])
        # iOS screen-ons: RETROSPECTIVE and observer-effect-free. The user was checking the
        # lock-screen clock on nights already recorded, unaware anything was being marked --
        # which is a stronger position than any prospective instrument can occupy.
        phone, plog_report = _powerlog_anchors(powerlog_path, night)
        anchors = markers + gaits + phone + _external_anchors(anchor_path)
        if anchors:
            print(f"  {len(markers)} DECLARED marker gesture(s) -- the strongest anchor: the user "
                  f"said so")
            print(f"  {len(gaits)} gait anchor(s) -- inferred, but from a signal no detector reads")
            print(f"  {len(phone)} iPhone screen-on(s) from the PowerLog -- retrospective, and "
                  f"the user was not aware of being measured")
            print(f"  {len(anchors) - len(markers) - len(gaits) - len(phone)} from an external file")
            if markers:
                print("  marker times: " + ", ".join(str(m)[11:19] for m in markers[:12]))
        if not anchors:
            print("  no external anchor file supplied -- skipping.")
            print("  This layer needs objective known-awake instants the system did NOT produce")
            print("  (phone unlocks, sent messages, logged bed exits). Pass --anchors FILE.json.")
        else:
            try:
                print(anchor_report(evaluate_wake_anchors(rows, anchors), label=label))
            except Exception as exc:
                print(f"  could not compute: {exc!r}")

        print("\n[2b] INTERNAL CONSISTENCY (our wake flags vs our own stage labels)")
        print("  NOT validation -- both sides come from this system. It says whether the wake")
        print("  voter and the stager agree with each other, nothing about whether either is right.")
        try:
            res = evaluate_wake_anchors(rows, _internal_wake_flags(night))
            print(anchor_report(res, label=label))
        except Exception as exc:
            print(f"  could not compute: {exc!r}")

        print("\n[3] SLEEP/WAKE DETECTOR (shadow mode)")
        sw = night.get("sleep_wake_shadow")
        if not sw:
            err = night.get("sleep_wake_shadow_error")
            print(f"  not available{f' -- {err}' if err else ''}")
        else:
            print(f"  epochs {sw['n_epochs']}, comparable {sw['n_comparable']}, "
                  f"agreement with live staging {sw.get('agreement_with_live_staging')}")
            print(f"  detector saw wake where the live path did not: "
                  f"{sw['detector_wake_live_sleep']}")
            print(f"  live path saw wake where the detector did not: "
                  f"{sw['live_wake_detector_sleep']}")
            print(f"  autonomic channel enabled: {sw.get('autonomic_enabled')}")

        hv = night.get("hrv_windows_summary")
        if hv:
            print(f"\n[4] HRV FEATURE WINDOWS: {hv.get('n_windows')} windows from "
                  f"{hv.get('n_intervals')} intervals")
        elif night.get("hrv_windows_error"):
            print(f"\n[4] HRV FEATURE WINDOWS: FAILED -- {night['hrv_windows_error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
