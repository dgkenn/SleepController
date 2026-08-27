"""Full-detail night-data publisher for the always-on Windows control machine.

Mirrors ``health_snapshot.py`` / ``publish-health.ps1`` exactly, except this one is NOT
scrubbed: the user explicitly decided their physiology data being public is fine, and that
minimizing how often they have to touch the laptop matters more than keeping it private. So
this publishes full per-night detail -- raw sensor samples, reconstructed sleep architecture,
Perfect Sleep Index, sleep-onset signals (including whether the accelerometer/stillness signal
contributed), staging transitions, wake detection, and thermal interventions -- to a public
``night-data`` branch of the same repo, in the clear, no encryption. An off-box Claude session
reads it with a plain ``git fetch origin night-data`` -- no key, no script run by hand, no
laptop interaction beyond what the watchdog already does automatically.

``scripts/publish-night-data.ps1`` calls this on the same cadence as publish-health.ps1.

Everything here is defensive in the same spirit as health_snapshot.py: a bad db path, an
import hiccup, an empty night -- none of it should crash the publish. On a hard failure
``write_export`` still writes a minimal error file before signalling failure via
``SystemExit(1)`` (the PS layer decides success by exit code).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

SCHEMA = "sleepctl.night_data/v1"


def _iso(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def _build_repo(db_path: str):
    """Open a sleepctl ``Repository`` (with the dashboard-only tables applied) over ``db_path``.

    wake_log and friends are dashboard tables, not base sleepctl schema -- mirrors
    health_snapshot.py's ``_build_repo`` exactly so this reads the same tables the API does.
    """
    from sleepctl.storage.repository import Repository
    from app import db as app_db

    repo = Repository(db_path, check_same_thread=False)
    repo.conn.row_factory = sqlite3.Row
    try:
        repo.conn.executescript(app_db._DASHBOARD_DDL)
        app_db._apply_migrations(repo.conn)
        repo.conn.commit()
    except Exception:
        pass
    return repo


def _recent_night_dates(conn, limit: int) -> list:
    rows = conn.execute(
        "SELECT DISTINCT night_date FROM raw_samples WHERE night_date IS NOT NULL "
        "ORDER BY night_date DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [r["night_date"] for r in rows]


def build_night_export(repo, night_date: str) -> dict:
    """Build the full-detail export for one night. Never raises -- degrades to partial data."""
    from sleepctl.loop.night_rollup import reconstruct_night_summary
    from sleepctl.benchmarks import perfect_sleep_index, NightMode

    conn = repo.conn
    out = {"schema": SCHEMA, "night_date": night_date, "generated_utc": _iso(None)}

    # ---- 1. raw samples (the full sensor time series) ------------------------------------
    try:
        rows = conn.execute(
            "SELECT ts, stage, stage_confidence, heart_rate, hrv, respiratory_rate, movement, "
            "presence, bed_temp_f, commanded_level, controller_state, wake_event "
            "FROM raw_samples WHERE night_date = ? ORDER BY id ASC",
            (night_date,),
        ).fetchall()
        out["raw_samples"] = [dict(r) for r in rows]
        n = len(rows)
        out["sensor_capture"] = {
            "n_samples": n,
            "heart_rate_present": sum(1 for r in rows if r["heart_rate"] is not None),
            "movement_present": sum(1 for r in rows if r["movement"] is not None),
            "usable_stage_present": sum(1 for r in rows if r["stage"] not in (None, "unknown")),
            "first_ts": rows[0]["ts"] if rows else None,
            "last_ts": rows[-1]["ts"] if rows else None,
        }
    except Exception as exc:
        out["raw_samples_error"] = repr(exc)
        rows = []

    first_ts = rows[0]["ts"] if rows else None
    last_ts = rows[-1]["ts"] if rows else None

    # ---- 2. reconstructed architecture + Perfect Sleep Index ------------------------------
    try:
        night = reconstruct_night_summary(repo, night_date)
        out["architecture"] = {
            "total_sleep_min": night.total_sleep_min, "deep_min": night.deep_min,
            "rem_min": night.rem_min, "light_min": night.light_min,
            "sleep_onset_latency_min": night.sleep_onset_latency_min,
            "waso_min": night.waso_min, "wake_events": night.wake_events,
            "sleep_efficiency": night.sleep_efficiency, "avg_hr": night.avg_hr,
            "avg_hrv": night.avg_hrv, "avg_respiratory_rate": night.avg_respiratory_rate,
        }
        if night.total_sleep_min:
            psi = perfect_sleep_index(night, NightMode.NORMAL)
            out["perfect_sleep_index"] = psi
    except Exception as exc:
        out["architecture_error"] = repr(exc)

    # ---- 2b. sleep-onset detection (which signals fired, incl. accelerometer/stillness) ---
    try:
        onset_rows = conn.execute(
            "SELECT ts, message, data FROM events WHERE category='sleep' "
            "AND code='onset_confirmed' AND ts >= ? AND ts <= ? ORDER BY ts ASC",
            (first_ts, last_ts),
        ).fetchall() if first_ts and last_ts else []
        onset_events = []
        for r in onset_rows:
            try:
                d = json.loads(r["data"]) if r["data"] else {}
            except Exception:
                d = {}
            signals = d.get("signals") or []
            onset_events.append({
                "ts": r["ts"], "confidence": d.get("confidence"),
                "latency_min": d.get("latency_min"), "signals": signals,
                "accelerometer_contributed": "stillness" in signals,
            })
        out["onset_events"] = onset_events
    except Exception as exc:
        out["onset_events_error"] = repr(exc)

    # ---- 3. staging + wake detection -------------------------------------------------------
    try:
        stage_counts = Counter(r["stage"] or "None" for r in rows)
        prev = None
        transitions = []
        for r in rows:
            if r["stage"] != prev:
                transitions.append({"ts": r["ts"], "stage": r["stage"],
                                    "confidence": r["stage_confidence"]})
                prev = r["stage"]
        out["staging"] = {
            "stage_distribution": dict(stage_counts),
            "transitions": transitions,
            "wake_events": [{"ts": r["ts"], "stage": r["stage"]} for r in rows if r["wake_event"]],
        }
        wl = conn.execute("SELECT * FROM wake_log WHERE date = ?", (night_date,)).fetchone()
        out["wake_log"] = dict(wl) if wl else None
    except Exception as exc:
        out["staging_error"] = repr(exc)

    # ---- 3b. thermal exposure -----------------------------------------------------------------
    # A night can be entirely inside the comfort band at every instant and still be far too cold,
    # because nothing about a single reading captures how LONG the bed sat there. 2026-08-24 held
    # 65-67F for four straight hours and the user reported waking from cold. Publish the measured
    # comfort edges alongside the actual exposure so that is visible off-box instead of inferred.
    try:
        prof = None
        for getter in ("get_comfort_profile",):
            fn = getattr(repo, getter, None)
            if callable(fn):
                try:
                    prof = fn()
                except Exception:
                    prof = None
                break
        out["comfort_profile"] = prof
        from sleepctl.controller.calibration import level_to_fahrenheit

        levels = [(r["ts"], r["commanded_level"], r["controller_state"])
                  for r in rows if r["commanded_level"] is not None]
        maint = [lv for _, lv, st in levels if st in ("maintenance", "wake_recovery")]
        exposure = {"n_commanded": len(levels)}
        if maint:
            temps = [level_to_fahrenheit(int(lv)) for lv in maint]
            exposure.update({
                "maintenance_min_f": round(min(temps), 1),
                "maintenance_mean_f": round(sum(temps) / len(temps), 1),
                "maintenance_max_f": round(max(temps), 1),
            })
            cool_edge = (prof or {}).get("cool_edge_f") if isinstance(prof, dict) else None
            if cool_edge is not None:
                near = sum(1 for t in temps if t <= float(cool_edge) + 1.0)
                exposure["minutes_within_1F_of_cool_edge"] = near
                exposure["frac_at_cool_edge"] = round(near / len(temps), 3)
        out["thermal_exposure"] = exposure
    except Exception as exc:
        out["thermal_exposure_error"] = repr(exc)

    # ---- 4. thermal steering ----------------------------------------------------------------
    try:
        iv = conn.execute(
            "SELECT ts, controller_state, action, magnitude_f, reason, held, reverted "
            "FROM interventions WHERE night_date = ? ORDER BY id ASC",
            (night_date,),
        ).fetchall()
        out["interventions"] = [dict(r) for r in iv]
        out["interventions_summary"] = {
            "n": len(iv),
            "by_action": dict(Counter(r["action"] for r in iv)),
            "by_controller_state": dict(Counter(r["controller_state"] for r in iv)),
        }
    except Exception as exc:
        out["interventions_error"] = repr(exc)

    return out


def export_bytes(export: dict) -> bytes:
    """Deterministic, stable-ordered JSON encoding (+ trailing newline) for git-friendly diffs."""
    return json.dumps(export, indent=2, sort_keys=True, default=str).encode("utf-8") + b"\n"


def _write_error_file(out_path: str, exc: BaseException) -> None:
    payload = {"schema": SCHEMA, "error": repr(exc), "generated_utc": _iso(None)}
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    except Exception:
        pass
    with open(out_path, "wb") as fh:
        fh.write(export_bytes(payload))


def write_exports(db_path: str, out_dir: str, nights: int = 14) -> list:
    """Write one JSON file per recent night_date into ``out_dir``; return the paths written.

    A normal empty/partial night is a success (exit 0). A HARD failure (couldn't open the DB)
    still writes a minimal error file and raises ``SystemExit(1)`` so the PS layer records FAIL.
    """
    repo = None
    try:
        repo = _build_repo(db_path)
        dates = _recent_night_dates(repo.conn, nights)
        os.makedirs(out_dir, exist_ok=True)
        paths = []
        for d in dates:
            export = build_night_export(repo, d)
            path = os.path.join(out_dir, f"night-{d}.json")
            with open(path, "wb") as fh:
                fh.write(export_bytes(export))
            paths.append(path)
        return paths
    except Exception as exc:
        try:
            _write_error_file(os.path.join(out_dir, "error.json"), exc)
        except Exception:
            pass
        raise SystemExit(1)
    finally:
        if repo is not None:
            try:
                repo.close()
            except Exception:
                pass


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.stderr.write("usage: python -m app.night_export <db_path> <out_dir> [nights]\n")
        raise SystemExit(2)
    _db_path = sys.argv[1]
    _out_dir = sys.argv[2]
    _nights = int(sys.argv[3]) if len(sys.argv) > 3 else 14
    _paths = write_exports(_db_path, _out_dir, nights=_nights)
    for p in _paths:
        print(p)
