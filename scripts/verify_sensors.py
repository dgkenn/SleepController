#!/usr/bin/env python3
"""Per-channel sensor verification: which inputs actually work, and which are merely quiet.

Every sensor on this system is OPTIONAL by design — the controller degrades rather than stops —
which is exactly why "is it working?" is hard to answer by looking. A channel that is silent looks
identical to a channel that is broken, and both look identical to a channel that was never set up.

This separates the three questions that get conflated:

  PATH   — does the ingest/decode/fusion code work? Exercised here with synthetic data, so it is
           answerable on any machine, with no hardware and no network.
  LIVE   — is real data arriving right now? Read from the database; only meaningful on the box.
  ROLE   — what does the controller lose without it?

Run it anywhere for the PATH column:

    python scripts/verify_sensors.py --paths-only

Run it on the controller box for the full picture:

    python scripts/verify_sensors.py --db C:\\path\\to\\sleepctl.db

Read-only and side-effect-free with respect to the Pod: it never sends a device command. The path
checks write to a THROWAWAY temp database, never the one you pass in.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard" / "api"))

OK, DEAD, QUIET, SKIP = "OK", "BROKEN", "QUIET", "SKIPPED"


class Result:
    def __init__(self, name, role):
        self.name, self.role = name, role
        self.path = SKIP
        self.path_note = ""
        self.live = SKIP
        self.live_note = ""

    def path_ok(self, note=""):
        self.path, self.path_note = OK, note

    def path_dead(self, note):
        self.path, self.path_note = DEAD, note


# ----------------------------------------------------------------- PATH checks (no hardware)
def _tmp_repo():
    """A throwaway DB with BOTH schemas applied — never the caller's database."""
    from sleepctl.storage.repository import Repository
    from app import db as app_db

    path = os.path.join(tempfile.mkdtemp(prefix="sleepctl-verify-"), "verify.db")
    repo = Repository(path, check_same_thread=False)
    repo.conn.executescript(app_db._DASHBOARD_DDL)
    app_db._apply_migrations(repo.conn)
    repo.conn.commit()
    return repo


def check_verity_cardiac(r):
    """HR + beat-to-beat RR -> HRV, the authoritative cardiac channel."""
    from app import bridge, services

    repo = _tmp_repo()
    try:
        out = services.ingest_hr(repo, {"hr": 57.0, "rr": [1030.0, 1055.0, 1012.0],
                                        "source": "verity"})
        if not out.get("ok"):
            return r.path_dead(f"ingest refused a valid batch: {out}")
        if out.get("hrv") is None:
            return r.path_dead("RR intervals did not produce an HRV (RMSSD)")
        fused = bridge.read_fused_sensor(repo.conn)
        if not fused or fused.get("hr") != 57.0:
            return r.path_dead(f"HR did not reach fusion: {fused}")
        if fused.get("hr_source") != "verity":
            return r.path_dead(f"wrong source attribution: {fused.get('hr_source')}")
        r.path_ok(f"HR 57.0 -> fusion, HRV {out['hrv']:.1f} ms from 3 RR intervals")
    finally:
        repo.close()


