"""Unit tests for the night-data publisher (app/night_export.py).

Mirrors test_health_snapshot.py's fixture style: a throwaway Repository over a temp SQLite
file with the dashboard tables applied, isolated per test.
"""

from __future__ import annotations

import json

import pytest

from app import night_export


@pytest.fixture()
def repo(tmp_path):
    from sleepctl.storage.repository import Repository
    from app import db as app_db
    import sqlite3

    r = Repository(str(tmp_path / "night_test.db"), check_same_thread=False)
    r.conn.row_factory = sqlite3.Row
    r.conn.executescript(app_db._DASHBOARD_DDL)
    app_db._apply_migrations(r.conn)
    r.conn.commit()
    yield r
    r.close()


def _insert_sample(conn, ts, night_date, **kw):
    defaults = dict(stage=None, stage_confidence=None, heart_rate=None, hrv=None,
                     respiratory_rate=None, movement=None, presence=None, bed_temp_f=None,
                     commanded_level=None, controller_state=None, wake_event=0)
    defaults.update(kw)
    conn.execute(
        "INSERT INTO raw_samples (ts, night_date, stage, stage_confidence, heart_rate, hrv, "
        "respiratory_rate, movement, presence, bed_temp_f, commanded_level, controller_state, "
        "wake_event) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, night_date, defaults["stage"], defaults["stage_confidence"], defaults["heart_rate"],
         defaults["hrv"], defaults["respiratory_rate"], defaults["movement"],
         defaults["presence"], defaults["bed_temp_f"], defaults["commanded_level"],
         defaults["controller_state"], defaults["wake_event"]),
    )


def _insert_onset_event(conn, ts, signals, confidence=0.6, latency_min=12.0):
    conn.execute(
        "INSERT INTO events (ts, category, severity, code, message, data) VALUES (?,?,?,?,?,?)",
        (ts, "sleep", "info", "onset_confirmed", "onset confirmed",
         json.dumps({"confidence": confidence, "latency_min": latency_min, "signals": signals})),
    )


# ------------------------------------------------------------------ build_night_export
def test_no_data_produces_empty_but_valid_shape(repo):
    out = night_export.build_night_export(repo, "2026-08-23")
    assert out["schema"] == "sleepctl.night_data/v1"
    assert out["night_date"] == "2026-08-23"
    assert out["raw_samples"] == []
    assert out["sensor_capture"]["n_samples"] == 0
    assert out["onset_events"] == []


def test_raw_samples_and_sensor_capture_reflect_the_data(repo):
    _insert_sample(repo.conn, "2026-08-23T22:00:00", "2026-08-23", stage="awake",
                    heart_rate=68, movement=0.5)
    _insert_sample(repo.conn, "2026-08-23T22:30:00", "2026-08-23", stage="light",
                    heart_rate=62, movement=0.1)
    repo.conn.commit()
    out = night_export.build_night_export(repo, "2026-08-23")
    assert out["sensor_capture"]["n_samples"] == 2
    assert out["sensor_capture"]["heart_rate_present"] == 2
    assert len(out["raw_samples"]) == 2


def test_onset_event_within_the_nights_span_is_captured_with_its_signals(repo):
    _insert_sample(repo.conn, "2026-08-23T22:00:00", "2026-08-23", stage="awake")
    _insert_sample(repo.conn, "2026-08-23T23:00:00", "2026-08-23", stage="deep")
    _insert_onset_event(repo.conn, "2026-08-23T22:45:00",
                        ["stillness", "hr_drop", "hr_trend_down"])
    repo.conn.commit()
    out = night_export.build_night_export(repo, "2026-08-23")
    assert len(out["onset_events"]) == 1
    ev = out["onset_events"][0]
    assert ev["accelerometer_contributed"] is True
    assert "stillness" in ev["signals"]


def test_onset_event_outside_the_nights_span_is_not_pulled_in(repo):
    # only a single instantaneous sample -> span is a single point, well before this event
    _insert_sample(repo.conn, "2026-08-23T22:00:00", "2026-08-23", stage="awake")
    _insert_onset_event(repo.conn, "2026-08-24T05:00:00", ["stillness"])
    repo.conn.commit()
    out = night_export.build_night_export(repo, "2026-08-23")
    assert out["onset_events"] == []


def test_wake_log_row_is_included_when_present(repo):
    repo.conn.execute(
        "INSERT INTO wake_log (date, woke_from_stage, minutes_early, window_min, forced, p_wake) "
        "VALUES (?,?,?,?,?,?)",
        ("2026-08-23", "light", 5, 20, 0, 0.7),
    )
    repo.conn.commit()
    out = night_export.build_night_export(repo, "2026-08-23")
    assert out["wake_log"]["woke_from_stage"] == "light"