def check_verity_accelerometer(r):
    """The armband's OWN accelerometer: actigraphy without the phone."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import polar_pmd as pmd
    from app import bridge, services

    repo = _tmp_repo()
    try:
        mags = [1.0 + 0.02 * ((i % 7) - 3) for i in range(260)]
        counts = pmd.actigraphy_counts(mags)
        counts["fs"] = 52
        out = services.ingest_hr(repo, {"hr": 56.0, "acc": counts, "source": "verity"})
        if not out.get("ok"):
            return r.path_dead(f"ingest refused an ACC batch: {out}")
        fused = bridge.read_fused_sensor(repo.conn)
        mv = (fused or {}).get("movement")
        if mv is None:
            return r.path_dead("accelerometer counts did not become a movement index")
        if (fused or {}).get("movement_source") != "verity":
            return r.path_dead(f"wrong movement source: {(fused or {}).get('movement_source')}")
        r.path_ok(f"PIM {counts['pim']:.2f} -> movement index {mv} (no phone needed)")
    finally:
        repo.close()


def check_phone_bcg(r):
    """iPhone accelerometer -> sub-second movement (+ best-effort BCG heart rate)."""
    from app import bridge, services

    repo = _tmp_repo()
    try:
        fs = 50
        n = fs * 20
        mag = [1.0 + 0.03 * math.sin(2 * math.pi * 1.0 * (i / fs)) for i in range(n)]
        out = services.ingest_bcg(repo, {"fs": fs, "mag": mag})
        if not out.get("ok"):
            return r.path_dead(f"ingest refused a valid accel batch: {out}")
        sample = bridge.read_sensor_sample(repo.conn)
        if not sample or sample.get("movement") is None:
            return r.path_dead(f"movement did not reach the bridge: {sample}")
        r.path_ok(f"{n} accel samples @ {fs}Hz -> movement {sample['movement']}")
    finally:
        repo.close()


def check_pod_frame_decode(r):
    """The Pod cloud frame decode: stage/HR/HRV/RR/presence/bed-temp mapping."""
    from types import SimpleNamespace

    from sleepctl.adapters.eightsleep_cloud import map_stage
    from sleepctl.models import SleepStage

    pairs = [("awake", SleepStage.AWAKE), ("light", SleepStage.LIGHT),
             ("deep", SleepStage.DEEP), ("rem", SleepStage.REM)]
    for raw, expected in pairs:
        got = map_stage(raw)
        if got is not expected:
            return r.path_dead(f"stage {raw!r} mapped to {got}, expected {expected}")
    if map_stage(None) is not SleepStage.UNKNOWN:
        return r.path_dead("a missing stage must map to UNKNOWN, not a real stage")
    if map_stage("something-new") is not SleepStage.UNKNOWN:
        return r.path_dead("an unrecognised stage must map to UNKNOWN")
    r.path_ok("stage mapping + unknown-safe fallback verified (cloud decode)")


def check_thermal_feedback(r):
    """The bed's arrival measurement — closed-loop feedback.

    NOT a thermometer on this Pod. Sensed cover temperature (``bed_temp_f`` / ``tempBedC``) comes
    down the membership-gated trends pipeline and is absent without an Autopilot subscription, so
    the signal that actually runs here is the Hub's water-side ``device_level``. Both decoders are
    exercised: the temperature path in case a membership ever appears, and the level path because
    it is the one carrying tonight."""
    from sleepctl.learning.prevention_timing import (
        measure_arrival_min, measure_level_arrival_min)

    start = datetime(2026, 7, 30, 2, 0)
    trace = []
    for i in range(-5, 30):
        t = start + timedelta(minutes=i)
        trace.append({"ts": t, "bed_temp_f": 72.0 if i < 8 else 72.0 - 0.3 * (i - 8),
                      "wake_event": 0})
    arrival = measure_arrival_min(trace, start)
    if arrival is None:
        return r.path_dead("a clear cooling ramp was not detected as thermal arrival")
    flat = [{"ts": start + timedelta(minutes=i), "bed_temp_f": 72.0, "wake_event": 0}
            for i in range(-5, 30)]
    if measure_arrival_min(flat, start) is not None:
        return r.path_dead("a FLAT trace was reported as arriving — false thermal response")

    # The membership-free path, which is what this box actually uses.
    levels = [{"ts": start + timedelta(minutes=i),
               "device_level": -10 if i < 8 else -10 - 1.5 * (i - 8)} for i in range(-5, 30)]
    lvl_arrival = measure_level_arrival_min(levels, start)
    if lvl_arrival is None:
        return r.path_dead("a clear device-level cooling ramp was not detected as arrival")
    flat_levels = [{"ts": start + timedelta(minutes=i), "device_level": -10} for i in range(-5, 30)]
    if measure_level_arrival_min(flat_levels, start) is not None:
        return r.path_dead("a FLAT device-level trace was reported as arriving")

    r.path_ok(f"cooling ramp detected at {arrival} min (temp) / {lvl_arrival} min (level); "
              f"flat traces correctly report none")


def check_weather(r):
    """Ambient forecast -> feed-forward pre-compensation of the setpoint."""
    from sleepctl.config import AppConfig
    from sleepctl.precompensation import compute_precompensation

    cfg = AppConfig.default()

    def _bias(temp_f):
        forecast = {"hours": [{"temp_f": temp_f} for _ in range(12)], "trend": "steady"}
        return compute_precompensation(forecast, cfg)["bias_f"]

    # Thresholds are 62F (above -> cool bias) and 40F (below -> warm bias); BETWEEN them the
    # bias is deliberately zero, so the probes have to sit outside that band to mean anything.
    hot, cold, mild = _bias(90.0), _bias(30.0), _bias(50.0)
    if not hot < 0:
        return r.path_dead(f"a hot night must bias the bed COOLER, got {hot}")
    if not cold > 0:
        return r.path_dead(f"a cold night must bias the bed WARMER, got {cold}")
    if mild != 0.0:
        return r.path_dead(f"a mild night is inside the no-bias band; expected 0.0, got {mild}")
    if abs(hot) > cfg.tunables.precomp_max_bias_f + 1e-9:
        return r.path_dead(f"bias {hot} exceeds the {cfg.tunables.precomp_max_bias_f}F cap")
    none_bias = compute_precompensation(None, cfg)["bias_f"]
    if none_bias != 0.0:
        return r.path_dead(f"no forecast must mean no bias, got {none_bias}")
    empty = compute_precompensation({"hours": []}, cfg)["bias_f"]
    if empty != 0.0:
        return r.path_dead(f"an empty forecast must mean no bias, got {empty}")
    r.path_ok(f"90F -> {hot:+.2f}F, 30F -> {cold:+.2f}F, 50F -> {mild:+.2f}F (no-bias band); "
              f"no/empty forecast -> 0.00F; capped at {cfg.tunables.precomp_max_bias_f}F")


def check_calendar(r):
    """ICS feed -> the wake deadline the whole night is planned around."""
    from sleepctl.adapters.calendar import parse_ics, upcoming_events

    ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Day shift\r\n"
           "DTSTART:20260801T070000Z\r\nDTEND:20260801T190000Z\r\n"
           "END:VEVENT\r\nEND:VCALENDAR\r\n")
    events = parse_ics(ics)
    if not events:
        return r.path_dead("a valid ICS document produced no events")
    up = upcoming_events(events, now=datetime(2026, 7, 31, 20, 0))
    if not up:
        return r.path_dead("a future event was not returned as upcoming")
    if parse_ics("not an ics at all") != []:
        return r.path_dead("garbage input must produce no events, not junk ones")
    r.path_ok(f"parsed {len(events)} event(s); garbage input rejected cleanly")


def check_hue_dawn(r):
    """The sunrise ramp: bulb levels through the dawn window + the therapy lamp at wake."""
    from sleepctl.controller.wake_orchestrator import WakeConfig, WakeOrchestrator
    from sleepctl.models import SensorFrame, SleepStage

    deadline = datetime(2026, 8, 1, 7, 0)
    orch = WakeOrchestrator(WakeConfig(window_min=30, light_enabled=True))
    levels = []
    for i in range(40, -1, -2):
        t = deadline - timedelta(minutes=i)
        f = SensorFrame(timestamp=t, stage=SleepStage.DEEP, presence=True, heart_rate=50.0,
                        hrv=55.0, respiratory_rate=14.0, movement=0.01, bed_temp_f=72.0,
                        room_temp_f=68.0, data_age_seconds=5)
        levels.append(orch.evaluate(t, f, [], deadline).light_level)
    rising = [x for x in levels if x > 0]
    if not rising:
        return r.path_dead("the dawn ramp never produced a light level")
    if rising != sorted(rising):
        return r.path_dead(f"the sunrise must only brighten, got {rising}")
    if abs(rising[-1] - 1.0) > 1e-6:
        return r.path_dead(f"the ramp must reach full brightness, peaked at {rising[-1]}")
    r.path_ok(f"ramps {rising[0]:.2f} -> {rising[-1]:.2f} over the dawn window "
              f"({len(rising)} steps)")


def check_stager(r):
    """HR (+ optional motion) -> a sleep stage, the signal the controller steers on."""
    from sleepctl.ml.sleep_staging.infer import SleepStager

    stager = SleepStager.load()
    if not stager.available:
        return r.path_dead("no usable model weights bundled — the learned stager is unavailable")
    base = datetime(2026, 7, 30, 23, 0).timestamp()
    samples = [(base + i * 2.0, 52.0 + 3.0 * math.sin(i / 40.0)) for i in range(900)]
    est = stager.predict(samples, minutes_since_start=30)
    if est is None:
        return r.path_dead("30 minutes of dense HR produced no estimate")
    if est.stage_label not in ("wake", "light", "deep", "rem"):
        return r.path_dead(f"nonsense stage label {est.stage_label!r}")
    r.path_ok(f"{len(samples)} HR samples -> '{est.stage_label}' "
              f"(confidence {est.confidence:.2f}, smoothed={est.smoothed})")


# ----------------------------------------------------------------- LIVE checks (needs the box)
def live_state(results, db_path):
    """Fill in the LIVE column from the real database."""
    from sleepctl.storage.repository import Repository
    from app import bridge

    repo = Repository(db_path, check_same_thread=False)
    try:
        def age_of(sample):
            return None if not sample else sample.get("age_seconds")

        card = bridge.read_cardiac_sample(repo.conn)
        phone = bridge.read_sensor_sample(repo.conn)
        _fill(results, "Verity cardiac (HR/HRV)", age_of(card))
        _fill(results, "Verity accelerometer", _actigraphy_age(repo))
        _fill(results, "iPhone accelerometer", age_of(phone))

        rt = bridge.read_runtime_state(repo.conn, 180)
        extra = rt.get("extra") or {}
        _set(results, "Eight Sleep Pod frame",
             OK if not rt.get("stale") else QUIET,
             f"runtime_state {'fresh' if not rt.get('stale') else 'STALE'}; "
             f"device online={extra.get('device', {}).get('online')}")
        th = extra.get("thermal_health") or {}
        _set(results, "Bed thermal feedback (water-side level)",
             OK if th.get("responding") else QUIET,
             f"state={th.get('state')} responding={th.get('responding')} "
             f"({th.get('reason')})")

        # Weather: the daemon publishes its computed bias onto runtime_state each refresh.
        pc = extra.get("precompensation") or {}
        if pc.get("trend") is not None:
            _set(results, "Weather / ambient", OK,
                 f"forecast applied: bias {pc.get('bias_f')}F ({pc.get('reason')})")
        else:
            _set(results, "Weather / ambient", QUIET,
                 "no overnight forecast applied (no location configured, or fetch failing)")

        # Calendar: this one is CONFIGURED-or-not rather than streaming-or-not.
        try:
            row = repo.conn.execute(
                "SELECT value FROM settings_kv WHERE key='calendar_config'").fetchone()
        except Exception:
            row = None
        configured = bool(row and row["value"] and row["value"] not in ("null", "{}"))
        _set(results, "Work calendar (ICS)",
             OK if configured else QUIET,
             "feed connected" if configured
             else "no feed connected — shift-aware wake planning is inactive")

        # Hue: an OUTPUT, and configured-or-not rather than streaming-or-not. Without target
        # bulb ids the orchestrator never computes a light level at all (set_dawn_light).
        try:
            hrow = repo.conn.execute(
                "SELECT value FROM settings_kv WHERE key='hue_config'").fetchone()
            import json as _json
            hc = _json.loads(hrow["value"]) if hrow and hrow["value"] else {}
        except Exception:
            hc = {}
        ready = bool(hc.get("enabled") and hc.get("bridge_ip") and hc.get("token")
                     and (hc.get("target_ids") or hc.get("target_id")))
        _set(results, "Hue dawn light (output)",
             OK if ready else QUIET,
             "bridge + target bulbs configured; the sunrise ramp is active" if ready
             else "not configured — the wake still works, silently, with no light")

        # The stager is DERIVED, not a sensor: its liveness is "are usable weights loaded".
        try:
            from sleepctl.ml.sleep_staging.infer import SleepStager
            _set(results, "Sleep stager (derived)",
                 OK if SleepStager.load().available else QUIET,
                 "model weights loaded" if SleepStager.load().available
                 else "no usable weights — falls back to the heuristic estimator")
        except Exception as exc:
            _set(results, "Sleep stager (derived)", QUIET, f"not loadable: {exc!r}")
    finally:
        repo.close()


def _actigraphy_age(repo):
    try:
        row = repo.conn.execute(
            "SELECT ts FROM actigraphy WHERE pim IS NOT NULL ORDER BY ts DESC LIMIT 1").fetchone()
    except Exception:
        return None
    if not row:
        return None
    try:
        return (datetime.now(timezone.utc)
                - datetime.fromisoformat(row["ts"])).total_seconds()
    except Exception:
        return None


def _fill(results, name, age):
    if age is None:
        _set(results, name, QUIET, "never received any data")
    elif age < 120:
        _set(results, name, OK, f"streaming (last sample {int(age)}s ago)")
    else:
        _set(results, name, QUIET, f"not streaming (last sample {int(age / 60)} min ago)")


def _set(results, name, state, note):
    for r in results:
        if r.name == name:
            r.live, r.live_note = state, note


# ----------------------------------------------------------------- driver
CHANNELS = [
    ("Verity cardiac (HR/HRV)", "onset, arousal, wake-risk, staging", check_verity_cardiac),
    ("Verity accelerometer", "motion WITHOUT the phone; onset + arousal", check_verity_accelerometer),
    ("iPhone accelerometer", "sub-second motion when the phone is in bed", check_phone_bcg),
    ("Eight Sleep Pod frame", "stage/presence when the membership is active", check_pod_frame_decode),
    ("Bed thermal feedback (water-side level)",
     "closed-loop control + arrival timing (NOT a thermometer -- see check_thermal_feedback)",
     check_thermal_feedback),
    ("Weather / ambient", "feed-forward setpoint pre-compensation", check_weather),
    ("Work calendar (ICS)", "the wake deadline the night is planned around", check_calendar),
    ("Sleep stager (derived)", "turns HR into the stage the controller steers on", check_stager),
    ("Hue dawn light (output)", "sunrise ramp before the alarm + therapy lamp at wake", check_hue_dawn),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", help="live database to read current sensor freshness from")
    ap.add_argument("--paths-only", action="store_true",
                    help="skip the live column (no box / no database needed)")
    args = ap.parse_args(argv)

    results = []
    for name, role, fn in CHANNELS:
        r = Result(name, role)
        try:
            fn(r)
        except Exception as exc:  # a check that explodes IS a broken path
            r.path_dead(f"{type(exc).__name__}: {exc}")
        results.append(r)

    if args.db and not args.paths_only:
        try:
            live_state(results, args.db)
        except Exception as exc:
            print(f"  (live state unavailable: {exc!r})\n")

    width = max(len(r.name) for r in results)
    print()
    print("=" * (width + 46))
    print("  SENSOR CHANNELS".ljust(width + 4) + "PATH      LIVE")
    print("=" * (width + 46))
    broken = 0
    for r in results:
        if r.path == DEAD:
            broken += 1
        print(f"  {r.name.ljust(width)}  {r.path.ljust(8)}  {r.live}")
        print(f"  {' ' * width}  role: {r.role}")
        if r.path_note:
            print(f"  {' ' * width}  path: {r.path_note}")
        if r.live_note:
            print(f"  {' ' * width}  live: {r.live_note}")
        print()
    print("=" * (width + 46))
    if broken:
        print(f"  {broken} channel(s) have a BROKEN code path — fix before relying on them.")
    else:
        print("  Every sensor code path works.")
    if args.paths_only or not args.db:
        print("  LIVE not checked: pass --db <path> ON THE CONTROLLER BOX to see what is")
        print("  actually arriving. A working PATH does not mean data is flowing.")
    print("=" * (width + 46))
    print()
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