def test_interventions_are_included_and_summarized(repo):
    repo.conn.execute(
        "INSERT INTO interventions (ts, night_date, controller_state, action, magnitude_f, "
        "reason, held, reverted) VALUES (?,?,?,?,?,?,?,?)",
        ("2026-08-23T23:00:00", "2026-08-23", "cooling", "cool", 1.5, "too warm", 0, 0),
    )
    repo.conn.commit()
    out = night_export.build_night_export(repo, "2026-08-23")
    assert out["interventions_summary"]["n"] == 1
    assert out["interventions_summary"]["by_action"] == {"cool": 1}


# ------------------------------------------------------------------ export_bytes / write_exports
def test_export_bytes_roundtrips():
    payload = {"schema": night_export.SCHEMA, "night_date": "2026-08-23"}
    raw = night_export.export_bytes(payload)
    assert isinstance(raw, bytes)
    assert raw.endswith(b"\n")
    assert json.loads(raw) == payload


def test_write_exports_writes_one_file_per_recent_night(tmp_path):
    from sleepctl.storage.repository import Repository
    import sqlite3

    db_path = str(tmp_path / "we_test.db")
    r = Repository(db_path, check_same_thread=False)
    _insert_sample(r.conn, "2026-08-22T22:00:00", "2026-08-22", stage="light")
    _insert_sample(r.conn, "2026-08-23T22:00:00", "2026-08-23", stage="light")
    r.conn.commit()
    r.close()

    out_dir = str(tmp_path / "out")
    paths = night_export.write_exports(db_path, out_dir, nights=14)
    assert len(paths) == 2
    names = {p.split("/")[-1] for p in paths}
    assert names == {"night-2026-08-22.json", "night-2026-08-23.json"}
    for p in paths:
        with open(p) as fh:
            parsed = json.load(fh)
        assert parsed["schema"] == night_export.SCHEMA


def test_write_exports_hard_failure_writes_error_file_and_exits_nonzero(tmp_path, monkeypatch):
    out_dir = str(tmp_path / "out")

    def boom(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(night_export, "_build_repo", boom)
    with pytest.raises(SystemExit) as excinfo:
        night_export.write_exports("/nonexistent/db.sqlite", out_dir)
    assert excinfo.value.code == 1
    with open(f"{out_dir}/error.json") as fh:
        parsed = json.load(fh)
    assert "db exploded" in parsed["error"]


def test_preempting_ticks_carry_their_reason_and_the_night_carries_a_reason_histogram(repo):
    from app import night_export
    night = "2026-09-07"
    for i in range(6):
        pre = json.dumps({"preemption": {"preempting": i % 2 == 0, "wake_risk": 0.3,
                                         "risk_reasons": ["light_stage"]}})
        repo.conn.execute(
            "INSERT INTO decisions (ts, night_date, state, thermal_intent, target_temp_f, target_level,"
            " action, reason, log_payload) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"2026-09-08T00:{i:02d}:00", night, "maintenance",
             "settle_cool" if i % 2 == 0 else "stabilize", 69.0, -54, "hold",
             f"maintenance -> settle_cool; clamped to band (was 66.{i}F)" if i % 2 == 0 else "hold", pre))
    repo.conn.execute("INSERT INTO efficacy_trials (night_date, arm, eligible, seed, resolved)"
                      " VALUES (?,?,?,?,0)", (night, "active", 1, 0.223))
    repo.conn.commit()
    out = night_export.build_night_export(repo, night)
    ev = out["preemption_events"]
    assert len(ev) == 3 and ev[0]["intent"] == "settle_cool" and ev[0]["target_f"] == 69.0
    assert "clamped" in ev[0]["reason"]
    hist = out["decision_reasons"]["maintenance"]
    assert any(h["reason"].endswith("clamped to band (was #F)") and h["n"] == 3 for h in hist), hist
    assert out["trial_arms"]["efficacy"]["arm"] == "active"


def test_the_onset_trace_and_thermal_profile_are_exported(repo):
    from app import night_export
    night = "2026-09-07"
    for i in range(4):
        pl = {"stage": "light", "stage_confidence": 0.6,
              "thermal_profile": {"neutral_f": 71.5, "neutral_is_measured": False},
              "onset": {"signals": ["asleep_stage"], "run_len": i, "transition_hits": 0,
                        "awake_hr_ref": 70.2, "entry_ref_n": 8, "confirmed": False}}
        repo.conn.execute(
            "INSERT INTO decisions (ts, night_date, state, action, reason, log_payload) VALUES (?,?,?,?,?,?)",
            (f"2026-09-07T21:{42+i}:00", night, "induction", "hold", "x", json.dumps(pl)))
    repo.conn.commit()
    out = night_export.build_night_export(repo, night)
    assert len(out["onset_trace"]) == 4 and out["onset_trace"][0]["awake_hr_ref"] == 70.2
    assert out["thermal_profile"]["neutral_f"] == 71.5
